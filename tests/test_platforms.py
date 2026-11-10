"""The platform interface: what base.Platform lists is what every platform module provides,
and the rest of the portal reaches a platform only through it."""
import inspect
import re
from pathlib import Path

import pytest

from app import platforms
from app.platforms import base

APP = Path(platforms.__file__).resolve().parents[1]
MEMBERS = {**{n: None for n in base.Platform.__annotations__},
           **{n: f for n, f in vars(base.Platform).items() if inspect.isfunction(f)}}


def test_current_is_registered():
    assert platforms.current() is platforms.PLATFORMS["vsphere"]
    assert all(name == p.NAME for name, p in platforms.PLATFORMS.items())


@pytest.mark.parametrize("platform", platforms.PLATFORMS.values(), ids=lambda p: p.NAME)
def test_platform_provides_everything(platform):
    for name, spec in MEMBERS.items():
        assert hasattr(platform, name), f"{platform.NAME} has no {name}"
        if spec is None:
            continue
        want = list(inspect.signature(spec).parameters)[1:]   # without self
        got = list(inspect.signature(getattr(platform, name)).parameters)
        assert got == want, f"{platform.NAME}.{name}{tuple(got)} should take {tuple(want)}"
    # The values that depend on each other agree.
    assert set(platform.tf_placement()) == set(platform.TF_PLACEMENT_VARS)
    assert {f"TF_VAR_{v}" for v in (*platform.TF_CONNECTION_VARS, *platform.TF_SECRET_VARS)} == set(platform.tf_env())
    assert not set(platform.TF_PLACEMENT_VARS) & {*platform.TF_CONNECTION_VARS, *platform.TF_SECRET_VARS}
    assert platform.tf_module_dir().is_dir()


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


def test_only_the_platform_package_talks_to_vsphere():
    """Everything else goes through platforms.current(). The one exception is the Settings →
    vCenter page (admin.py), which is vSphere's own settings page and uses its inventory."""
    direct = re.compile(r"\b(pyVmomi|pyVim)\b|\bvsphere\.[a-z_]+\(|\bvsphere\.[a-z_]+,")
    found = {}
    for f in APP.rglob("*.py"):
        if "platforms" in f.relative_to(APP).parts:
            continue
        hits = [line.strip() for line in f.read_text().splitlines() if direct.search(line)]
        if hits:
            found[f.name] = hits
    assert set(found) == {"admin.py"} and all("vsphere.inventory" in h for h in found["admin.py"]), found
