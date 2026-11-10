"""Autostart: the VM's entry in its host's startup list, set on create and from the VM page."""
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from app import db, provision, users
from app.platforms import vsphere
from app.backup import checks

from backup_helpers import admin, make_vm
from tests.test_web import client, csrf, login


# --- vSphere ----------------------------------------------------------------------------

class FakeAutoStartManager:
    def __init__(self, entries=(), host_enabled=True):
        self.config = SimpleNamespace(defaults=SimpleNamespace(enabled=host_enabled), powerInfo=list(entries))
        self.specs = []

    def ReconfigureAutostart(self, spec):
        self.specs.append(spec)
        for new in spec.powerInfo:
            self.config.powerInfo = [p for p in self.config.powerInfo if p.key._moId != new.key._moId] + [new]


def entry(moid, action):
    return SimpleNamespace(key=SimpleNamespace(_moId=moid), startAction=action)


@pytest.fixture
def host(monkeypatch):
    vm = vim.VirtualMachine("vm-42")
    mgr = FakeAutoStartManager([entry("vm-7", "powerOn")])
    h = SimpleNamespace(name="esx1", configManager=SimpleNamespace(autoStartManager=mgr))
    monkeypatch.setattr(vsphere, "_find_vm_locked", lambda name, moid: vm)
    monkeypatch.setattr(vsphere, "_host_of", lambda v: h)
    return mgr


def test_spec_touches_only_this_vm():
    spec = vsphere.autostart_spec(vim.VirtualMachine("vm-42"), True)
    assert spec.defaults is None                      # the host's own settings are left alone
    [info] = spec.powerInfo
    assert info.key._moId == "vm-42" and info.startAction == "powerOn" and info.startOrder == -1
    assert info.stopAction == "systemDefault"
    assert vsphere.autostart_spec(vim.VirtualMachine("vm-42"), False).powerInfo[0].startAction == "none"


def test_read_and_set(host):
    assert vsphere.autostart("web-01", "vm-42") == vsphere.Autostart(False, "esx1", True)  # no entry yet
    assert vsphere.set_autostart("web-01", "vm-42", True).enabled
    assert vsphere.autostart("web-01", "vm-42").enabled
    assert not vsphere.set_autostart("web-01", "vm-42", False).enabled
    assert [p.key._moId for p in host.config.powerInfo] == ["vm-7", "vm-42"]  # other VMs untouched
    assert host.config.powerInfo[0].startAction == "powerOn"
    host.config.defaults.enabled = False
    assert not vsphere.autostart("web-01", "vm-42").host_enabled


# --- create -----------------------------------------------------------------------------

class Log:
    def __init__(self):
        self.text = ""

    def step(self, t):
        self.text += t + "\n"

    def write(self, t):
        self.text += t


async def test_create_job_only_warns_when_autostart_fails(monkeypatch):
    make_vm()

    def down(*a):
        raise RuntimeError("no permission")
    monkeypatch.setattr(vsphere, "set_autostart", down)
    log = Log()
    await provision.enable_autostart("web-01", log)
    assert "Warning: couldn't turn on autostart (no permission)" in log.text

    calls = []
    monkeypatch.setattr(vsphere, "set_autostart",
                        lambda *a: calls.append(a) or vsphere.Autostart(True, "esx1", False))
    log = Log()
    await provision.enable_autostart("web-01", log)
    assert calls == [("web-01", "vm-42", True)]       # by the VM's id from its state
    assert "esx1 has autostart turned off" in log.text


def test_create_form_is_ticked_by_default_and_is_stored(monkeypatch):
    monkeypatch.setattr(vsphere, "templates", lambda: [])
    with client() as c:
        admin()
        login(c, "admin2", "correct horse battery")
        assert 'name="autostart" value="on" checked' in c.get("/vms/new").text
    reqs = []
    monkeypatch.setattr(provision.jobs, "submit", lambda *a, **kw: 1)
    for on in (True, False):
        req = provision.VMRequest(name=f"vm{int(on)}-x", template="t", cpus=1, memory_mb=1024, disk_gb=10,
                                  autostart=on)
        provision.create(req)
        reqs.append(db.active_vm(req.name)["autostart"])
    assert reqs == [1, 0]


def test_existing_vms_are_not_managed():
    make_vm()
    assert db.active_vm("web-01")["autostart"] is None


# --- VM page and route ------------------------------------------------------------------

@pytest.fixture
def page(monkeypatch):
    make_vm()
    state = {"live": vsphere.Autostart(False, "esx1", True), "fail": None, "calls": []}
    monkeypatch.setattr(vsphere, "vms_by_name",
                        lambda fresh=False: {"web-01": vsphere.VMInfo("web-01", False, "poweredOn")})
    monkeypatch.setattr(vsphere, "templates", lambda: [])
    monkeypatch.setattr(vsphere, "autostart", lambda name, moid: state["live"])

    def set_(name, moid, on):
        state["calls"].append((name, moid, on))
        if state["fail"]:
            raise RuntimeError(state["fail"])
        state["live"] = vsphere.Autostart(on, "esx1", state["live"].host_enabled)
        return state["live"]
    monkeypatch.setattr(vsphere, "set_autostart", set_)
    return state


def test_route_turns_it_on_and_off_for_the_owner(page):
    with client() as c:
        login(c, "alice", "correct horse battery")
        r = c.get("/vms/web-01")
        assert "not managed by the portal" in r.text and 'action="/vms/web-01/autostart"' in r.text
        tok = csrf(c, "/vms/web-01")
        r = c.post("/vms/web-01/autostart", data={"enabled": "on", "csrf_token": tok}, follow_redirects=False)
        assert r.status_code == 303 and db.active_vm("web-01")["autostart"] == 1
        assert page["calls"] == [("web-01", "vm-42", True)]
        assert "not managed" not in c.get("/vms/web-01").text
        c.post("/vms/web-01/autostart", data={"csrf_token": tok})
        assert db.active_vm("web-01")["autostart"] == 0
    actions = [r["action"] for r in db.query("SELECT action FROM audit ORDER BY id")]
    assert actions[-2:] == ["vm.autostart.on", "vm.autostart.off"]


def test_route_checks(page, monkeypatch):
    with client() as c:
        users.create("bob", "correct horse battery", "user")
        login(c, "bob", "correct horse battery")
        tok = csrf(c, "/")
        assert c.post("/vms/web-01/autostart", data={"enabled": "on", "csrf_token": tok}).status_code == 404
        assert c.post("/vms/web-01/autostart", data={"enabled": "on"}).status_code == 403   # no CSRF token
        c.post("/logout", data={"csrf_token": tok})
        admin()
        login(c, "admin2", "correct horse battery")
        tok = csrf(c, "/")
        page["fail"] = "NoPermission: Host.Config.AutoStart"
        r = c.post("/vms/web-01/autostart", data={"enabled": "on", "csrf_token": tok})
        assert r.status_code == 502 and "Host.Config.AutoStart" in r.text
        assert db.active_vm("web-01")["autostart"] is None      # only stored once vCenter took it
        monkeypatch.setattr(provision.jobs, "is_locked", lambda key: True)
        assert c.post("/vms/web-01/autostart", data={"enabled": "on", "csrf_token": tok}).status_code == 409
        monkeypatch.setattr(provision.jobs, "is_locked", lambda key: False)
        db.execute("UPDATE vms SET status='failed'")
        assert c.post("/vms/web-01/autostart", data={"enabled": "on", "csrf_token": tok}).status_code == 409
    assert len(page["calls"]) == 1


def test_page_warns_about_drift_and_a_host_without_autostart(page):
    db.execute("UPDATE vms SET autostart=1")
    with client() as c:
        admin()
        login(c, "admin2", "correct horse battery")
        text = c.get("/vms/web-01").text
        assert "Autostart should be on, but host esx1 has it" in text and "Reapply" in text
        page["live"] = vsphere.Autostart(True, "esx1", False)
        text = c.get("/vms/web-01").text
        assert "Reapply" not in text and "Host esx1 has autostart turned off" in text
        page["live"] = vsphere.Autostart(True, "esx1", True)
        text = c.get("/vms/web-01").text
        assert "Reapply" not in text and "autostart turned off" not in text


# --- backups ----------------------------------------------------------------------------

def test_backup_rows_carry_it():
    row = {"name": "web-01", "status": "ready", "template": "t", "cpus": 2, "memory_mb": 4096, "disk_gb": 40,
           "ip_mode": "dhcp", "has_compose": 0, "created_at": "2026-11-01T00:00:00+00:00"}
    assert checks.VmRow.model_validate(row).autostart is None   # a backup from before the setting
    assert checks.VmRow.model_validate({**row, "autostart": 1}).autostart == 1
    with pytest.raises(ValueError):
        checks.VmRow.model_validate({**row, "autostart": 2})
