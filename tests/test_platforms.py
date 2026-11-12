"""The platform interface: what base.Platform lists is what every platform module provides,
and the rest of the portal reaches a platform only through a backend."""
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import platforms
from app.config import BackendFields
from app.platforms import base

APP = Path(platforms.__file__).resolve().parents[1]
MEMBERS = {**{n: None for n in base.Platform.__annotations__},
           **{n: f for n, f in vars(base.Platform).items() if inspect.isfunction(f)}}


def backend(kind: str):
    """A configured backend of this type, without the database."""
    cfg = {"vsphere": dict(vsphere_server="vc.example", vsphere_user="u", vsphere_password="pw",
                           vsphere_datacenter="DC", vsphere_cluster="C", vsphere_datastore="DS",
                           vsphere_network="N"),
           "proxmox": dict(proxmox_server="pve.example", proxmox_token_id="portal@pve!t", proxmox_token_secret="s",
                           proxmox_node="pve1", proxmox_storage="local-lvm", proxmox_iso_storage="local",
                           proxmox_bridge="vmbr0")}[kind]
    p = platforms.PLATFORMS[kind]
    return SimpleNamespace(id=7, name="b", type=kind, cfg=BackendFields(**cfg), key="7:x", configured=True,
                           platform=p)


def test_registry():
    assert set(platforms.PLATFORMS) == {"vsphere", "proxmox"}
    assert all(name == p.NAME for name, p in platforms.PLATFORMS.items())


@pytest.mark.parametrize("platform", platforms.PLATFORMS.values(), ids=lambda p: p.NAME)
def test_platform_provides_everything(platform, monkeypatch):
    for name, spec in MEMBERS.items():
        assert hasattr(platform, name), f"{platform.NAME} has no {name}"
        if spec is None or name == "inventory":   # each platform's settings page has its own connection args
            continue
        want = list(inspect.signature(spec).parameters)[1:]   # without self
        got = list(inspect.signature(getattr(platform, name)).parameters)
        assert got == want, f"{platform.NAME}.{name}{tuple(got)} should take {tuple(want)}"
    # The values that depend on each other agree.
    assert set(platform.REQUIRED) <= set(platform.CONNECTION) | set(platform.PLACEMENT)
    assert {platform.SERVER_FIELD, platform.SECRET_FIELD} <= set(platform.CONNECTION)
    assert set(platform.CONNECTION) | set(platform.PLACEMENT) <= set(BackendFields.model_fields)
    b = backend(platform.NAME)
    if platform.NAME == "proxmox":
        monkeypatch.setattr(platform, "ca_file", lambda b: None)
    assert set(platform.tf_placement(b)) == set(platform.TF_PLACEMENT_VARS)
    env = platform.tf_env(b)
    assert {f"TF_VAR_{v}" for v in (*platform.TF_CONNECTION_VARS, *platform.TF_SECRET_VARS)} == \
        {k for k in env if k.startswith("TF_VAR_")}
    assert not set(platform.TF_PLACEMENT_VARS) & {*platform.TF_CONNECTION_VARS, *platform.TF_SECRET_VARS}
    assert not set(platform.TF_EXTRA_VARS) & {*platform.TF_PLACEMENT_VARS, *platform.TF_CONNECTION_VARS}
    assert platform.tf_module_dir().is_dir()
    variables = (platform.tf_module_dir() / "variables.tf").read_text()
    for v in (*platform.TF_CONNECTION_VARS, *platform.TF_SECRET_VARS, *platform.TF_PLACEMENT_VARS,
              *platform.TF_EXTRA_VARS, "name", "template", "cpus", "memory_mb", "disk_gb", "metadata", "userdata",
              "annotation"):
        assert f'variable "{v}"' in variables, f"{platform.NAME}'s module has no variable {v}"
    assert f'resource "{platform.TF_RESOURCE[0]}" "{platform.TF_RESOURCE[1]}"' in \
        (platform.tf_module_dir() / "main.tf").read_text()
    assert all(k.startswith("PKR_VAR_") or k == "SSL_CERT_FILE" for k in platform.packer_vars(b))


def test_vsphere_reads_its_own_state():
    vs = platforms.PLATFORMS["vsphere"]
    values = {"num_cpus": 2, "memory": 4096, "guest_id": "g", "scsi_type": "pvscsi", "firmware": "efi",
              "network_interface": [{"adapter_type": "vmxnet3"}], "clone": [{"template_uuid": "u"}],
              "disk": [{"size": 40}]}
    assert vs.tf_size(values) == {"cpus": 2, "memory_mb": 4096, "disk_gb": 40}
    assert set(vs.tf_template_facts(values)) == set(vs.TF_FACTS)
    assert vs.tf_size(None) is None and vs.tf_template_facts({}) is None
    state = {"resources": [
        {"mode": "data", "type": "vsphere_virtual_machine", "name": "template", "instances": [{"attributes": {"moid": "vm-1"}}]},
        {"mode": "managed", "type": "vsphere_virtual_machine", "name": "this", "instances": [{"attributes": {"moid": "vm-42"}}]}]}
    assert vs.state_vm_id(state) == "vm-42" and vs.state_vm_id({}) is None
    assert vs.VM_ID_RE.match("vm-42") and not vs.VM_ID_RE.match("vm-42; rm -rf")


def test_proxmox_reads_its_own_state():
    pve = platforms.PLATFORMS["proxmox"]
    values = {"cpu": [{"cores": 4}], "memory": [{"dedicated": 8192}], "disk": [{"size": 60}]}
    assert pve.tf_size(values) == {"cpus": 4, "memory_mb": 8192, "disk_gb": 60}
    state = {"resources": [{"mode": "managed", "type": "proxmox_virtual_environment_vm", "name": "this",
                            "provider": f'provider["{pve.TF_PROVIDER}"]', "instances": [{"attributes": {"vm_id": 104}}]}]}
    assert pve.state_vm_id(state) == "104" and platforms.for_state(state) is pve
    assert pve.VM_ID_RE.match("104") and not pve.VM_ID_RE.match("vm-42")


def test_state_tells_its_platform():
    vs, pve = platforms.PLATFORMS["vsphere"], platforms.PLATFORMS["proxmox"]
    assert platforms.for_state({"resources": [{"provider": f'provider["{vs.TF_LEGACY_PROVIDER}"]'}]}) is vs
    assert platforms.for_state({"resources": [{"type": pve.TF_RESOURCE[0]}]}) is pve
    assert platforms.for_state({"resources": [{"provider": 'provider["registry.example/evil/x"]'}]}) is None


def test_only_the_platform_package_talks_to_platforms():
    """Everything else goes through a backend (b.platform.<call>(b, ...)), never a platform
    module by name; and pyVmomi stays inside the vSphere module."""
    direct = re.compile(r"\b(pyVmomi|pyVim)\b|\b(vsphere|proxmox)\.[a-z_]+\(|\b(vsphere|proxmox)\.[a-z_]+,"
                        r"|from \.platforms import (?!PLATFORMS|for_state)")
    found = {}
    for f in APP.rglob("*.py"):
        if "platforms" in f.relative_to(APP).parts:
            continue
        hits = [line.strip() for line in f.read_text().splitlines() if direct.search(line)]
        if hits:
            found[f.name] = hits
    assert found == {}, found


def test_vsphere_never_offers_a_link_local_address():
    from app.platforms import vsphere
    nic = SimpleNamespace(ipAddress=["fe80::250:56ff:fe00:1", "169.254.10.1", "10.0.0.31"])
    row = {"name": "deb-01", "guest.ipAddress": "fe80::250:56ff:fe00:1", "guest.net": [nic]}
    info = vsphere._to_info(row)
    assert info.ip == "10.0.0.31" and info.ips == ["10.0.0.31"]
    assert vsphere._to_info({**row, "guest.net": []}).ip is None          # no IPv4 yet: nothing, not fe80
    assert vsphere._to_info({**row, "guest.ipAddress": "10.0.0.40"}).ip == "10.0.0.40"
    assert base.usable_ipv4(" 10.1.2.3 ") == "10.1.2.3" and base.usable_ipv4("127.0.0.1") is None


def test_vsphere_template_disk_size():
    from pyVmomi import vim
    from app.platforms import vsphere

    def disk(key, unit, size):
        return vim.vm.device.VirtualDisk(controllerKey=key, unitNumber=unit, capacityInBytes=size)
    gib = 1024 ** 3
    devices = [vim.vm.device.VirtualCdrom(), disk(1000, 1, 100 * gib), disk(1000, 0, 25 * gib)]
    assert vsphere.first_disk_gb(devices) == 25                         # the first disk, not the biggest
    assert vsphere.first_disk_gb([disk(1000, 0, 25 * gib + 1)]) == 26    # rounded up: never below it
    assert vsphere.first_disk_gb([vim.vm.device.VirtualCdrom()]) is None and vsphere.first_disk_gb(None) is None
