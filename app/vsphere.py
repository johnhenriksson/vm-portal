"""Read-only vCenter queries and power operations (pyVmomi).

Provisioning itself goes through Terraform; this module is for listing templates,
showing VM state, and powering VMs on/off. All functions are blocking: call them
with run_in_threadpool / asyncio.to_thread.
"""
import hashlib
import ssl
from urllib.parse import urlsplit
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .config import get_settings, vcenter_configured

_lock = threading.Lock()
_si = None

PROPS = [
    "name",
    "config.template",
    "config.createDate",
    "config.annotation",
    "runtime.powerState",
    "guest.ipAddress",
    "guest.net",
    "guest.toolsRunningStatus",
]


@dataclass
class VMInfo:
    name: str
    is_template: bool
    power_state: str = ""
    ip: str | None = None
    ips: list[str] = field(default_factory=list)
    created: datetime | None = None
    notes: str = ""
    tools_running: bool = False

    @property
    def age_days(self) -> int | None:
        if not self.created:
            return None
        return (datetime.now(timezone.utc) - self.created).days


def _connect():
    global _si
    s = get_settings()
    if _si is not None:
        try:
            _si.CurrentTime()
            return _si
        except Exception:  # noqa: BLE001 - session expired or network blip; reconnect
            _si = None
    if not vcenter_configured(s):
        raise RuntimeError("vCenter is not configured yet (Settings → vCenter)")
    _si = _smart_connect(s.vsphere_server, s.vsphere_user, s.vsphere_password, s.vsphere_insecure)
    return _si


def _smart_connect(server: str, user: str, password: str, insecure: bool):
    host, _, port = server.partition(":")
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    return SmartConnect(host=host, port=int(port or 443), user=user, pwd=password, sslContext=ctx,
                        httpConnectionTimeout=15)


def test_connection(server: str, user: str, password: str, insecure: bool) -> str:
    """Log in with the given credentials and describe what we found. Raises on failure."""
    si = _smart_connect(server, user, password, insecure)
    try:
        content = si.RetrieveContent()
        dcs = [e.name for e in content.rootFolder.childEntity if isinstance(e, vim.Datacenter)]
        return f"Connected to {content.about.fullName}. Datacenters: {', '.join(dcs) or 'none'}"
    finally:
        Disconnect(si)


def close() -> None:
    global _si
    _invalidate()
    with _lock:
        if _si is not None:
            try:
                Disconnect(_si)
            finally:
                _si = None


def _collect(props: list[str]) -> list[dict]:
    """Fetch properties of all VMs in one round trip via the PropertyCollector."""
    si = _connect()
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
        ips.extend(a for a in (nic.ipAddress or []) if ":" not in a)  # IPv4 only
    created = row.get("config.createDate")
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return VMInfo(
        name=row.get("name", ""),
        is_template=bool(row.get("config.template")),
        power_state=str(row.get("runtime.powerState") or ""),
        ip=row.get("guest.ipAddress"),
        ips=ips,
        created=created,
        notes=row.get("config.annotation") or "",
        tools_running=row.get("guest.toolsRunningStatus") == "guestToolsRunning",
    )


_cache: tuple[float, list[dict]] | None = None
CACHE_SECONDS = 5


def _rows(fresh: bool = False) -> list[dict]:
    global _cache
    with _lock:
        if not fresh and _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        rows = _collect(PROPS)
        _cache = (time.monotonic(), rows)
        return rows


def all_vms(fresh: bool = False) -> list[VMInfo]:
    return [_to_info(r) for r in _rows(fresh)]


def templates() -> list[VMInfo]:
    """Templates matching the configured prefixes, newest first."""
    from .images import template_prefixes
    prefixes = tuple(p + "-" for p in template_prefixes())
    ts = [v for v in all_vms() if v.is_template and v.name.startswith(prefixes)]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(ts, key=lambda t: t.created or epoch, reverse=True)


def vms_by_name(fresh: bool = False) -> dict[str, VMInfo]:
    return {v.name: v for v in all_vms(fresh) if not v.is_template}


def used_ips() -> set[str]:
    """Every IPv4 address vCenter knows a VM is using."""
    return {ip for v in all_vms() for ip in v.ips + ([v.ip] if v.ip else [])}


def name_exists(name: str) -> bool:
    return any(v.name == name for v in all_vms(fresh=True))


def _wait(task, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
        if time.monotonic() > deadline:
            raise TimeoutError("vCenter task timed out")
        time.sleep(1)
    if task.info.state == vim.TaskInfo.State.error:
        raise RuntimeError(task.info.error.msg)


def power(name: str, action: str) -> None:
    """action: on, off (hard), shutdown (guest), reboot (guest)."""
    with _lock:
        vm = _find_locked(name)
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
    _invalidate()


def delete_template(name: str) -> None:
    with _lock:
        vm = _find_locked(name)
        if not vm.config.template:
            raise ValueError(f"{name!r} is not a template")
        _wait(vm.Destroy_Task())
    _invalidate()


def _find_locked(name: str):
    for row in _collect(["name"]):
        if row.get("name") == name:
            return row["_obj"]
    raise LookupError(f"VM {name!r} not found in vCenter")


def _invalidate() -> None:
    global _cache
    _cache = None


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


_iso_cache: tuple[float, tuple, list[str]] | None = None


def list_isos() -> list[str]:
    """ISO files on the configured cluster's datastores, as "[datastore] path/file.iso"."""
    global _iso_cache
    s = get_settings()
    scope = (s.vsphere_server, s.vsphere_datacenter, s.vsphere_cluster)
    if _iso_cache and _iso_cache[1] == scope and time.monotonic() - _iso_cache[0] < 300:
        return _iso_cache[2]
    with _lock:
        content = _connect().RetrieveContent()
        dc = next((d for d in _datacenters(content.rootFolder) if d.name == s.vsphere_datacenter), None)
        if dc is None:
            raise LookupError(f"Datacenter {s.vsphere_datacenter!r} not found")
        view = content.viewManager.CreateContainerView(dc.hostFolder, [vim.ClusterComputeResource], True)
        try:
            cluster = next((c for c in view.view if c.name == s.vsphere_cluster), None)
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
    _iso_cache = (time.monotonic(), scope, isos)
    return isos


# --- console ------------------------------------------------------------------

@dataclass
class ConsoleTicket:
    host: str
    port: int
    path: str          # contains the one-time ticket; never sent to the browser
    thumbprint: str    # ESXi certificate thumbprint as reported by vCenter


def console_ticket(name: str) -> ConsoleTicket:
    """A short-lived WebMKS (VNC over WebSocket) ticket for the VM's ESXi host."""
    with _lock:
        vm = _find_locked(name)
        if vm.runtime.powerState != "poweredOn":
            raise RuntimeError("The VM is not powered on")
        t = vm.AcquireTicket("webmks")
    url = urlsplit(t.url or "")
    return ConsoleTicket(host=t.host or url.hostname, port=t.port or url.port or 443,
                         path=url.path or f"/ticket/{t.ticket}", thumbprint=t.sslThumbprint or "")
