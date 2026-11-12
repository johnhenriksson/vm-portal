"""VMware vSphere: vCenter through pyVmomi, the vmware/vsphere OpenTofu provider, and
Packer's vsphere-iso builder. Implements base.Platform.

Provisioning itself goes through OpenTofu; the API part is for listing templates,
showing VM state, powering VMs on/off and setting their autostart. Every call acts on one
backend (a vCenter): its connection and caches are kept per backend. All functions are
blocking: call them with asyncio.to_thread.
"""
import hashlib
import logging
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from ..config import get_settings
from .base import CREATED_BY, Autostart, ConsoleTicket, VMInfo, usable_ipv4

NAME = "vsphere"
LABEL = "VMware vSphere"
SUMMARY = "A vCenter, with a service account"
API = "vCenter"
CAN_PROVISION = True
GUEST_POWER_NEEDS_TOOLS = True      # RebootGuest/ShutdownGuest go through VMware Tools
GUEST_TOOLS = "VMware Tools"
AUTOSTART_HINT = "Set on the host in vCenter."
HOST_AUTOSTART_HELP = "An administrator can turn it on in vCenter (the host > Configure > VM Startup/Shutdown)."
SERVER_FIELD = "vsphere_server"
SECRET_FIELD = "vsphere_password"
CONNECTION = ("vsphere_server", "vsphere_user", "vsphere_password", "vsphere_insecure")
PLACEMENT = ("vsphere_datacenter", "vsphere_cluster", "vsphere_datastore", "vsphere_network", "vsphere_folder")
REQUIRED = ("vsphere_server", "vsphere_user", "vsphere_password", "vsphere_datacenter", "vsphere_cluster",
            "vsphere_datastore", "vsphere_network")
# Packer builds use this source type (see images/README.md).
PACKER_SOURCE = "vsphere-iso"
LOCATION_LABEL = ""                 # DRS (or the cluster) places a VM; there's nothing to choose
log = logging.getLogger(__name__)

PROPS = [
    "name",
    "config.template",
    "config.createDate",
    "config.annotation",
    "runtime.powerState",
    "runtime.bootTime",
    "guest.ipAddress",
    "guest.net",
    "guest.toolsRunningStatus",
]
CACHE_SECONDS = 5
DISK_SECONDS = 300      # a template's disk size: templates are rarely changed in place


@dataclass
class _Session:
    """One backend's connection and caches. pyVmomi calls on it are serialised by its lock;
    other backends aren't held up by a slow one."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    si: object | None = None
    rows: tuple[float, list[dict]] | None = None
    isos: tuple[float, tuple, list[str]] | None = None
    disks: dict[str, tuple[float, int | None]] = field(default_factory=dict)   # template moid -> its disk


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _session(b) -> _Session:
    with _sessions_lock:
        return _sessions.setdefault(b.key, _Session())


def _connect(b):
    """The backend's service instance; reconnects when the session expired. Called with
    the session's lock held."""
    st = _session(b)
    if st.si is not None:
        try:
            st.si.CurrentTime()
            return st.si
        except Exception:  # noqa: BLE001 - session expired or network blip; reconnect
            st.si = None
    if not b.configured:
        raise RuntimeError(f"{b.name} is not configured yet (Settings → Backends)")
    c = b.cfg
    st.si = _smart_connect(c.vsphere_server, c.vsphere_user, c.vsphere_password, c.vsphere_insecure)
    return st.si


def _smart_connect(server: str, user: str, password: str, insecure: bool):
    host, _, port = server.partition(":")
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    return SmartConnect(host=host, port=int(port or 443), user=user, pwd=password, sslContext=ctx,
                        httpConnectionTimeout=15)


def close(b=None) -> None:
    """Drop one backend's sessions (all of them without b): settings changed or stopping."""
    with _sessions_lock:
        keys = [k for k in _sessions if b is None or k.split(":")[0] == str(b.id)]
        gone = [_sessions.pop(k) for k in keys]
    for st in gone:
        with st.lock:
            if st.si is not None:
                try:
                    Disconnect(st.si)
                except Exception:  # noqa: BLE001 - it's going away anyway
                    pass
                st.si = None


def _collect(b, props: list[str]) -> list[dict]:
    """Fetch properties of all VMs in one round trip via the PropertyCollector."""
    si = _connect(b)
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        pc = vmodl.query.PropertyCollector
        spec = pc.FilterSpec(
            objectSet=[pc.ObjectSpec(obj=view, skip=True, selectSet=[
                pc.TraversalSpec(name="view", path="view", skip=False, type=vim.view.ContainerView)])],
            propSet=[pc.PropertySpec(type=vim.VirtualMachine, pathSet=props, all=False)],
        )
        out = []
        result = content.propertyCollector.RetrievePropertiesEx([spec], pc.RetrieveOptions())
        while result:
            for obj in result.objects:
                row = {p.name: p.val for p in obj.propSet}
                row["_obj"] = obj.obj
                out.append(row)
            if not result.token:
                break
            result = content.propertyCollector.ContinueRetrievePropertiesEx(result.token)
        return out
    finally:
        view.Destroy()


def _to_info(row: dict) -> VMInfo:
    ips = []
    for nic in row.get("guest.net") or []:
        ips.extend(a for a in (nic.ipAddress or []) if usable_ipv4(a))   # IPv4, not link-local
    created, booted = row.get("config.createDate"), row.get("runtime.bootTime")
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if booted and booted.tzinfo is None:
        booted = booted.replace(tzinfo=timezone.utc)
    return VMInfo(
        name=row.get("name", ""),
        is_template=bool(row.get("config.template")),
        power_state=str(row.get("runtime.powerState") or ""),
        # VMware Tools' primary address, unless it's IPv6 or link-local: then the first IPv4 one.
        ip=usable_ipv4(row.get("guest.ipAddress")) or (ips[0] if ips else None),
        ips=ips,
        created=created,
        notes=row.get("config.annotation") or "",
        tools_running=row.get("guest.toolsRunningStatus") == "guestToolsRunning",
        booted=booted,
    )


def _rows(b, fresh: bool = False) -> list[dict]:
    st = _session(b)
    with st.lock:
        if not fresh and st.rows and time.monotonic() - st.rows[0] < CACHE_SECONDS:
            return st.rows[1]
        rows = _collect(b, PROPS)
        st.rows = (time.monotonic(), rows)
        return rows


def all_vms(b, fresh: bool = False) -> list[VMInfo]:
    return [_to_info(r) for r in _rows(b, fresh)]


def first_disk_gb(devices) -> int | None:
    """The size in GB (rounded up) of the VM's first disk, the one a clone sizes as `disk.0`."""
    disks = sorted((d for d in devices or [] if isinstance(d, vim.vm.device.VirtualDisk)),
                   key=lambda d: (d.controllerKey, d.unitNumber or 0))
    if not disks:
        return None
    size = disks[0].capacityInBytes or (disks[0].capacityInKB or 0) * 1024
    return -(-size // 1024 ** 3) or None


def _template_disks(b, rows: list[dict]) -> dict[str, int | None]:
    """Each template's disk size by its moid, in one round trip for the ones not cached."""
    st = _session(b)
    with st.lock:
        now = time.monotonic()
        want = [r["_obj"] for r in rows if (hit := st.disks.get(r["_obj"]._moId)) is None
                or now - hit[0] > DISK_SECONDS]
        if want:
            si = _connect(b)
            content = si.RetrieveContent()
            pc = vmodl.query.PropertyCollector
            spec = pc.FilterSpec(objectSet=[pc.ObjectSpec(obj=o, skip=False) for o in want],
                                 propSet=[pc.PropertySpec(type=vim.VirtualMachine,
                                                          pathSet=["config.hardware.device"], all=False)])
            for o in want:
                st.disks[o._moId] = (now, None)
            result = content.propertyCollector.RetrievePropertiesEx([spec], pc.RetrieveOptions())
            while result:
                for obj in result.objects:
                    devices = next((p.val for p in obj.propSet if p.name == "config.hardware.device"), None)
                    st.disks[obj.obj._moId] = (now, first_disk_gb(devices))
                if not result.token:
                    break
                result = content.propertyCollector.ContinueRetrievePropertiesEx(result.token)
        return {k: v[1] for k, v in st.disks.items()}


def templates(b) -> list[VMInfo]:
    """Templates matching the configured prefixes, newest first, with their disk sizes."""
    from ..images import template_prefixes
    prefixes = tuple(p + "-" for p in template_prefixes())
    rows = [r for r in _rows(b) if r.get("config.template") and str(r.get("name", "")).startswith(prefixes)]
    try:
        disks = _template_disks(b, rows)
    except Exception:  # noqa: BLE001 - the sizes are a convenience; the list still works without them
        log.warning("Could not read the template disk sizes on %s", b.name, exc_info=True)
        disks = {}
    ts = []
    for r in rows:
        v = _to_info(r)
        v.disk_gb = disks.get(r["_obj"]._moId)
        ts.append(v)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(ts, key=lambda t: t.created or epoch, reverse=True)


def vms_by_name(b, fresh: bool = False) -> dict[str, VMInfo]:
    return {v.name: v for v in all_vms(b, fresh) if not v.is_template}


def used_ips(b) -> set[str]:
    """Every IPv4 address vCenter knows a VM is using."""
    return {ip for v in all_vms(b) for ip in v.ips + ([v.ip] if v.ip else [])}


def name_exists(b, name: str) -> bool:
    return any(v.name == name for v in all_vms(b, fresh=True))


def _wait(task, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
        if time.monotonic() > deadline:
            raise TimeoutError("vCenter task timed out")
        time.sleep(1)
    if task.info.state == vim.TaskInfo.State.error:
        raise RuntimeError(task.info.error.msg)


def power(b, name: str, action: str, vm_id: str | None = None) -> None:
    """action: on, off (hard), shutdown (guest), reboot (guest)."""
    st = _session(b)
    with st.lock:
        vm = _find_vm_locked(b, name, vm_id)
        if action == "on":
            _wait(vm.PowerOnVM_Task())
        elif action == "off":
            _wait(vm.PowerOffVM_Task())
        elif action == "shutdown":
            vm.ShutdownGuest()
        elif action == "reboot":
            vm.RebootGuest()
        else:
            raise ValueError(f"unknown power action {action!r}")
        st.rows = None


def _host_of(vm):
    host = vm.runtime.host
    if host is None:
        raise LookupError(f"VM {vm.name!r} has no host")
    return host


def _autostart_locked(vm) -> Autostart:
    host = _host_of(vm)
    cfg = host.configManager.autoStartManager.config
    entry = next((p for p in (cfg.powerInfo or []) if p.key._moId == vm._moId), None)
    return Autostart(enabled=entry is not None and entry.startAction == "powerOn", host=host.name,
                     host_enabled=bool(cfg.defaults and cfg.defaults.enabled))


def autostart_spec(vm, enabled: bool):
    """One entry for this VM, in any start order, with the host's default delays; the host's
    own settings and the other VMs' entries are left as they are."""
    info = vim.host.AutoStartManager.AutoPowerInfo(
        key=vm, startOrder=-1, startDelay=-1, waitForHeartbeat="systemDefault",
        startAction="powerOn" if enabled else "none", stopDelay=-1, stopAction="systemDefault")
    return vim.host.AutoStartManager.Config(powerInfo=[info])


def autostart(b, name: str, vm_id: str | None = None) -> Autostart:
    with _session(b).lock:
        return _autostart_locked(_find_vm_locked(b, name, vm_id))


def set_autostart(b, name: str, vm_id: str | None, enabled: bool) -> Autostart:
    """Turn the VM's autostart entry on or off on the host it's on now; returns the result."""
    with _session(b).lock:
        vm = _find_vm_locked(b, name, vm_id)
        _host_of(vm).configManager.autoStartManager.ReconfigureAutostart(autostart_spec(vm, enabled))
        return _autostart_locked(vm)


def delete_template(b, name: str) -> None:
    st = _session(b)
    with st.lock:
        vm = _find_locked(b, name)
        if not vm.config.template:
            raise ValueError(f"{name!r} is not a template")
        _wait(vm.Destroy_Task())
        st.rows = None


def _find_locked(b, name: str):
    for row in _collect(b, ["name"]):
        if row.get("name") == name:
            return row["_obj"]
    raise LookupError(f"VM {name!r} not found in vCenter")


def _find_vm_locked(b, name: str, moid: str | None):
    """The portal's VM: by its managed object id when known, so another VM that has (or
    later gets) the same name anywhere in vCenter is never the one acted on."""
    if moid:
        vm = vim.VirtualMachine(moid, _connect(b)._stub)
        try:
            ok = vm.name == name and not vm.config.template
        except vmodl.fault.ManagedObjectNotFound:
            ok = False
        if not ok:
            raise LookupError(f"VM {name!r} is no longer in vCenter")
        return vm
    # No id recorded (a VM from before Terraform state): only an unambiguous match.
    matches = [r["_obj"] for r in _collect(b, ["name", "config.template"])
               if r.get("name") == name and not r.get("config.template")]
    if len(matches) != 1:
        raise LookupError(f"VM {name!r} not found in vCenter" if not matches else
                          f"More than one VM in vCenter is named {name!r}; the portal won't guess which")
    return matches[0]


def vm_facts(b, moid: str) -> dict | None:
    """Where a VM really is, by its id (None when vCenter has no such VM): its name,
    whether it's a template, its notes, datacenter and folder (relative to the
    datacenter's VM folder, as in Settings)."""
    with _session(b).lock:
        vm = vim.VirtualMachine(moid, _connect(b)._stub)
        try:
            facts = {"name": vm.name, "template": bool(vm.config.template),
                     "annotation": vm.config.annotation or ""}
            parts, node = [], vm.parent
            while node is not None and not isinstance(node, vim.Datacenter):
                parts.append(node.name)
                node = node.parent
            facts["datacenter"] = node.name if node is not None else ""
        except vmodl.fault.ManagedObjectNotFound:
            return None
    # parts ends with the datacenter's own VM folder ("vm"), which Settings leave out.
    facts["folder"] = "/".join(reversed(parts[:-1]))
    return facts


# --- inventory (settings dropdowns) ------------------------------------------

_inv_cache: dict[str, tuple[float, dict]] = {}
INVENTORY_SECONDS = 60


def _datacenters(folder) -> list:
    out = []
    for e in folder.childEntity:
        if isinstance(e, vim.Datacenter):
            out.append(e)
        elif isinstance(e, vim.Folder):
            out.extend(_datacenters(e))
    return out


def _vm_folders(content, dc) -> list[str]:
    """VM folder paths relative to the datacenter's VM folder, as Terraform expects."""
    view = content.viewManager.CreateContainerView(dc.vmFolder, [vim.Folder], True)
    try:
        paths = []
        for f in view.view:
            parts, node = [], f
            while node is not None and node != dc.vmFolder:
                parts.append(node.name)
                node = node.parent
            path = "/".join(reversed(parts))
            if path.split("/")[0] != "vCLS":  # vSphere Cluster Services' own folder
                paths.append(path)
        return sorted(paths, key=str.lower)
    finally:
        view.Destroy()


def _cluster(c) -> dict:
    datastores = []
    for d in c.datastore:
        s = d.summary
        if s.accessible:
            datastores.append({"name": s.name, "type": s.type,
                               "free_gb": s.freeSpace // 2**30, "capacity_gb": s.capacity // 2**30})
    networks = set()
    for n in c.network:
        if isinstance(n, vim.dvs.DistributedVirtualPortgroup) and n.config.uplink:
            continue  # uplink port groups can't host VMs
        networks.add(n.name)
    return {"name": c.name,
            "datastores": sorted(datastores, key=lambda d: d["name"].lower()),
            "networks": sorted(networks, key=str.lower)}


def inventory(server: str, user: str, password: str, insecure: bool, fresh: bool = False) -> dict:
    """Datacenters with their clusters (datastores, networks) and VM folders.

    Uses its own short-lived session, so it works with credentials that aren't
    saved yet. Cached briefly per credential set (the password only as a hash).
    """
    key = hashlib.sha256("\0".join([server, user, password, str(insecure)]).encode()).hexdigest()
    hit = _inv_cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < INVENTORY_SECONDS:
        return hit[1]
    si = _smart_connect(server, user, password, insecure)
    try:
        content = si.RetrieveContent()
        dcs = []
        for dc in _datacenters(content.rootFolder):
            view = content.viewManager.CreateContainerView(dc.hostFolder, [vim.ClusterComputeResource], True)
            try:
                clusters = [_cluster(c) for c in view.view]
            finally:
                view.Destroy()
            dcs.append({"name": dc.name, "clusters": sorted(clusters, key=lambda c: c["name"].lower()),
                        "folders": _vm_folders(content, dc)})
        result = {"about": content.about.fullName, "datacenters": sorted(dcs, key=lambda d: d["name"].lower())}
    finally:
        Disconnect(si)
    if len(_inv_cache) > 20:
        _inv_cache.clear()
    _inv_cache[key] = (time.monotonic(), result)
    return result


def list_isos(b) -> list[str]:
    """ISO files on the backend's cluster's datastores, as "[datastore] path/file.iso"."""
    c, st = b.cfg, _session(b)
    scope = (c.vsphere_datacenter, c.vsphere_cluster)
    if st.isos and st.isos[1] == scope and time.monotonic() - st.isos[0] < 300:
        return st.isos[2]
    with st.lock:
        content = _connect(b).RetrieveContent()
        dc = next((d for d in _datacenters(content.rootFolder) if d.name == c.vsphere_datacenter), None)
        if dc is None:
            raise LookupError(f"Datacenter {c.vsphere_datacenter!r} not found")
        view = content.viewManager.CreateContainerView(dc.hostFolder, [vim.ClusterComputeResource], True)
        try:
            cluster = next((x for x in view.view if x.name == c.vsphere_cluster), None)
        finally:
            view.Destroy()
        stores = cluster.datastore if cluster else dc.datastore
        isos = []
        for ds in stores:
            if not ds.summary.accessible:
                continue
            spec = vim.host.DatastoreBrowser.SearchSpec(matchPattern=["*.iso"])
            task = ds.browser.SearchDatastoreSubFolders_Task(f"[{ds.name}]", spec)
            _wait(task, timeout=60)
            for r in task.info.result or []:
                folder = r.folderPath.rstrip("/")
                if folder.endswith("packer_cache"):
                    continue  # Packer's temporary cloud-init ISOs
                for f in r.file or []:
                    isos.append(f"{folder} {f.path}" if folder.endswith("]") else f"{folder}/{f.path}")
    isos.sort(key=str.lower)
    st.isos = (time.monotonic(), scope, isos)
    return isos


def iso_name(iso: str) -> str:
    """The file name of an ISO as list_isos gives it, to match an image's ISO pattern."""
    return iso.split("]")[-1].strip().split("/")[-1]


# --- console ------------------------------------------------------------------

def console_ticket(b, name: str, vm_id: str | None = None) -> ConsoleTicket:
    """A short-lived WebMKS (VNC over WebSocket) ticket for the VM's ESXi host."""
    with _session(b).lock:
        vm = _find_vm_locked(b, name, vm_id)
        if vm.runtime.powerState != "poweredOn":
            raise RuntimeError("The VM is not powered on")
        t = vm.AcquireTicket("webmks")
    url = urlsplit(t.url or "")
    return ConsoleTicket(host=t.host or url.hostname, port=t.port or url.port or 443,
                         path=url.path or f"/ticket/{t.ticket}", thumbprint=t.sslThumbprint or "")


# --- OpenTofu (terraform/vm, the vmware/vsphere provider) ---------------------------------

TF_RESOURCE = ("vsphere_virtual_machine", "this")
TF_PROVIDER = "registry.opentofu.org/vmware/vsphere"
TF_LEGACY_PROVIDER = "registry.terraform.io/vmware/vsphere"   # in state written by Terraform
# Connection settings always come from the backend's current settings (tf_env), so a changed
# vCenter or password never leaves a VM planning with the old connection. tfvars.json beats
# TF_VAR_*, so they must not be in it.
TF_CONNECTION_VARS = ("vsphere_server", "vsphere_user", "vsphere_insecure")
TF_SECRET_VARS = ("vsphere_password",)
TF_PLACEMENT_VARS = ("datacenter", "cluster", "datastore", "network", "folder")
TF_FACTS = ("guest_id", "scsi_type", "firmware", "adapter_type", "uuid")
# Attributes a resize may change. Anything else in the plan (a manual change in vCenter
# that the plan would revert, a replacement) stops the job before it touches the VM.
TF_RESIZABLE = {("num_cpus",), ("memory",), ("disk", 0, "size")}
# Attributes the provider recomputes on any update; they may show as unknown.
TF_RECOMPUTED = {"change_version", "reboot_required", "imported", "default_ip_address", "guest_ip_addresses",
                 "vmx_path", "moid", "power_state", "uuid", "vapp_transport"}
# Attributes that may go from unset in older state to the provider's default.
TF_DEFAULTED = {"swap_placement_policy": "inherit"}
# No variables beyond the common and placement ones.
TF_EXTRA_VARS: dict[str, type] = {}


def tf_module_dir() -> Path:
    return get_settings().terraform_module_dir


def tf_env(b) -> dict[str, str]:
    c = b.cfg
    return {
        "TF_VAR_vsphere_server": c.vsphere_server,
        "TF_VAR_vsphere_user": c.vsphere_user,
        "TF_VAR_vsphere_password": c.vsphere_password,
        "TF_VAR_vsphere_insecure": "true" if c.vsphere_insecure else "false",
    }


def locations(b) -> list[str]:
    return []


async def before_change(b, d, log) -> None:
    return None     # the provider finds a VM by its id wherever vMotion took it


def tf_placement(b, location: str | None = None) -> dict[str, str]:
    c = b.cfg
    return {
        "datacenter": c.vsphere_datacenter,
        "cluster": c.vsphere_cluster,
        "datastore": c.vsphere_datastore,
        "network": c.vsphere_network,
        "folder": c.vsphere_folder,
    }


def tf_template_facts(values: dict | None) -> dict | None:
    try:
        return {"guest_id": values["guest_id"], "scsi_type": values["scsi_type"], "firmware": values["firmware"],
                "adapter_type": values["network_interface"][0]["adapter_type"],
                "uuid": values["clone"][0]["template_uuid"]}
    except (TypeError, KeyError, IndexError):
        return None


def tf_size(values: dict | None) -> dict | None:
    try:
        return {"cpus": int(values["num_cpus"]), "memory_mb": int(values["memory"]),
                "disk_gb": int(values["disk"][0]["size"])}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def state_vm_id(state: dict) -> str | None:
    """The VM's managed object id (moid) in its OpenTofu state."""
    for r in state.get("resources") or []:
        if (r.get("type"), r.get("name")) == TF_RESOURCE and r.get("mode") != "data":
            for inst in r.get("instances") or []:
                moid = (inst.get("attributes") or {}).get("moid")
                if isinstance(moid, str) and moid:
                    return moid
    return None


# What a create needs beyond OpenTofu: nothing on vSphere (cloud-init travels in guestinfo).
async def before_create(b, req, d: Path, log) -> dict:
    return {}


async def after_create(b, req, vm_id: str | None, log) -> None:
    return None


async def after_destroy(b, name: str, vm_id: str | None, log) -> None:
    return None



# --- Packer (vsphere-iso) -----------------------------------------------------------------

def packer_vars(b) -> dict[str, str]:
    c = b.cfg
    return {
        "PKR_VAR_vsphere_server": c.vsphere_server,
        "PKR_VAR_vsphere_user": c.vsphere_user,
        "PKR_VAR_vsphere_password": c.vsphere_password,
        "PKR_VAR_vsphere_insecure": "true" if c.vsphere_insecure else "false",
        "PKR_VAR_vsphere_datacenter": c.vsphere_datacenter,
        "PKR_VAR_vsphere_cluster": c.vsphere_cluster,
        "PKR_VAR_vsphere_datastore": c.vsphere_datastore,
        "PKR_VAR_vsphere_network": c.vsphere_network,
        "PKR_VAR_vsphere_folder": c.vsphere_folder,
    }


def api_host(b) -> str:
    server = b.cfg.vsphere_server
    return server.rsplit(":", 1)[0] if server.count(":") == 1 else server


# --- backups ------------------------------------------------------------------------------

VM_ID_RE = re.compile(r"^vm-[0-9]{1,12}$")
# What the portal's module leaves in state beside the VM, and nothing else.
STATE_DATA = {("vsphere_datacenter", "this"), ("vsphere_compute_cluster", "this"), ("vsphere_datastore", "this"),
              ("vsphere_network", "this"), ("vsphere_virtual_machine", "template")}


def import_problems(b, vm_id: str, name: str, tfvars: dict) -> list[str]:
    """Confirm the id belongs to that very VM, inside this backend's datacenter (and folder),
    and that vm-portal created it. Otherwise importing the state would hand the portal, and
    a later destroy, some other VM."""
    c = b.cfg
    try:
        facts = vm_facts(b, vm_id)
    except Exception as e:  # noqa: BLE001 - shown in the preview
        return [f"Couldn't check vCenter: {e}"]
    if facts is None:
        return [f"vCenter has no VM with id {vm_id}"]
    problems = []
    if facts["name"] != name:
        problems.append(f"In vCenter, {vm_id} is a VM named {facts['name']!r}")
    if facts["template"]:
        problems.append(f"{vm_id} is a template")
    if facts["datacenter"] != c.vsphere_datacenter:
        problems.append(f"It's in datacenter {facts['datacenter']!r}, not the backend's {c.vsphere_datacenter!r}")
    folder = c.vsphere_folder.strip("/")
    if folder and facts["folder"] != folder and not facts["folder"].startswith(folder + "/"):
        problems.append(f"It's in folder {facts['folder'] or '(root)'!r}, outside the backend's {folder!r}")
    if not facts["annotation"].startswith(CREATED_BY):
        problems.append("vm-portal didn't create it (its vCenter notes don't say so)")
    if tfvars.get("datacenter") not in (None, c.vsphere_datacenter):
        problems.append("Its variables name another datacenter than the backend's")
    return problems
