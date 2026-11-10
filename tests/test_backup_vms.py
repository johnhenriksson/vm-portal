import json

import pytest

from app import db, provision, users
from app.backup import archive, checks, collect, restore
from app.backup.archive import BackupError

from backup_helpers import PROVIDER, admin, good_facts, make_vm, state, tfvars  # noqa: F401


@pytest.fixture
def vcenter(monkeypatch):
    facts = {"vm-42": good_facts()}
    from app import vsphere
    monkeypatch.setattr(vsphere, "vm_facts", lambda moid: facts.get(moid))
    return facts


def export(names=("web-01",)):
    files, meta = collect.vms(list(names), by="admin2")
    return archive.unpack(archive.pack("vms", files, **meta))


def test_export_has_only_the_chosen_vms_state_and_no_ids():
    make_vm()
    make_vm("db-01", "vm-43", ip="10.0.0.12")
    b = export()
    assert set(b.files) == {"db/vms.json", "vms/web-01/terraform.tfstate", "vms/web-01/terraform.tfvars.json"}
    row = b.json("db/vms.json")[0]
    assert row["owner_username"] == "alice" and row["owner_id"] is None and row["last_job_id"] is None
    assert b.manifest["vms"] == ["web-01"] and b.manifest["installation_id"] == collect.installation_id()
    with pytest.raises(ValueError, match="No VM"):
        collect.vms(["nope"], by="x")


def test_export_waits_for_running_jobs(monkeypatch):
    make_vm()
    monkeypatch.setattr(collect, "busy_vms", lambda: ["web-01"])
    with pytest.raises(ValueError, match="running job"):
        collect.vms(["web-01"], by="x")


@pytest.mark.parametrize("bad, match", [
    (state(extra=[{"mode": "managed", "type": "vsphere_virtual_machine", "name": "other", "provider": PROVIDER,
                   "instances": [{"attributes": {"name": "x", "moid": "vm-1"}}]}]), "didn't create"),
    (state(extra=[{"mode": "managed", "type": "null_resource", "name": "x", "provider": PROVIDER,
                   "instances": []}]), "didn't create"),
    (state(extra=[{"module": "module.x", "mode": "data", "type": "vsphere_datacenter", "name": "this",
                   "provider": PROVIDER, "instances": []}]), "didn't create"),
    (state().replace(b"registry.opentofu.org/vmware", b"registry.example.com/evil"), "unexpected provider"),
    (state(name="other"), "describes a VM named"),
    (state(moid="vm-42; rm -rf"), "valid vCenter id"),
    (b'{"version": 3, "resources": []}', "version 4"),
    (b"not json", "not valid JSON"),
])
def test_state_is_strictly_checked(bad, match):
    with pytest.raises(BackupError, match=match):
        checks.check_state("web-01", bad)


def test_tfvars_are_checked_and_lose_connection_settings():
    out = checks.check_tfvars("web-01", tfvars(vsphere_server="evil.example", vsphere_insecure=True))
    assert "vsphere_server" not in out and "vsphere_insecure" not in out and out["cpus"] == 2
    for bad, match in [(tfvars(iso_url="x"), "unexpected variable"), (tfvars(name="other"), "another VM"),
                       (tfvars(cpus="2"), "unexpected variable"), (tfvars(folder="a\nb"), "control")]:
        with pytest.raises(BackupError, match=match):
            checks.check_tfvars("web-01", bad)


def test_import_into_another_portal(vcenter, data_dir):
    make_vm()
    db.execute("UPDATE vms SET autostart=1")
    b = export()
    # "Another portal": the VM's record and folder are gone, alice exists there too.
    db.execute("DELETE FROM vms")
    import shutil
    shutil.rmtree(provision.vm_dir("web-01"))
    plan = restore.plan_vm_imports(b, admin())
    restore.check_vcenter(plan)
    assert [p.name for p in plan] == ["web-01"] and plan[0].ok, plan[0].problems
    assert restore.import_vms(plan, ["web-01"], admin(), "test") == ["web-01"]
    vm = db.active_vm("web-01")
    assert vm["status"] == "ready" and vm["owner_id"] == users.by_name("alice")["id"] and vm["notes"] == "hi"
    assert vm["autostart"] == 1
    d = provision.vm_dir("web-01")
    assert provision.vm_moid("web-01") == "vm-42"
    assert "vsphere_server" not in json.loads((d / "terraform.tfvars.json").read_text())
    for f in ("terraform.tfstate", "terraform.tfvars.json"):
        assert (d / f).stat().st_mode & 0o777 == 0o600
    assert d.stat().st_mode & 0o777 == 0o700
    assert db.one("SELECT 1 FROM audit WHERE action='vm.state_imported'")


@pytest.mark.parametrize("change, match", [
    ({"name": "someone-else"}, "is a VM named"),
    ({"template": True}, "template"),
    ({"datacenter": "Other"}, "datacenter"),
    ({"annotation": "Production database"}, "didn't create it"),
])
def test_import_refuses_vms_that_are_not_the_portals(vcenter, change, match):
    make_vm()
    b = export()
    db.execute("DELETE FROM vms")
    import shutil
    shutil.rmtree(provision.vm_dir("web-01"))
    vcenter["vm-42"] = {**good_facts(), **change}
    plan = restore.plan_vm_imports(b, admin())
    restore.check_vcenter(plan)
    assert any(match in p for p in plan[0].problems), plan[0].problems
    with pytest.raises(ValueError, match="can't be imported"):
        restore.import_vms(plan, ["web-01"], admin(), "test")
    assert db.active_vm("web-01") is None


def test_import_checks_folder_and_missing_vm(vcenter, monkeypatch):
    from app import config
    monkeypatch.setenv("VSPHERE_FOLDER", "Portal")
    config.get_settings.cache_clear()
    make_vm()
    b = export()
    db.execute("DELETE FROM vms")
    import shutil
    shutil.rmtree(provision.vm_dir("web-01"))
    vcenter["vm-42"] = {**good_facts(), "folder": "Prod"}
    plan = restore.plan_vm_imports(b, admin())
    restore.check_vcenter(plan)
    assert "outside the portal's 'Portal'" in plan[0].problems[0]
    vcenter["vm-42"] = {**good_facts(), "folder": "Portal/team-a"}
    plan = restore.plan_vm_imports(b, admin())
    restore.check_vcenter(plan)
    assert plan[0].ok
    del vcenter["vm-42"]
    plan = restore.plan_vm_imports(b, admin())
    restore.check_vcenter(plan)
    assert "no VM with id vm-42" in plan[0].problems[0]


def test_unknown_owner_becomes_the_importing_admin(vcenter):
    make_vm(owner="carol")
    b = export()
    db.execute("DELETE FROM vms")
    db.execute("DELETE FROM users WHERE username='carol'")
    import shutil
    shutil.rmtree(provision.vm_dir("web-01"))
    plan = restore.plan_vm_imports(b, admin())
    assert plan[0].owner_id == admin()["id"] and "you become the owner" in plan[0].notes[0]


def test_replacing_the_state_of_an_existing_vm(vcenter):
    d = make_vm()
    b = export()
    (d / "terraform.tfstate").write_text("damaged{")
    plan = restore.plan_vm_imports(b, admin())
    restore.check_vcenter(plan)
    assert plan[0].ok and plan[0].replace
    restore.import_vms(plan, ["web-01"], admin(), "test")
    assert provision.vm_moid("web-01") == "vm-42"
    assert [p.read_text() for p in provision.state_history_files(d)] == ["damaged{"]
    assert db.one("SELECT COUNT(*) AS n FROM vms")["n"] == 1


def test_never_retargets_an_existing_vm(vcenter):
    make_vm()
    b = export()
    (provision.vm_dir("web-01") / "terraform.tfstate").write_bytes(state(moid="vm-77"))
    plan = restore.plan_vm_imports(b, admin())
    assert "already has a VM named web-01 (vm-77)" in plan[0].problems[0]


def test_ip_conflict_is_a_problem(vcenter):
    make_vm()
    b = export()
    db.execute("UPDATE vms SET name='other-01'")
    import shutil
    shutil.rmtree(provision.vm_dir("web-01"))
    plan = restore.plan_vm_imports(b, admin())
    assert "10.0.0.11 is already used" in plan[0].problems[0]


def test_state_history_keeps_the_newest_five():
    d = make_vm()
    for i in range(7):
        (d / "terraform.tfstate").write_text(f"v{i}")
        provision.keep_state_history(d)
    hist = provision.state_history_files(d)
    assert [p.read_text() for p in hist] == ["v2", "v3", "v4", "v5", "v6"]
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in hist)
    assert (d / "history").stat().st_mode & 0o777 == 0o700
