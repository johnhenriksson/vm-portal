"""Proxmox VE through its HTTP API, the bpg/proxmox OpenTofu provider and Packer's
proxmox-iso builder. Implements base.Platform.

Every call acts on one backend (a Proxmox VE cluster); the portal talks to the node in its
settings. Differences from vSphere worth knowing:

  * A VM's id is its numeric VMID. Names aren't unique in Proxmox: the portal acts on its
    own VMs by id, and looks templates up by name only when the name is unambiguous.
  * Authentication is an API token, sent with every request. There is no session.
  * Proxmox's own certificates are signed by the cluster's CA, which nothing else trusts.
    Rather than turning verification off, the admin pins the certificate's SHA-256
    fingerprint (the node > System > Certificates). OpenTofu and Packer get that exact
    certificate as their only trusted one (SSL_CERT_FILE).
  * cloud-init data travels on a small seed ISO (NoCloud, label CIDATA) that the portal
    builds, uploads to the ISO storage through the API and attaches to the new VM. Proxmox's
    own "snippets" would need SSH to every node. Once the VM has booted, the seed is
    replaced by a copy without the secrets (keys, console password hash, compose file);
    the copy keeps the instance id, so cloud-init doesn't treat later boots as a new VM.

All functions are blocking unless async: call them with asyncio.to_thread.
"""
import asyncio
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import ssl
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import yaml

from ..config import get_bootstrap, get_settings
from .base import CREATED_BY, Autostart, ConsoleTicket, VMInfo

NAME = "proxmox"
LABEL = "Proxmox VE"
SUMMARY = "A node or cluster, with an API token"
API = "Proxmox"
CAN_PROVISION = True
GUEST_POWER_NEEDS_TOOLS = False     # without the guest agent, Proxmox shuts down and reboots over ACPI
GUEST_TOOLS = "the QEMU guest agent"
AUTOSTART_HINT = "It's the VM's \"Start at boot\" option in Proxmox."
HOST_AUTOSTART_HELP = ""            # a node always starts the VMs set to start at boot
SERVER_FIELD = "proxmox_server"
SECRET_FIELD = "proxmox_token_secret"
CONNECTION = ("proxmox_server", "proxmox_token_id", "proxmox_token_secret", "proxmox_fingerprint")
PLACEMENT = ("proxmox_node", "proxmox_storage", "proxmox_iso_storage", "proxmox_bridge", "proxmox_pool",
             "proxmox_vlan", "proxmox_nodes")
REQUIRED = ("proxmox_server", "proxmox_token_id", "proxmox_token_secret", "proxmox_node", "proxmox_storage",
            "proxmox_iso_storage", "proxmox_bridge")
PACKER_SOURCE = "proxmox-iso"
LOCATION_LABEL = "Node"

TIMEOUT = 15
MAX_RESPONSE = 32 * 1024 * 1024
MAX_UPLOAD = 16 * 1024 * 1024        # a seed ISO is a few hundred KB


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --- the HTTP API -----------------------------------------------------------------

@dataclass(frozen=True)
class Conn:
    server: str          # host[:port]; the API listens on 8006
    token_id: str
    token_secret: str
    fingerprint: str     # "" = verify against the system's CAs

    @property
    def auth(self) -> str:
        return f"PVEAPIToken={self.token_id}={self.token_secret}"


def fingerprint_of(der: bytes) -> str:
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, 64, 2))


def _host_port(server: str) -> tuple[str, int]:
    host, _, port = server.partition(":")
    return host, int(port or 8006)


def _open(c: Conn, timeout: float = TIMEOUT) -> http.client.HTTPSConnection:
    """A connection whose certificate has been checked. Nothing (in particular not the
    token) is sent before that."""
    host, port = _host_port(c.server)
    if c.fingerprint:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE          # the pin below replaces the CA check
    else:
        ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    try:
        conn.connect()
        if c.fingerprint:
            seen = fingerprint_of(conn.sock.getpeercert(binary_form=True))
            if not hmac.compare_digest(seen, c.fingerprint):
                raise RuntimeError(f"The certificate of {host} doesn't match the saved fingerprint. It now "
                                   f"presents {seen}. Only save that if the certificate was really replaced")
    except ssl.SSLCertVerificationError as e:
        conn.close()
        seen = _presented_fingerprint(host, port)
        hint = (f" It presents the certificate with SHA-256 fingerprint {seen}. If that is what Proxmox shows "
                f"for this node (System > Certificates), paste it into the fingerprint field" if seen else "")
        raise RuntimeError(f"The certificate of {host} isn't trusted ({e.verify_message}).{hint}") from None
    except BaseException:
        conn.close()
        raise
    return conn


def _presented_fingerprint(host: str, port: int) -> str | None:
    """The certificate a server shows, without trusting it or sending it anything."""
    pem = _presented_pem(host, port)
    return fingerprint_of(ssl.PEM_cert_to_DER_cert(pem)) if pem else None


def _presented_pem(host: str, port: int) -> str | None:
    try:
        return ssl.get_server_certificate((host, port), timeout=TIMEOUT)
    except (OSError, ssl.SSLError):
        return None


def _path(*parts: str | int) -> str:
    return "/api2/json/" + "/".join(quote(str(p), safe="") for p in parts)


def request(c: Conn, method: str, parts: tuple, params: dict | None = None, *, files: dict | None = None,
            timeout: float = TIMEOUT):
    """One API call; returns the response's `data`. GET/DELETE take params in the query,
    POST/PUT as a form (files: {field: (filename, bytes)} makes it multipart)."""
    url = _path(*parts)
    params = {k: v for k, v in (params or {}).items() if v is not None}
    body, headers = None, {"Authorization": c.auth, "Accept": "application/json"}
    if method in ("GET", "DELETE"):
        if params:
            url += "?" + urlencode(params)
    elif files:
        boundary = uuid.uuid4().hex
        chunks = []
        for k, v in params.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (filename, data) in files.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{filename}"\r\n'
                          f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        body = urlencode(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn = _open(c, timeout)
    try:
        conn.request(method, url, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise RuntimeError("Proxmox sent an unexpectedly large answer")
        if resp.status == 401:
            raise ApiError(401, "Proxmox refused the API token (check the token ID and secret)")
        if resp.status >= 400:
            # Proxmox puts the reason in the status line; it never contains the token.
            raise ApiError(resp.status, f"Proxmox answered {resp.status}: {resp.reason or 'error'}")
        try:
            return json.loads(data).get("data")
        except (ValueError, AttributeError):
            raise RuntimeError(f"{c.server} didn't answer like a Proxmox API (is the port right? It is 8006 "
                               "by default)") from None
    finally:
        conn.close()


def conn(b) -> Conn:
    if not b.configured:
        raise RuntimeError(f"{b.name} is not configured yet (Settings → Backends)")
    c = b.cfg
    return Conn(c.proxmox_server, c.proxmox_token_id, c.proxmox_token_secret, c.proxmox_fingerprint)


def _get(b, *parts, **params):
    return request(conn(b), "GET", parts, params)


def wait_task(b, node: str, upid, timeout: int = 600) -> None:
    """Wait for a Proxmox task (the UPID an API call returned) to finish successfully."""
    if not isinstance(upid, str) or not upid.startswith("UPID:"):
        return   # a synchronous call
    deadline = time.monotonic() + timeout
    while True:
        st = _get(b, "nodes", node, "tasks", upid, "status") or {}
        if st.get("status") == "stopped":
            if st.get("exitstatus") != "OK":
                raise RuntimeError(f"Proxmox task failed: {str(st.get('exitstatus'))[:200]}")
            return
        if time.monotonic() > deadline:
            raise TimeoutError("Proxmox task timed out")
        time.sleep(1)


# --- VMs --------------------------------------------------------------------------

CACHE_SECONDS = 5        # the list of VMs (one call)
DETAIL_SECONDS = 60      # per VM: notes, creation time, addresses (a call or two each)
LOOKUPS = 8              # detail calls running at once

_lock = threading.Lock()
_rows_cache: dict[str, tuple[float, list[dict]]] = {}
_details: dict[tuple, tuple[float, dict]] = {}
_iso_cache: dict[str, tuple[float, tuple, list[str]]] = {}

POWER = {"running": "poweredOn", "stopped": "poweredOff"}
# Interfaces that aren't the VM's own address: loopback, and the bridges and veth pairs
# container runtimes create (every Docker host has a 172.17.0.1).
INTERNAL_IFACE_RE = re.compile(r"^(lo|docker\d*|br-[0-9a-f]+|veth.*|virbr\d*|cni.*|flannel.*|cali.*|tailscale\d*)$")
IPCONFIG_RE = re.compile(r"(?:^|,)ip=(\d{1,3}(?:\.\d{1,3}){3})/\d{1,2}")


def close(b=None) -> None:
    """Forget what was read with old settings (there's no session to end)."""
    with _lock:
        for cache in (_rows_cache, _iso_cache):
            for k in [k for k in cache if b is None or k.split(":")[0] == str(b.id)]:
                del cache[k]
        for k in [k for k in _details if b is None or k[0].split(":")[0] == str(b.id)]:
            del _details[k]
        _inv_cache.clear()


def _invalidate(b) -> None:
    with _lock:
        _rows_cache.pop(b.key, None)


def _rows(b, fresh: bool = False) -> list[dict]:
    """Every QEMU guest on the cluster, as /cluster/resources lists them."""
    with _lock:
        hit = _rows_cache.get(b.key)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    rows = [r for r in _get(b, "cluster", "resources", type="vm") or []
            if r.get("type") == "qemu" and r.get("node") and r.get("vmid")]
    with _lock:
        _rows_cache[b.key] = (time.monotonic(), rows)
    return rows


def created_at(meta: str) -> datetime | None:
    """From a VM's `meta` option, e.g. "creation-qemu=9.0.2,ctime=1712345678"."""
    m = re.search(r"(?:^|,)ctime=(\d{9,11})(?:,|$)", meta or "")
    return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc) if m else None


def guest_ips(interfaces: list[dict]) -> list[str]:
    """IPv4 addresses the guest agent reports on the VM's own interfaces."""
    out = []
    for nic in interfaces or []:
        if INTERNAL_IFACE_RE.match(str(nic.get("name", ""))):
            continue
        for a in nic.get("ip-addresses") or []:
            ip = str(a.get("ip-address", ""))
            if a.get("ip-address-type") == "ipv4" and not ip.startswith(("127.", "169.254.")):
                out.append(ip)
    return list(dict.fromkeys(out))


def _agent_on(cfg: dict) -> bool:
    return str(cfg.get("agent") or "0").split(",")[0] in ("1", "enabled=1")


def _detail(b, c: Conn, row: dict) -> dict:
    """What the list doesn't say about a VM. Cached longer than the list, and per status,
    so a VM that was just started or stopped is looked at again."""
    key = (b.key, row["node"], row["vmid"], row.get("status"))
    with _lock:
        hit = _details.get(key)
    # A running VM whose agent doesn't answer (yet: it may be booting) is looked at again soon.
    if hit and time.monotonic() - hit[0] < (DETAIL_SECONDS if hit[1]["agent"] or row.get("status") != "running"
                                            else CACHE_SECONDS):
        return hit[1]
    d = {"notes": "", "created": None, "static": [], "ips": [], "agent": False, "disk_gb": None}
    try:
        cfg = request(c, "GET", ("nodes", row["node"], "qemu", row["vmid"], "config")) or {}
    except ApiError:
        cfg = {}     # e.g. the token may see the VM but not its configuration
    d["notes"] = str(cfg.get("description") or "")
    d["created"] = created_at(str(cfg.get("meta") or ""))
    d["disk_gb"] = _disk_gb(cfg) or None
    d["static"] = [m.group(1) for k, v in cfg.items() if re.fullmatch(r"ipconfig\d+", k)
                   for m in [IPCONFIG_RE.search(str(v))] if m]
    if _agent_on(cfg) and row.get("status") == "running" and not row.get("template"):
        try:
            res = request(c, "GET", ("nodes", row["node"], "qemu", row["vmid"], "agent", "network-get-interfaces"))
            d["ips"], d["agent"] = guest_ips((res or {}).get("result") or []), True
        except (ApiError, OSError):
            pass     # the agent isn't running (yet): no addresses, and no guest shutdown
    with _lock:
        if len(_details) > 5000:
            _details.clear()
        _details[key] = (time.monotonic(), d)
    return d


def booted_at(row: dict) -> datetime | None:
    """When a running VM last started, from its uptime in the list. Proxmox's reboot stops
    and starts the VM, so this moves on a reboot."""
    up = row.get("uptime")
    if row.get("status") != "running" or not isinstance(up, (int, float)) or up <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=int(up))


def _to_info(row: dict, d: dict) -> VMInfo:
    ips = list(dict.fromkeys([*d["static"], *d["ips"]]))
    return VMInfo(name=str(row.get("name") or f"VM {row['vmid']}"), is_template=bool(row.get("template")),
                  power_state=POWER.get(row.get("status"), "suspended"), ip=ips[0] if ips else None, ips=ips,
                  created=d["created"], notes=d["notes"], tools_running=d["agent"], vm_id=str(row["vmid"]), host=str(row["node"]),
                  disk_gb=d.get("disk_gb") if row.get("template") else None, booted=booted_at(row))


def all_vms(b, fresh: bool = False) -> list[VMInfo]:
    rows, c = _rows(b, fresh), conn(b)
    if not rows:
        return []
    with ThreadPoolExecutor(max_workers=LOOKUPS) as pool:
        details = list(pool.map(lambda r: _detail(b, c, r), rows))
    return [_to_info(r, d) for r, d in zip(rows, details)]


def templates(b) -> list[VMInfo]:
    """Templates matching the configured prefixes, newest first."""
    from ..images import template_prefixes
    prefixes = tuple(p + "-" for p in template_prefixes())
    ts = [v for v in all_vms(b) if v.is_template and v.name.startswith(prefixes)]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(ts, key=lambda t: t.created or epoch, reverse=True)


def vms_by_name(b, fresh: bool = False) -> dict[str, VMInfo]:
    return {v.name: v for v in all_vms(b, fresh) if not v.is_template}


def used_ips(b) -> set[str]:
    """Every IPv4 address Proxmox knows a VM has: set in its cloud-init options, or
    reported by its guest agent."""
    return {ip for v in all_vms(b) for ip in v.ips}


def name_exists(b, name: str) -> bool:
    return any(r.get("name") == name for r in _rows(b, fresh=True))


def _find_vm(b, name: str, vm_id: str | None) -> dict:
    """The portal's VM, by its VMID when known (and only if it still has that name), so a
    VM that has, or later gets, the same name is never the one acted on."""
    rows = _rows(b, fresh=True)
    if vm_id:
        row = next((r for r in rows if str(r["vmid"]) == str(vm_id)), None)
        if row is None or row.get("name") != name or row.get("template"):
            raise LookupError(f"VM {name!r} is no longer in Proxmox")
        return row
    matches = [r for r in rows if r.get("name") == name and not r.get("template")]
    if len(matches) != 1:
        raise LookupError(f"VM {name!r} not found in Proxmox" if not matches else
                          f"More than one VM in Proxmox is named {name!r}; the portal won't guess which")
    return matches[0]


def _find_template(b, name: str) -> dict:
    matches = [r for r in _rows(b, fresh=True) if r.get("name") == name and r.get("template")]
    if len(matches) != 1:
        raise LookupError(f"Template {name!r} not found in Proxmox" if not matches else
                          f"More than one template in Proxmox is named {name!r}; the portal won't guess which")
    return matches[0]


POWER_CALLS = {"on": "start", "off": "stop", "shutdown": "shutdown", "reboot": "reboot"}


def power(b, name: str, action: str, vm_id: str | None = None) -> None:
    """action: on, off (hard), shutdown (guest), reboot (guest)."""
    if action not in POWER_CALLS:
        raise ValueError(f"unknown power action {action!r}")
    row = _find_vm(b, name, vm_id)
    upid = request(conn(b), "POST", ("nodes", row["node"], "qemu", row["vmid"], "status", POWER_CALLS[action]))
    if action in ("on", "off"):
        wait_task(b, row["node"], upid, timeout=300)
    _invalidate(b)


def autostart(b, name: str, vm_id: str | None = None) -> Autostart:
    """Proxmox keeps it on the VM ("Start at boot"); the node always honours it."""
    row = _find_vm(b, name, vm_id)
    cfg = _get(b, "nodes", row["node"], "qemu", row["vmid"], "config") or {}
    return Autostart(enabled=str(cfg.get("onboot", "0")) == "1", host=row["node"], host_enabled=True)


def set_autostart(b, name: str, vm_id: str | None, enabled: bool) -> Autostart:
    row = _find_vm(b, name, vm_id)
    request(conn(b), "PUT", ("nodes", row["node"], "qemu", row["vmid"], "config"), {"onboot": int(enabled)})
    return autostart(b, name, str(row["vmid"]))


def delete_template(b, name: str) -> None:
    row = _find_template(b, name)
    upid = request(conn(b), "DELETE", ("nodes", row["node"], "qemu", row["vmid"]), {"purge": 1})
    wait_task(b, row["node"], upid)
    _invalidate(b)


def list_isos(b) -> list[str]:
    """ISOs on the backend's ISO storage, as volume ids ("local:iso/debian-13.iso"). The
    portal's own seed ISOs aren't installers and are left out."""
    c = b.cfg
    key = (c.proxmox_node, c.proxmox_iso_storage)
    with _lock:
        hit = _iso_cache.get(b.key)
    if hit and hit[1] == key and time.monotonic() - hit[0] < 60:
        return hit[2]
    items = _get(b, "nodes", c.proxmox_node, "storage", c.proxmox_iso_storage, "content", content="iso") or []
    isos = sorted(str(i["volid"]) for i in items if i.get("volid") and SEED_PREFIX not in str(i["volid"]))
    with _lock:
        _iso_cache[b.key] = (time.monotonic(), key, isos)
    return isos


def iso_name(iso: str) -> str:
    return iso.split("/")[-1]


# --- what the Settings page offers ---------------------------------------------------

INVENTORY_SECONDS = 60
_inv_cache: dict[str, tuple[float, dict]] = {}


def _gb(n) -> int:
    return int((n or 0) / 1024 ** 3)


# What the portal's API token needs (README, "Proxmox VE"). The guest agent is one of two,
# by version.
PRIVILEGES = ("VM.Allocate", "VM.Clone", "VM.Audit", "VM.Config.CDROM", "VM.Config.CPU", "VM.Config.Disk",
              "VM.Config.HWType", "VM.Config.Memory", "VM.Config.Network", "VM.Config.Options", "VM.PowerMgmt",
              "VM.Console", "Datastore.Audit", "Datastore.AllocateSpace", "Datastore.AllocateTemplate",
              "Datastore.Allocate", "SDN.Use", "Sys.Audit")
AGENT_PRIVILEGES = ("VM.GuestAgent.Audit", "VM.Monitor")


def missing_privileges(privileges: list[str] | None) -> list[str]:
    """The needed privileges the token has on no path at all (it may still lack one on the
    storage or VM that matters; that shows when it is used). Empty when unknown."""
    if privileges is None:
        return []
    missing = [p for p in PRIVILEGES if p not in privileges]
    if not any(p in privileges for p in AGENT_PRIVILEGES):
        missing.append("VM.GuestAgent.Audit (Proxmox VE 9) or VM.Monitor (8)")
    return missing


def inventory(server: str, token_id: str, token_secret: str, fingerprint: str, fresh: bool = False) -> dict:
    """The cluster's nodes with their storages and bridges, and its resource pools.

    Works with a connection that isn't saved yet. Cached briefly per connection (the
    secret only as a hash).
    """
    key = hashlib.sha256("\0".join([server, token_id, token_secret, fingerprint]).encode()).hexdigest()
    hit = _inv_cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < INVENTORY_SECONDS:
        return hit[1]
    c = Conn(server, token_id, token_secret, fingerprint)
    version = request(c, "GET", ("version",)) or {}
    nodes = []
    for n in sorted(request(c, "GET", ("nodes",)) or [], key=lambda n: str(n.get("node", ""))):
        name, online = str(n.get("node", "")), n.get("status") == "online"
        node = {"name": name, "online": online, "storages": [], "bridges": []}
        if online:
            for st in request(c, "GET", ("nodes", name, "storage")) or []:
                if not st.get("enabled", 1) or not st.get("active", 1):
                    continue
                node["storages"].append({
                    "name": str(st.get("storage", "")), "type": str(st.get("type", "")),
                    "content": sorted(str(st.get("content", "")).split(",")),
                    "free_gb": _gb(st.get("avail")), "capacity_gb": _gb(st.get("total")),
                    "shared": bool(st.get("shared"))})
            node["storages"].sort(key=lambda s: s["name"].lower())
            node["bridges"] = sorted(str(x["iface"]) for x in
                                     request(c, "GET", ("nodes", name, "network"), {"type": "any_bridge"}) or []
                                     if x.get("iface"))
            if not node["bridges"]:     # for the message: what the node has instead
                try:
                    node["interfaces"] = sorted(str(x["iface"]) for x in
                                                request(c, "GET", ("nodes", name, "network")) or [] if x.get("iface"))
                except ApiError:
                    pass
        nodes.append(node)
    try:
        pools = sorted(str(p["poolid"]) for p in request(c, "GET", ("pools",)) or [] if p.get("poolid"))
    except ApiError:
        pools = []
    try:        # what the token may do, on any path: {path: {privilege: propagate}}
        perms = request(c, "GET", ("access", "permissions")) or {}
        privileges = sorted({p for on_path in perms.values() for p, on in on_path.items() if on is not None})
    except (ApiError, AttributeError):
        privileges = None       # unknown
    result = {"about": f"Proxmox VE {version.get('version', '?')}", "nodes": nodes, "pools": pools,
              "privileges": privileges, "missing_privileges": missing_privileges(privileges)}
    if len(_inv_cache) > 20:
        _inv_cache.clear()
    _inv_cache[key] = (time.monotonic(), result)
    return result


# --- console ------------------------------------------------------------------

def console_ticket(b, name: str, vm_id: str | None = None) -> ConsoleTicket:
    """A VNC ticket for the VM, relayed over the node's vncwebsocket. The ticket is also the
    VNC password, which the portal answers the challenge with (console.vnc_login)."""
    row = _find_vm(b, name, vm_id)
    if row.get("status") != "running":
        raise RuntimeError("The VM is not powered on")
    c = conn(b)
    # VNC passwords are 8 bytes: Proxmox generates a random one for the session when asked
    # (newer versions); older ones take the ticket itself as the password.
    call = ("nodes", row["node"], "qemu", row["vmid"], "vncproxy")
    try:
        t = request(c, "POST", call, {"websocket": 1, "generate-password": 1}) or {}
    except ApiError as e:
        if e.status != 400:
            raise
        t = request(c, "POST", call, {"websocket": 1}) or {}
    if not t.get("ticket") or not t.get("port"):
        raise RuntimeError("Proxmox didn't hand out a console ticket")
    host, port = _host_port(c.server)
    path = (_path("nodes", row["node"], "qemu", row["vmid"], "vncwebsocket") + "?"
            + urlencode({"port": t["port"], "vncticket": t["ticket"]}))
    return ConsoleTicket(host=host, port=port, path=path, thumbprint=c.fingerprint, ca_verified=not c.fingerprint,
                         headers={"Authorization": c.auth}, vnc_password=t.get("password") or t["ticket"])


# --- the seed ISO (cloud-init NoCloud) -------------------------------------------------

SEED_PREFIX = "vm-portal-seed-"


def seed_names(name: str) -> tuple[str, str]:
    """The file names of a VM's seed ISO with its secrets, and of the cleaned copy."""
    return f"{SEED_PREFIX}{name}.iso", f"{SEED_PREFIX}{name}-clean.iso"


def seed_files(metadata: str, userdata: str) -> dict[str, str]:
    """NoCloud's files from the guest driver's metadata and user-data. The network config is
    its own file there (on VMware it's inside the metadata); the guestinfo cleanup keys mean
    nothing on NoCloud."""
    md = yaml.safe_load(metadata) or {}
    network = md.pop("network", None)
    for k in ("redact", "cleanup-guestinfo"):
        md.pop(k, None)
    files = {"meta-data": yaml.safe_dump(md, sort_keys=False), "user-data": userdata}
    if network:
        files["network-config"] = yaml.safe_dump(network, sort_keys=False)
    return files


def clean_user_data() -> str:
    """What the seed keeps after the first boot. cloud-init has already applied the real
    user-data (and keeps its own copy, readable by root only); with the same instance id it
    doesn't run it again."""
    return "#cloud-config\n{}\n"


def build_iso(files: dict[str, str], out: Path) -> None:
    """A CIDATA ISO with these files (xorriso, as Packer's cd_content uses; genisoimage
    or mkisofs where that's what's installed)."""
    if shutil.which("xorriso"):
        cmd = [shutil.which("xorriso"), "-as", "mkisofs"]
    elif shutil.which("genisoimage") or shutil.which("mkisofs"):
        cmd = [shutil.which("genisoimage") or shutil.which("mkisofs")]
    else:
        raise RuntimeError("xorriso is not installed")
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        os.chmod(tmp, 0o700)
        for fname, text in files.items():
            Path(tmp, fname).write_text(text)
        import subprocess
        res = subprocess.run([*cmd, "-quiet", "-output", str(out), "-volid", "CIDATA", "-joliet", "-rock",
                              *sorted(files)], cwd=tmp, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"Couldn't build the seed ISO: {res.stderr.strip()[:300]}")


def upload_iso(b, data: bytes, filename: str, node: str | None = None) -> str:
    """Upload an ISO to the backend's ISO storage on a node (the backend's own unless given),
    replacing one of the same name; returns its volume id."""
    if len(data) > MAX_UPLOAD:
        raise RuntimeError("The seed ISO is unexpectedly large")
    c, cfg = conn(b), b.cfg
    node = node or cfg.proxmox_node
    volid = f"{cfg.proxmox_iso_storage}:iso/{filename}"
    delete_iso(b, volid, node)
    upid = request(c, "POST", ("nodes", node, "storage", cfg.proxmox_iso_storage, "upload"),
                   {"content": "iso"}, files={"filename": (filename, data)}, timeout=120)
    wait_task(b, node, upid, timeout=300)
    return volid


def delete_iso(b, volid: str, node: str | None = None) -> None:
    """Delete one of the portal's seed ISOs (on that node's view of the ISO storage: a
    local storage holds different files on each node); nothing if it's not there."""
    cfg = b.cfg
    node = node or cfg.proxmox_node
    if not volid.startswith(f"{cfg.proxmox_iso_storage}:iso/{SEED_PREFIX}"):
        raise ValueError(f"{volid} isn't one of the portal's seed ISOs")
    try:
        upid = request(conn(b), "DELETE", ("nodes", node, "storage", cfg.proxmox_iso_storage,
                                           "content", volid))
        wait_task(b, node, upid, timeout=120)
    except ApiError as e:
        if e.status not in (404, 500):   # 500: "volume does not exist" on some storages
            raise


def _make_seed(b, d: Path, files: dict[str, str], filename: str, node: str | None = None) -> str:
    out = d / ".seed.iso"
    out.unlink(missing_ok=True)
    try:
        build_iso(files, out)
        return upload_iso(b, out.read_bytes(), filename, node)
    finally:
        out.unlink(missing_ok=True)


def _disk_gb(cfg: dict) -> int:
    """The size of a VM's (template's) boot disk in GB, from its config."""
    for key in ("scsi0", "virtio0", "sata0", "ide0"):
        m = re.search(r"(?:^|,)size=(\d+)([KMGT]?)", str(cfg.get(key, "")))
        if m:
            n, unit = int(m.group(1)), m.group(2) or "G"
            return max(1, round(n * {"K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}[unit]))
    return 0


async def before_create(b, req, d: Path, log) -> dict:
    """Find the template's VMID, build and upload the seed ISO. Returns the variables for
    the module (template, seed and the disk size, which can't be below the template's)."""
    tmpl = await asyncio.to_thread(_find_template, b, req.template)
    tcfg = await asyncio.to_thread(_get, b, "nodes", tmpl["node"], "qemu", tmpl["vmid"], "config") or {}
    from .. import guests
    data = guests.get(req.guest).tfvars(req)
    # On the node the VM goes on: a local ISO storage holds different files on each node.
    node = tf_placement(b, getattr(req, "location", None))["node"]
    log.step(f"Uploading the first-boot seed ISO to {b.cfg.proxmox_iso_storage} on node {node}")
    volid = await asyncio.to_thread(_make_seed, b, d, seed_files(data["metadata"], data["userdata"]),
                                    seed_names(req.name)[0], node)
    from .. import db
    db.execute("UPDATE vms SET seed_pending=1 WHERE name=? AND status='provisioning'", (req.name,))
    return {"template_vmid": int(tmpl["vmid"]), "template_node": str(tmpl["node"]), "seed_iso": volid,
            "bios": "ovmf" if tcfg.get("bios") == "ovmf" else "seabios", "disk_gb": max(req.disk_gb, _disk_gb(tcfg))}


AGENT_WAIT = 600


def _agent_answers(b, row) -> bool:
    try:
        request(conn(b), "POST", ("nodes", row["node"], "qemu", row["vmid"], "agent", "ping"))
        return True
    except (ApiError, OSError):
        return False


def clean_seed(b, name: str, vm_id: str | None, wait: int = AGENT_WAIT) -> bool:
    """Once the VM has booted (its guest agent answers), swap its seed ISO for a copy without
    the secrets. False if the VM didn't come up in time (the seed stays)."""
    from .. import db, provision
    row = _find_vm(b, name, vm_id)
    deadline = time.monotonic() + wait
    while not _agent_answers(b, row):
        if time.monotonic() > deadline:
            return False
        time.sleep(5)
    tfvars = json.loads((provision.vm_dir(name) / "terraform.tfvars.json").read_text())
    files = seed_files(tfvars.get("metadata", ""), clean_user_data())
    first, clean = seed_names(name)
    volid = _make_seed(b, provision.vm_dir(name), files, clean, row["node"])
    cfg = _get(b, "nodes", row["node"], "qemu", row["vmid"], "config") or {}
    drive = next((k for k, v in cfg.items() if re.fullmatch(r"(ide|sata|scsi)\d+", k) and first in str(v)), "ide2")
    request(conn(b), "PUT", ("nodes", row["node"], "qemu", row["vmid"], "config"),
            {drive: f"{volid},media=cdrom"})
    delete_iso(b, f"{b.cfg.proxmox_iso_storage}:iso/{first}", row["node"])
    db.execute("UPDATE vms SET seed_pending=0 WHERE name=? AND status NOT IN ('destroyed', 'forgotten')", (name,))
    return True


async def after_create(b, req, vm_id: str | None, log) -> None:
    log.step("Waiting for the VM's first boot, to remove the secrets from its seed ISO")
    try:
        done = await asyncio.to_thread(clean_seed, b, req.name, vm_id)
    except Exception as e:  # noqa: BLE001 - the VM itself is fine; the VM page offers to retry
        log.write(f"Warning: couldn't clean up the seed ISO ({e}). Try again from the VM page.\n")
        return
    if done:
        log.write("Seed ISO replaced by a copy without keys, password hash or compose file\n")
    else:
        log.write("Warning: the VM's guest agent didn't answer in time, so its seed ISO still holds its keys and "
                  "console password hash. Clean it up from the VM page once the VM is up.\n")


async def after_destroy(b, name: str, vm_id: str | None, log) -> None:
    # The VM is gone, so which node it was on isn't known here: every node VMs go on is
    # asked (a shared storage answers on the first, a local one only where the file is).
    for fname in seed_names(name):
        for node in allowed_nodes(b):
            try:
                await asyncio.to_thread(delete_iso, b, f"{b.cfg.proxmox_iso_storage}:iso/{fname}", node)
            except Exception as e:  # noqa: BLE001 - the VM is gone either way
                log.write(f"Warning: couldn't delete {fname} on node {node} ({e}); remove it from the ISO "
                          "storage by hand.\n")


# --- certificates for OpenTofu and Packer ------------------------------------------------

def ca_file(b) -> Path | None:
    """With a pinned fingerprint: a file holding exactly that certificate (fetched and
    checked now), for OpenTofu's and Packer's SSL_CERT_FILE, so they trust it and nothing
    else. None when the certificate is to be trusted like any website's."""
    c = b.cfg
    if not c.proxmox_fingerprint:
        return None
    host, port = _host_port(c.proxmox_server)
    pem = _presented_pem(host, port)
    if pem is None:
        raise RuntimeError(f"Couldn't read the certificate of {host}")
    if not hmac.compare_digest(fingerprint_of(ssl.PEM_cert_to_DER_cert(pem)), c.proxmox_fingerprint):
        raise RuntimeError(f"The certificate of {host} doesn't match the saved fingerprint")
    d = get_bootstrap().data_dir / "backends"
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / f"{b.id}-ca.pem"
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(pem)
    tmp.replace(path)
    return path


def _endpoint(b) -> str:
    host, port = _host_port(b.cfg.proxmox_server)
    return f"https://{host}:{port}/"


# --- OpenTofu (terraform/proxmox, the bpg/proxmox provider) ------------------------------

TF_RESOURCE = ("proxmox_virtual_environment_vm", "this")
TF_PROVIDER = "registry.opentofu.org/bpg/proxmox"
TF_LEGACY_PROVIDER = "registry.terraform.io/bpg/proxmox"
TF_CONNECTION_VARS = ("proxmox_endpoint",)
TF_SECRET_VARS = ("proxmox_api_token",)
TF_PLACEMENT_VARS = ("node", "datastore", "bridge", "pool", "vlan")
TF_FACTS: tuple[str, ...] = ()        # later plans don't read the template: clone is ignored after create
TF_EXTRA_VARS = {"template_vmid": int, "template_node": str, "seed_iso": str, "bios": str}
TF_RESIZABLE = {("cpu", 0, "cores"), ("memory", 0, "dedicated"), ("disk", 0, "size")}
# Read back from the guest agent or set by Proxmox; they may show as unknown.
TF_RECOMPUTED = {"ipv4_addresses", "ipv6_addresses", "mac_addresses", "network_interface_names"}
TF_DEFAULTED: dict[str, str] = {}
VM_ID_RE = re.compile(r"^[1-9][0-9]{2,8}$")     # a VMID: 100 to 999999999
STATE_DATA: set[tuple[str, str]] = set()


def tf_module_dir() -> Path:
    return get_settings().terraform_module_dir.parent / "proxmox"


def tf_env(b) -> dict[str, str]:
    c = b.cfg
    env = {"TF_VAR_proxmox_endpoint": _endpoint(b),
           "TF_VAR_proxmox_api_token": f"{c.proxmox_token_id}={c.proxmox_token_secret}"}
    ca = ca_file(b)
    if ca:
        env["SSL_CERT_FILE"] = str(ca)
    return env


def allowed_nodes(b) -> list[str]:
    """The nodes the backend's settings allow VMs on: its own node first."""
    c = b.cfg
    return list(dict.fromkeys(n for n in [c.proxmox_node, *c.proxmox_nodes] if n))


def locations(b) -> list[str]:
    """The allowed nodes that are online now (all of them if the cluster can't say)."""
    allowed = allowed_nodes(b)
    if len(allowed) < 2:
        return allowed
    try:
        online = {str(n.get("node")) for n in _get(b, "nodes") or [] if n.get("status") == "online"}
    except (ApiError, OSError, RuntimeError):
        return allowed
    return [n for n in allowed if n in online]


def tf_placement(b, location: str | None = None) -> dict[str, str]:
    c = b.cfg
    if location and location not in allowed_nodes(b):
        raise ValueError(f"{b.name} doesn't put VMs on node {location!r}")
    return {"node": location or c.proxmox_node, "datastore": c.proxmox_storage, "bridge": c.proxmox_bridge,
            "pool": c.proxmox_pool, "vlan": c.proxmox_vlan}


def state_node(state: dict) -> tuple[dict | None, str | None]:
    """(the VM's attributes in a parsed state, the node they say it's on)."""
    for r in state.get("resources") or []:
        if (r.get("type"), r.get("name")) == TF_RESOURCE and r.get("mode") != "data":
            for inst in r.get("instances") or []:
                attrs = inst.get("attributes")
                if isinstance(attrs, dict):
                    return attrs, attrs.get("node_name")
    return None, None


async def before_change(b, d: Path, log) -> None:
    """A VM that was migrated to another node since: record the node it's on in its state
    and variables. The provider reads and changes a VM through the node in its state, and a
    node in the variables that differs from it would be a plan to move (or rebuild) the VM.
    The caller has already kept a copy of the state."""
    from .. import provision
    path = d / "terraform.tfstate"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    vmid = state_vm_id(state) if isinstance(state, dict) else None
    if not vmid:
        return
    rows = await asyncio.to_thread(_rows, b, True)
    row = next((r for r in rows if str(r["vmid"]) == vmid and not r.get("template")), None)
    if row is None:
        return          # gone from the cluster: the plan itself will say so
    node = str(row["node"])
    attrs, recorded = state_node(state)
    if attrs is not None and recorded != node:
        log.step(f"The VM is on node {node} now (it was created or last seen on {recorded}): recording that")
        attrs["node_name"] = node
        state["serial"] = int(state.get("serial") or 0) + 1
        provision.write_private(path, json.dumps(state, indent=2).encode())
    tfvars_path = d / "terraform.tfvars.json"
    try:
        current = json.loads(tfvars_path.read_text()).get("node")
    except (OSError, ValueError):
        return
    if current != node:
        provision.update_tfvars(d, node=node)


def tf_template_facts(values: dict | None) -> dict | None:
    return None


def tf_size(values: dict | None) -> dict | None:
    try:
        return {"cpus": int(values["cpu"][0]["cores"]), "memory_mb": int(values["memory"][0]["dedicated"]),
                "disk_gb": int(values["disk"][0]["size"])}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def state_vm_id(state: dict) -> str | None:
    """The VM's VMID in its OpenTofu state."""
    for r in state.get("resources") or []:
        if (r.get("type"), r.get("name")) == TF_RESOURCE and r.get("mode") != "data":
            for inst in r.get("instances") or []:
                attrs = inst.get("attributes") or {}
                vmid = attrs.get("vm_id", attrs.get("id"))
                if vmid is not None and str(vmid).isdigit():
                    return str(vmid)
    return None


# --- Packer (proxmox-iso) -----------------------------------------------------------------

def packer_vars(b) -> dict[str, str]:
    c = b.cfg
    env = {
        "PKR_VAR_proxmox_url": _endpoint(b) + "api2/json",
        "PKR_VAR_proxmox_token_id": c.proxmox_token_id,
        "PKR_VAR_proxmox_token_secret": c.proxmox_token_secret,
        "PKR_VAR_proxmox_node": c.proxmox_node,
        "PKR_VAR_proxmox_storage": c.proxmox_storage,
        "PKR_VAR_proxmox_iso_storage": c.proxmox_iso_storage,
        "PKR_VAR_proxmox_bridge": c.proxmox_bridge,
        "PKR_VAR_proxmox_pool": c.proxmox_pool,
        "PKR_VAR_proxmox_vlan": c.proxmox_vlan,
    }
    ca = ca_file(b)
    if ca:
        env["SSL_CERT_FILE"] = str(ca)
    return env


def api_host(b) -> str:
    return _host_port(b.cfg.proxmox_server)[0]


# --- backups ------------------------------------------------------------------------------

def import_problems(b, vm_id: str, name: str, tfvars: dict) -> list[str]:
    """Confirm the VMID belongs to that very VM, that it's not a template, that it's in the
    backend's pool (when one is set) and that vm-portal created it."""
    try:
        row = next((r for r in _rows(b, fresh=True) if str(r["vmid"]) == str(vm_id)), None)
        if row is None:
            return [f"Proxmox has no VM with id {vm_id}"]
        cfg = _get(b, "nodes", row["node"], "qemu", row["vmid"], "config") or {}
    except Exception as e:  # noqa: BLE001 - shown in the preview
        return [f"Couldn't check Proxmox: {e}"]
    problems = []
    if row.get("name") != name:
        problems.append(f"In Proxmox, {vm_id} is a VM named {row.get('name')!r}")
    if row.get("template"):
        problems.append(f"{vm_id} is a template")
    pool = b.cfg.proxmox_pool
    if pool and row.get("pool") != pool:
        problems.append(f"It's not in the backend's pool {pool!r}")
    if not str(cfg.get("description") or "").startswith(CREATED_BY):
        problems.append("vm-portal didn't create it (its Proxmox notes don't say so)")
    return problems
