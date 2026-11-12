"""Backends: the portal's connections to vCenters and Proxmox VE clusters, and the upgrade
from before they existed (when the vCenter connection and IP pool were global settings)."""
import json
from contextlib import contextmanager

import pytest

from app import backends, config, crypto, db, users
from app.backup import archive, checks, collect, restore
from app.backup.archive import BackupError
from app.platforms import PLATFORMS

from backup_helpers import admin, make_vm, state, tfvars, vc

PVE_STATE = json.dumps({"version": 4, "resources": [
    {"mode": "managed", "type": "proxmox_virtual_environment_vm", "name": "this",
     "provider": 'provider["registry.opentofu.org/bpg/proxmox"]',
     "instances": [{"attributes": {"name": "pve-01", "vm_id": 104}}]}]}).encode()


def drop_env(monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith(("VSPHERE_", "IP_")):
            monkeypatch.delenv(k)
    config.get_settings.cache_clear()


def legacy_settings():
    """What a portal from before backends had stored."""
    db.save_settings({"vsphere_server": "vc.old", "vsphere_user": "svc", "vsphere_datacenter": "DC",
                      "vsphere_cluster": "C", "vsphere_datastore": "DS", "vsphere_network": "N",
                      "ip_pool_start": "10.5.0.10", "ip_pool_end": "10.5.0.20", "ip_gateway": "10.5.0.1",
                      "default_cpus": 4}, set(), by="old")
    db.save_settings({"vsphere_password": "old-secret"}, {"vsphere_password"}, by="old")


def test_upgrade_turns_the_old_settings_into_a_backend(monkeypatch):
    drop_env(monkeypatch)
    db.conn()
    db.execute("DELETE FROM backends")
    legacy_settings()
    db.execute("INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, created_at)"
               " VALUES ('web-01', 'ready', 't', 1, 1024, 10, 'dhcp', ?)", (db.now(),))
    db.close()                                         # the next start opens the database again
    [b] = backends.all_backends()
    assert (b.name, b.type, b.from_env) == ("vCenter", "vsphere", True)
    assert b.cfg.vsphere_server == "vc.old" and b.cfg.vsphere_password == "old-secret" and b.configured
    assert b.cfg.ip_pool_end == "10.5.0.20"
    stored = db.load_settings()
    assert "vsphere_server" not in stored and "vsphere_password" not in stored and "ip_pool_start" not in stored
    assert stored["default_cpus"] == 4                 # the global settings stay
    assert db.active_vm("web-01")["backend_id"] == b.id
    assert db.one("SELECT 1 FROM audit WHERE action='backends.migrated'")
    db.close()
    assert len(backends.all_backends()) == 1           # only once


def test_environment_still_describes_a_backend():
    b = vc()
    assert b.from_env and b.cfg.vsphere_server == "vc.example" and b.cfg.vsphere_password == "vc-secret"
    assert b.cfg.ip_pool_start == "10.0.0.10" and {"vsphere_server", "ip_pool_start"} <= b.locked
    assert len(backends.all_backends()) == 1           # created once, then found


def test_names_and_validation():
    bid = backends.create("Proxmox Oslo", "proxmox", {}, by="t")
    with pytest.raises(ValueError, match="already a backend"):
        backends.create("proxmox oslo", "proxmox", {}, by="t")
    for bad in ("", " ", "x" * 41, "<b>", "a/b"):
        with pytest.raises(ValueError):
            backends.validate_name(bad)
    with pytest.raises(ValueError, match="Not settings of a Proxmox"):
        backends.validate("proxmox", {"vsphere_server": "vc"})
    with pytest.raises(ValueError, match="IP pool"):
        backends.validate("proxmox", {"ip_pool_start": "10.0.0.5"})
    assert not backends.get(bid).configured and backends.get(bid) not in backends.usable(admin())


def test_environment_secret_never_goes_to_another_server(monkeypatch):
    """The server comes from the UI, the password from the environment: the server can't change."""
    bid = vc().id
    backends.update(vc(), {}, "t")
    db.execute("UPDATE backends SET config=? WHERE id=?", (json.dumps({"vsphere_server": "vc.lab"}), bid))
    monkeypatch.delenv("VSPHERE_SERVER")
    config.get_settings.cache_clear()
    b = backends.get(bid)
    assert b.cfg.vsphere_server == "vc.lab" and "vsphere_password" in b.locked
    with pytest.raises(ValueError, match="set VSPHERE_SERVER in the environment"):
        backends.update(b, {"vsphere_server": "attacker.example"}, "t")
    assert backends.get(bid).cfg.vsphere_server == "vc.lab"


def test_full_backup_round_trip_with_two_backends(monkeypatch):
    pve = backends.create("Proxmox Oslo", "proxmox", {"proxmox_server": "pve.lab", "proxmox_token_id": "p@pve!t",
                                                      "proxmox_token_secret": "pve-secret"}, by="t")
    users.create("boss", "correct horse battery", "admin")
    make_vm()
    files, meta = collect.full("boss")
    assert json.loads(files["secrets.json"]) == {"backends": {str(pve): {"proxmox_token_secret": "pve-secret"}}}
    assert b"pve-secret" not in files["db/backends.json"]
    data = archive.write("full", files, passphrase="correct horse battery staple", **meta)
    db.execute("DELETE FROM backends WHERE id=?", (pve,))
    plan = restore.plan_full_restore(archive.read(data, "correct horse battery staple"))
    restore.apply_full_restore(plan, by="boss")
    b = backends.get(pve)
    assert b.name == "Proxmox Oslo" and b.cfg.proxmox_token_secret == "pve-secret"
    assert db.active_vm("web-01")["backend_id"] == vc().id


def test_restoring_a_backup_from_before_backends(monkeypatch):
    """Its vCenter connection and pool are settings: they become a backend once restored."""
    drop_env(monkeypatch)
    users.create("boss", "correct horse battery", "admin")
    dump = db.dump(("users", "jobs", "audit", "image_settings"))
    vm = {"id": 1, "name": "web-01", "status": "ready", "template": "t", "cpus": 2, "memory_mb": 4096,
          "disk_gb": 40, "ip_mode": "dhcp", "ip": None, "has_compose": 0, "created_at": db.now(),
          "last_job_id": None, "owner_id": None, "console_login": 0, "vm_user": None, "image_id": None,
          "notes": "", "autostart": None}
    files = {f"db/{t}.json": json.dumps(rows).encode() for t, rows in dump.items()}
    files.update({"db/vms.json": json.dumps([vm]).encode(),
                  "db/settings.json": json.dumps({"vsphere_server": "vc.old", "vsphere_user": "svc",
                                                  "ip_pool_start": "10.5.0.10", "ip_pool_end": "10.5.0.20",
                                                  "ip_gateway": "10.5.0.1"}).encode(),
                  "secrets.json": json.dumps({"vsphere_password": "old-secret"}).encode(),
                  "vms/web-01/terraform.tfstate": state(), "vms/web-01/terraform.tfvars.json": tfvars()})
    data = archive.write("full", files, passphrase="correct horse battery staple", **collect.meta("boss"))
    plan = restore.plan_full_restore(archive.read(data, "correct horse battery staple"))
    restore.apply_full_restore(plan, by="boss")
    [b] = backends.all_backends()
    assert b.cfg.vsphere_server == "vc.old" and b.cfg.vsphere_password == "old-secret"
    assert b.cfg.ip_pool_start == "10.5.0.10" and db.active_vm("web-01")["backend_id"] == b.id


def test_full_backup_rows_are_checked():
    users.create("boss", "correct horse battery", "admin")
    files, meta = collect.full("boss")
    rows = json.loads(files["db/backends.json"])
    for bad, match in [({"type": "hyperv"}, "unknown backend type"), ({"name": "<b>"}, "Name the backend"),
                       ({"config": {"vsphere_server": "bad host!"}}, "hostname")]:
        tampered = {**files, "db/backends.json": json.dumps([{**rows[0], **bad}]).encode()}
        data = archive.write("full", tampered, passphrase="correct horse battery staple", **meta)
        with pytest.raises(BackupError, match=match):
            restore.plan_full_restore(archive.read(data, "correct horse battery staple"))


# --- VM backups ----------------------------------------------------------------------------

def export(names=("web-01",)):
    files, meta = collect.vms(list(names), by="admin2")
    return archive.unpack(archive.pack("vms", files, **meta))


def test_vm_backup_names_its_backend_and_import_finds_it(monkeypatch):
    other = backends.create("Other vCenter", "vsphere", {}, by="t")
    make_vm()
    b = export()
    row = b.json("db/vms.json")[0]
    assert row["backend_name"] == "vCenter" and row["backend_type"] == "vsphere" and row["backend_id"] is None
    db.execute("DELETE FROM vms")
    import shutil
    from app import provision
    shutil.rmtree(provision.vm_dir("web-01"))
    plan = restore.plan_vm_imports(b, admin())
    assert plan[0].backend.id == vc().id                # by name, though two vCenters could hold it
    db.execute("UPDATE backends SET name='Renamed' WHERE id=?", (vc().id,))
    plan = restore.plan_vm_imports(b, admin())
    assert "several VMware vSphere backends" in plan[0].problems[0]
    db.execute("DELETE FROM backends WHERE id=?", (other,))
    assert restore.plan_vm_imports(b, admin())[0].backend.id == vc().id   # the only vCenter


def test_proxmox_state_is_checked_by_its_own_rules():
    pf, vmid = checks.check_state_platform("pve-01", PVE_STATE)
    assert pf is PLATFORMS["proxmox"] and vmid == "104"
    good = json.dumps({"name": "pve-01", "template": "t", "node": "pve1", "datastore": "local-lvm", "bridge": "vmbr0",
                       "pool": "", "template_vmid": 100, "template_node": "pve1",
                       "seed_iso": "local:iso/vm-portal-seed-pve-01.iso", "cpus": 2, "memory_mb": 2048,
                       "disk_gb": 25, "metadata": "m", "userdata": "u", "annotation": "a",
                       "proxmox_endpoint": "https://evil/", "proxmox_api_token": "x"}).encode()
    out = checks.check_tfvars("pve-01", good, pf)
    assert "proxmox_endpoint" not in out and "proxmox_api_token" not in out and out["template_vmid"] == 100
    for bad in ({"template_vmid": "100"}, {"template_vmid": True}, {"datacenter": "DC"}):
        with pytest.raises(BackupError, match="unexpected variable"):
            checks.check_tfvars("pve-01", json.dumps({**json.loads(good), **bad}).encode(), pf)
    # A vSphere VM's variables aren't a Proxmox VM's.
    with pytest.raises(BackupError, match="unexpected variable"):
        checks.check_tfvars("web-01", tfvars(), pf)
    assert crypto  # (imported for the module's fixtures)


# --- pages that act on one backend ------------------------------------------------------------

@contextmanager
def web():
    """A client logged in as the first admin (the portal started, as in production)."""
    import re

    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"username": "admin", "password": "test-password-123", "csrf_token": tok})
        yield c, re.search(r'name="csrf_token" value="([^"]+)"', c.get("/account").text).group(1)


def test_image_isos_builds_and_templates_are_per_backend(monkeypatch):
    from app import images, packer
    from app.platforms import vsphere
    from app.platforms.base import VMInfo
    other = backends.create("Other vCenter", "vsphere", {k: getattr(vc().cfg, k) for k in vsphere.REQUIRED}, by="t")
    calls = []
    monkeypatch.setattr(vsphere, "templates", lambda b: [VMInfo(f"ubuntu-2604-docker-2026110{i}-0300", True)
                                                         for i in (2, 1)] if b.id == other else [])
    monkeypatch.setattr(vsphere, "delete_template", lambda b, name: calls.append((b.id, name)))
    monkeypatch.setattr(packer, "start_build", lambda img, b, user_id=None: calls.append(("build", b.id)) or 1)
    monkeypatch.setattr(packer, "preflight", lambda img=None, cfg=None, b=None: [])
    with web() as (c, tok):
        r = c.post("/images/defs/ubuntu-26.04/settings", data={"csrf_token": tok, "enabled": "on",
                   f"iso_{vc().id}": "[ds1] ISO/a.iso", f"iso_{other}": "[ds2] ISO/b.iso", "cpus": "2",
                   "memory_mb": "2048", "disk_mb": "25600", "keep": "3", "param_ssh_username": "ubuntu",
                   "param_keyboard_layout": "us", "param_locale": "en_US.UTF-8"})
        assert r.status_code == 200, r.text
        cfg = images.config(images.get("ubuntu-26.04"))
        assert cfg.isos == {str(vc().id): "[ds1] ISO/a.iso", str(other): "[ds2] ISO/b.iso"}
        assert images.iso_for(cfg, backends.get(other)) == "[ds2] ISO/b.iso"
        assert c.post("/images/defs/ubuntu-26.04/build", data={"csrf_token": tok}).status_code == 400  # which one?
        r = c.post("/images/defs/ubuntu-26.04/build", data={"csrf_token": tok, "backend_id": str(other)},
                   follow_redirects=False)
        assert r.status_code == 303 and calls == [("build", other)]
        page = c.get("/images").text
        assert "Other vCenter" in page and f"/images/templates/{other}/ubuntu-2604-docker-20261101-0300/delete" in page
        assert f"/images/templates/{other}/ubuntu-2604-docker-20261102-0300/delete" not in page   # the newest
        r = c.post(f"/images/templates/{other}/ubuntu-2604-docker-20261101-0300/delete", data={"csrf_token": tok},
                   follow_redirects=False)
        assert r.status_code == 303 and calls[-1] == (other, "ubuntu-2604-docker-20261101-0300")
        r = c.post(f"/images/templates/{vc().id}/ubuntu-2604-docker-20261101-0300/delete", data={"csrf_token": tok})
        assert r.status_code == 404                      # not on that backend


def test_check_vms_and_seed_cleanup_routes(monkeypatch):
    from app.platforms import vsphere
    monkeypatch.setattr(vsphere, "vm_facts", lambda b, moid: {"name": "web-01", "template": False, "datacenter": "DC",
                                                              "folder": "", "annotation": "Created by vm-portal"})
    monkeypatch.setattr(vsphere, "vms_by_name", lambda b, fresh=False: {})
    monkeypatch.setattr(vsphere, "templates", lambda b: [])
    with web() as (c, tok):
        make_vm()       # after the first admin exists (it's only made while there are no users)
        r = c.post("/settings/backup/check-vms", data={"csrf_token": tok})
        assert "All 1 VM(s) match their backends" in r.text
        assert c.post("/vms/web-01/seed-cleanup", data={"csrf_token": tok}).status_code == 400   # vSphere: no seed
        page = c.get("/vms/web-01").text
        assert "Backend</dt><dd>vCenter" in page and "seed ISO" not in page


def test_setup_with_a_proxmox_backend(monkeypatch):
    drop_env(monkeypatch)
    with web() as (c, tok):
        r = c.post("/setup/backend", data={"csrf_token": tok, "type": "proxmox", "name": "Lab PVE"},
                   follow_redirects=False)
        assert r.headers["location"] == "/setup/backend"
        page = c.get("/setup/backend").text
        assert "Connect to Lab PVE" in page and "API token" in page and "/settings/backends/" in page
        r = c.post("/setup/backend", data={"csrf_token": tok, "proxmox_server": "pve.lab", "proxmox_token_id": "a@pve!t",
                                           "proxmox_token_secret": "s3", "proxmox_node": "pve1",
                                           "proxmox_storage": "local-lvm", "proxmox_iso_storage": "local",
                                           "proxmox_bridge": "vmbr0"}, follow_redirects=False)
        assert r.status_code == 303 and backends.all_backends()[0].configured
        assert "s3" not in c.get("/settings/backends/1").text.split("csrf_token")[0]
