"""Resizing a VM: the plan guard, the tfvars handling and the job, with a fake OpenTofu."""
import asyncio
import json
import sys
import textwrap

import pytest

from app import db, jobs, provision, vsphere
from tests.test_web import client, csrf, login

VM_VALUES = {
    "num_cpus": 2, "memory": 4096, "guest_id": "ubuntu64Guest", "scsi_type": "pvscsi", "firmware": "efi",
    "network_interface": [{"adapter_type": "vmxnet3", "network_id": "net-1"}],
    "clone": [{"template_uuid": "tmpl-uuid"}],
    "disk": [{"label": "disk0", "size": 40, "thin_provisioned": True}],
    "annotation": "x", "swap_placement_policy": "",
}


def plan(after: dict, actions=("update",), unknown: dict | None = None, address="vsphere_virtual_machine.this"):
    return {"resource_changes": [
        {"address": "data.vsphere_datacenter.this", "change": {"actions": ["read"]}},
        {"address": address, "change": {"actions": list(actions), "before": VM_VALUES,
                                        "after": {**VM_VALUES, **after}, "after_unknown": unknown or {}}},
    ]}


def test_plan_guard_allows_only_a_resize():
    assert provision.check_resize_plan(plan({"num_cpus": 4, "memory": 8192})) == []
    assert provision.check_resize_plan(plan({"disk": [{**VM_VALUES["disk"][0], "size": 80}]})) == []
    assert provision.check_resize_plan(plan({}, unknown={"reboot_required": True})) == []
    assert provision.check_resize_plan(plan({"swap_placement_policy": "inherit"})) == []  # "" -> default, seen with vcsim
    assert provision.check_resize_plan(plan({"swap_placement_policy": "vmDirectory"}))
    # A manual change in vCenter that the plan would undo, a replacement, another resource.
    assert provision.check_resize_plan(plan({"annotation": "y"})) == ["would change annotation"]
    assert provision.check_resize_plan(plan({"disk": [{**VM_VALUES["disk"][0], "thin_provisioned": False}]})) \
        == ["would change disk.0.thin_provisioned"]
    assert provision.check_resize_plan(plan({}, actions=("delete", "create")))
    assert provision.check_resize_plan(plan({}, address="vsphere_virtual_machine.other"))
    assert provision.check_resize_plan(plan({}, unknown={"network_interface": True}))


def test_facts_and_sizes_come_from_state():
    show = {"values": {"root_module": {"resources": [{"address": "vsphere_virtual_machine.this",
                                                       "values": VM_VALUES}]}}}
    assert provision.template_facts(show) == {"guest_id": "ubuntu64Guest", "scsi_type": "pvscsi", "firmware": "efi",
                                              "adapter_type": "vmxnet3", "uuid": "tmpl-uuid"}
    assert provision.actual_size(show) == {"cpus": 2, "memory_mb": 4096, "disk_gb": 40}
    assert provision.template_facts({}) is None and provision.actual_size({}) is None


# A stand-in for tofu: `show -json` prints state.json (or plan.json for a plan file),
# `apply` copies the sizes from terraform.tfvars.json into state.json.
FAKE_TOFU = textwrap.dedent('''\
    import json, sys, pathlib
    args = sys.argv[1:]
    if args[0] == "show":
        f = "plan.json" if len(args) > 2 else "state.json"
        print(pathlib.Path(f).read_text())
        sys.exit(0)
    elif args[0] == "plan":
        out = next(a for a in args if a.startswith("-out="))[5:]
        pathlib.Path(out).write_text("plan with secrets")
        pathlib.Path("plan-dir-mode").write_text(oct(pathlib.Path(out).parent.stat().st_mode & 0o777))
    elif args[0] == "apply":
        if pathlib.Path("fail-apply").exists():
            sys.exit(1)
        v = json.loads(pathlib.Path("terraform.tfvars.json").read_text())
        s = json.loads(pathlib.Path("state.json").read_text())
        vm = s["values"]["root_module"]["resources"][0]["values"]
        vm["num_cpus"], vm["memory"], vm["disk"][0]["size"] = v["cpus"], v["memory_mb"], v["disk_gb"]
        pathlib.Path("state.json").write_text(json.dumps(s))
    print("ok", args[0])
''')


@pytest.fixture
def fake_vm(data_dir, monkeypatch):
    tofu = data_dir / "fake-tofu"
    tofu.write_text(f"#!{sys.executable}\n" + FAKE_TOFU)
    tofu.chmod(0o755)
    monkeypatch.setattr(provision, "TOFU", str(tofu))
    d = data_dir / "vms" / "web01"
    d.mkdir(parents=True)
    (d / "terraform.tfstate").write_text("{}")
    (d / "state.json").write_text(json.dumps({"values": {"root_module": {"resources": [
        {"address": "vsphere_virtual_machine.this", "values": VM_VALUES}]}}}))
    (d / "terraform.tfvars.json").write_text(json.dumps({"name": "web01", "cpus": 2, "memory_mb": 4096,
                                                         "disk_gb": 40, "userdata": "#cloud-config keep",
                                                         "vsphere_server": "old-vc", "vsphere_insecure": True}))
    (d / "plan.json").write_text(json.dumps(plan({"num_cpus": 4})))
    db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,created_at,owner_id)"
               " VALUES ('web01','ready','ubuntu-2604-docker-20260101-0000',2,4096,40,'dhcp',?,NULL)", (db.now(),))
    return d


async def _finish(job_id):
    while db.get_job(job_id)["status"] not in ("succeeded", "failed"):
        await asyncio.sleep(0.05)
    return db.get_job(job_id)["status"], jobs.log_path(job_id).read_text()


async def test_resize_job_updates_vm_and_keeps_first_boot_data(fake_vm):
    vm = db.active_vm("web01")
    status, log = await _finish(provision.resize(vm, 4, 8192, 60, restart=True))
    assert status == "succeeded", log
    tfvars = json.loads((fake_vm / "terraform.tfvars.json").read_text())
    assert tfvars["userdata"] == "#cloud-config keep"           # first-boot data untouched
    assert "vsphere_insecure" not in tfvars and "vsphere_server" not in tfvars  # current settings apply
    assert tfvars["template_facts"]["uuid"] == "tmpl-uuid"      # no template lookup needed any more
    assert (tfvars["cpus"], tfvars["memory_mb"], tfvars["disk_gb"]) == (4, 8192, 60)
    row = db.active_vm("web01")
    assert (row["cpus"], row["memory_mb"], row["disk_gb"]) == (4, 8192, 60)
    assert not (fake_vm / ".plan").exists()                     # the plan file holds the password
    assert (fake_vm / "plan-dir-mode").read_text() == "0o700"   # and was private while it existed
    assert (fake_vm / "main.tf").exists()                       # module copy refreshed
    assert "grows into the new space" in log


async def test_resize_stops_when_the_plan_does_more(fake_vm):
    (fake_vm / "plan.json").write_text(json.dumps(plan({"annotation": "changed by hand"})))
    status, log = await _finish(provision.resize(db.active_vm("web01"), 4, 4096, 40, restart=True))
    assert status == "failed" and "would change annotation" in log and "ok apply" not in log
    assert db.active_vm("web01")["cpus"] == 2
    assert json.loads((fake_vm / "terraform.tfvars.json").read_text())["cpus"] == 2  # back to what's real
    assert not (fake_vm / ".plan").exists()


async def test_failed_apply_records_the_real_size(fake_vm):
    (fake_vm / "fail-apply").touch()
    status, _ = await _finish(provision.resize(db.active_vm("web01"), 4, 4096, 40, restart=True))
    assert status == "failed" and db.active_vm("web01")["cpus"] == 2


async def test_no_restart_means_no_cpu_change_on_a_running_vm(fake_vm, monkeypatch):
    monkeypatch.setattr(vsphere, "vms_by_name", lambda fresh=False: {"web01": vsphere.VMInfo("web01", False, "poweredOn")})
    status, log = await _finish(provision.resize(db.active_vm("web01"), 4, 4096, 40, restart=False))
    assert status == "failed" and "no restart was allowed" in log and "ok plan" not in log


async def test_disk_cannot_shrink_below_the_real_size(fake_vm):
    db.execute("UPDATE vms SET disk_gb=20 WHERE name='web01'")   # clone got the template's bigger disk
    status, log = await _finish(provision.resize(db.active_vm("web01"), 2, 4096, 30, restart=False))
    assert status == "failed" and "already 40 GB" in log
    with pytest.raises(ValueError):
        provision.resize(db.active_vm("web01"), 2, 4096, 10, restart=False)


def test_resize_route_permissions_and_restart_tick(fake_vm, monkeypatch):
    from app import users
    started = []
    monkeypatch.setattr(provision, "resize", lambda vm, *a, **kw: started.append((a, kw)) or 1)
    monkeypatch.setattr(vsphere, "vms_by_name", lambda fresh=False: {"web01": vsphere.VMInfo("web01", False, "poweredOn")})
    with client() as c:
        users.create("alice", "purple-otter-41", "user")
        login(c, "alice", "purple-otter-41")
        tok = csrf(c, "/")
        form = {"cpus": 4, "memory_mb": 4096, "disk_gb": 40, "csrf_token": tok}
        assert c.post("/vms/web01/resize", data=form).status_code == 404       # not alice's VM
        db.execute("UPDATE vms SET owner_id=? WHERE name='web01'", (users.by_name("alice")["id"],))
        assert 'action="/vms/web01/resize"' in c.get("/vms/web01").text
        r = c.post("/vms/web01/resize", data=form)
        assert r.status_code == 400 and "Restart" in r.text and not started  # running, no tick
        r = c.post("/vms/web01/resize", data={**form, "restart": "on"}, follow_redirects=False)
        assert r.status_code == 303 and started[0][1]["restart"] is True
        assert c.post("/vms/web01/resize", data={**form, "memory_mb": 4097, "restart": "on"}).status_code == 400
        assert c.post("/vms/web01/resize", data={**form, "cpus": 99, "restart": "on"}).status_code == 422
        assert any(r["action"] == "vm.resize" for r in db.query("SELECT action FROM audit"))


def test_notes_and_owner(fake_vm):
    from app import users
    with client() as c:
        users.create("alice", "purple-otter-41", "user")
        alice = users.by_name("alice")
        login(c)
        tok = csrf(c, "/")
        r = c.post("/vms/web01/details", data={"notes": "Runs <b>wiki</b>", "owner_id": str(alice["id"]),
                                               "csrf_token": tok}, follow_redirects=False)
        assert r.status_code == 303
        vm = db.active_vm("web01")
        assert vm["owner_id"] == alice["id"] and vm["notes"] == "Runs <b>wiki</b>"
        assert "Runs &lt;b&gt;wiki&lt;/b&gt;" in c.get("/vms/web01").text
        assert c.post("/vms/web01/details", data={"notes": "x", "owner_id": "999", "csrf_token": tok}).status_code == 400
        c.post("/logout", data={"csrf_token": tok})
        # Owners edit notes but can't hand the VM to someone else.
        login(c, "alice", "purple-otter-41")
        tok = csrf(c, "/")
        admin_id = users.by_name("admin")["id"]
        c.post("/vms/web01/details", data={"notes": "mine", "owner_id": str(admin_id), "csrf_token": tok})
        vm = db.active_vm("web01")
        assert vm["owner_id"] == alice["id"] and vm["notes"] == "mine"
        actions = [r["action"] for r in db.query("SELECT action FROM audit")]
        assert "vm.owner_changed" in actions and "vm.notes_changed" in actions
