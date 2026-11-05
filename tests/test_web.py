import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def no_vcenter(monkeypatch):
    from app import vsphere

    async def no_delay():
        return None
    monkeypatch.setattr("app.main.login_failed_delay", no_delay)
    monkeypatch.setattr(vsphere, "vms_by_name", lambda fresh=False: {})
    monkeypatch.setattr(vsphere, "templates", lambda: [])
    monkeypatch.setattr(vsphere, "name_exists", lambda name: False)


def client():
    from app.main import app
    return TestClient(app)


def csrf(c, path="/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(path).text).group(1)


def login(c, username="admin", password="test-password-123"):
    r = c.post("/login", data={"username": username, "password": password, "csrf_token": csrf(c)},
               follow_redirects=False)
    return r


def test_requires_login():
    with client() as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/healthz").text == "ok"


def test_login_flow():
    with client() as c:
        assert login(c, password="wrong").status_code == 401
        assert login(c, username="nobody").status_code == 401
        r = login(c)
        assert r.status_code == 303
        page = c.get("/")
        assert page.status_code == 200 and "Virtual machines" in page.text


def test_security_headers():
    with client() as c:
        r = c.get("/login")
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"


def test_post_without_csrf_is_rejected():
    with client() as c:
        login(c)
        r = c.post("/vms", data={"name": "abc", "template": "t", "cpus": 2, "memory_mb": 2048, "disk_gb": 40})
        assert r.status_code == 403
        r = c.post("/logout", data={"csrf_token": "forged"})
        assert r.status_code == 403


def test_login_is_throttled():
    with client() as c:
        for _ in range(5):
            assert login(c, password="wrong-password").status_code == 401
        assert login(c).status_code == 429  # even the right password, for a while


def test_create_rejects_bad_name(monkeypatch):
    from app import config
    monkeypatch.setattr("app.main.vcenter_configured", lambda s=None: True)
    with client() as c:
        login(c)
        r = c.post("/vms", data={"name": "Bad_Name", "template": "t", "cpus": 2, "memory_mb": 2048,
                                 "disk_gb": 40, "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "Name must be" in r.text


def test_setup_requires_token(monkeypatch):
    from app import users
    monkeypatch.delenv("ADMIN_PASSWORD")
    with client() as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/setup"
        token = csrf(c, "/setup")
        data = {"username": "boss", "password": "correct horse battery", "confirm": "correct horse battery",
                "csrf_token": token}
        assert c.post("/setup", data={**data, "token": "guess"}).status_code == 403
        assert users.setup_token
        r = c.post("/setup", data={**data, "token": users.setup_token}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/settings/users").status_code == 200
        # Setup can't be repeated once a user exists.
        data["csrf_token"] = csrf(c, "/")  # the token rotates on login
        assert c.post("/setup", data={**data, "token": "x"}, follow_redirects=False).headers["location"] == "/login"


def test_regular_user_isolation():
    from app import db, users
    with client() as c:
        users.create("alice", "purple-otter-41", "user")
        users.create("bob", "green-walrus-72", "user")
        alice, bob = users.by_name("alice"), users.by_name("bob")
        db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,created_at,owner_id)"
                   " VALUES ('alice-vm','ready','t',1,1024,10,'dhcp',?,?)", (db.now(), alice["id"]))
        job = db.create_job("vm-create", "alice-vm", alice["id"])
        login(c, "bob", "green-walrus-72")
        assert "alice-vm" not in c.get("/").text
        assert c.get("/vms/alice-vm").status_code == 404
        assert c.get(f"/jobs/{job}").status_code == 404
        assert c.get("/settings/vcenter").status_code == 403
        assert c.get("/images").status_code == 403
        c.post("/logout", data={"csrf_token": csrf(c, "/")})
        login(c, "alice", "purple-otter-41")
        assert c.get("/vms/alice-vm").status_code == 200
        assert c.get(f"/jobs/{job}").status_code == 200


def test_password_change_ends_other_sessions():
    from app import users
    with client() as a, client() as b:
        login(a)
        login(b)
        uid = users.by_name("admin")["id"]
        users.set_password(uid, "a-brand-new-password")
        assert a.get("/", follow_redirects=False).status_code == 303


def test_settings_secret_is_encrypted_and_never_rendered(data_dir, monkeypatch):
    from app import db
    with client() as c:
        login(c)
        token = csrf(c, "/settings/vcenter")
        form = {"vsphere_server": "vc2.example", "vsphere_user": "svc", "vsphere_password": "s3cret-pw",
                "vsphere_datacenter": "DC", "vsphere_cluster": "C", "vsphere_datastore": "DS",
                "vsphere_network": "N", "csrf_token": token}
        # conftest sets VSPHERE_* in the environment, which wins; drop them for this test
        import os
        for k in list(os.environ):
            if k.startswith("VSPHERE_"):
                monkeypatch.delenv(k)
        from app import config
        config.get_settings.cache_clear()
        r = c.post("/settings/vcenter", data=form)
        assert r.status_code == 200, r.text
        assert "s3cret-pw" not in r.text
        row = db.one("SELECT value, encrypted FROM settings WHERE key='vsphere_password'")
        assert row["encrypted"] == 1 and "s3cret-pw" not in row["value"]
        assert config.get_settings().vsphere_password == "s3cret-pw"
        assert "s3cret-pw" not in (data_dir / "portal.db").read_bytes().decode(errors="ignore")
        # Changing the server without re-entering the password is refused.
        r = c.post("/settings/vcenter", data={**form, "vsphere_server": "evil.example", "vsphere_password": ""})
        assert r.status_code == 400 and "Re-enter the password" in r.text
        audit = db.query("SELECT detail FROM audit WHERE action='settings.changed'")
        assert audit and "s3cret-pw" not in audit[0]["detail"]


def test_env_values_are_locked():
    from app import config
    assert "vsphere_server" in config.env_overrides()
    with client() as c:
        login(c)
        page = c.get("/settings/vcenter").text
        assert "Set by the environment" in page and "vc-secret" not in page


def test_password_rules():
    from app import users
    for bad in ("short", "alice-is-great-123", "aaaaaaaaaaaaaaa", "x" * 300):
        with pytest.raises(ValueError):
            users.validate_password(bad, "alice")
    users.validate_password("correct horse battery", "alice")
    h = users.hash_password("correct horse battery")
    assert users.verify_password("correct horse battery", h) and not users.verify_password("nope", h)


INV = {"about": "VMware vCenter <b>9</b>", "datacenters": [
    {"name": "DC01", "folders": ["portal", "portal/dev"], "clusters": [
        {"name": "cluster01", "datastores": [{"name": "ds01", "type": "NFS", "free_gb": 900, "capacity_gb": 2000}],
         "networks": ["VLAN100", "VM Network"]},
        {"name": "cluster02", "datastores": [{"name": "ds02", "type": "VMFS", "free_gb": 10, "capacity_gb": 100}],
         "networks": ["VLAN200"]}]},
    {"name": "DC02", "folders": [], "clusters": []}]}


@pytest.fixture
def fake_inventory(monkeypatch):
    from app import vsphere
    seen = {}

    def fake(server, user, password, insecure, fresh=False):
        seen.update(server=server, password=password)
        return INV
    monkeypatch.setattr(vsphere, "inventory", fake)
    for k in list(__import__("os").environ):
        if k.startswith("VSPHERE_"):
            monkeypatch.delenv(k)
    return seen


def test_placement_dropdowns(fake_inventory):
    with client() as c:
        login(c)
        token = csrf(c, "/settings/vcenter")
        base = {"csrf_token": token, "vsphere_server": "vc.lab", "vsphere_user": "u", "vsphere_password": "pw"}
        r = c.post("/settings/vcenter/placement", data=base, headers={"HX-Request": "true"})
        assert r.status_code == 200 and "VMware vCenter &lt;b&gt;9" in r.text  # escaped
        assert fake_inventory == {"server": "vc.lab", "password": "pw"}
        assert '<option value="DC01"' in r.text and "cluster01" not in r.text  # nothing chosen yet
        r = c.post("/settings/vcenter/placement", data={**base, "vsphere_datacenter": "DC01"})
        assert "cluster02" in r.text and "ds01" not in r.text
        r = c.post("/settings/vcenter/placement", data={**base, "vsphere_datacenter": "DC01",
                                                        "vsphere_cluster": "cluster02"})
        assert 'value="ds02" selected' in r.text and 'value="VLAN200" selected' in r.text  # only choice
        assert "portal/dev" in r.text
        # Changing the datacenter resets the cluster.
        r = c.post("/settings/vcenter/placement", data={**base, "vsphere_datacenter": "DC02",
                                                        "vsphere_cluster": "cluster02"},
                   headers={"HX-Trigger-Name": "vsphere_datacenter"})
        assert "not found in vCenter" not in r.text
        assert c.post("/settings/vcenter/placement", data={"vsphere_server": "x"}).status_code == 403  # no CSRF


def test_placement_flags_missing_and_falls_back(fake_inventory, monkeypatch):
    from app import vsphere
    with client() as c:
        login(c)
        base = {"csrf_token": csrf(c, "/settings/vcenter"), "vsphere_server": "vc.lab", "vsphere_user": "u",
                "vsphere_password": "pw", "vsphere_datacenter": "DC01", "vsphere_cluster": "gone"}
        r = c.post("/settings/vcenter/placement", data=base)
        assert "gone (not found in vCenter)" in r.text

        def boom(**kw):
            raise RuntimeError("no route to host")
        monkeypatch.setattr(vsphere, "inventory", boom)
        r = c.post("/settings/vcenter/placement", data=base)
        assert "no route to host" in r.text and 'name="vsphere_cluster" value="gone"' in r.text


def test_setup_wizard(fake_inventory):
    from app import config
    with client() as c:
        login(c)
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/setup/vcenter"
        token = csrf(c, "/setup/vcenter")
        r = c.post("/setup/vcenter", data={"csrf_token": token, "vsphere_server": "vc.lab",
                                           "vsphere_user": "u", "vsphere_password": "pw"})
        assert r.status_code == 400 and "Still missing" in r.text
        r = c.post("/setup/vcenter", data={"csrf_token": token, "vsphere_server": "vc.lab", "vsphere_user": "u",
                                           "vsphere_password": "pw", "vsphere_datacenter": "DC01",
                                           "vsphere_cluster": "cluster01", "vsphere_datastore": "ds01",
                                           "vsphere_network": "VLAN100"}, follow_redirects=False)
        assert r.headers["location"] == "/setup/network"
        assert config.vcenter_configured()
        assert c.get("/setup/network").status_code == 200


def test_admin_cannot_lock_themselves_out():
    from app import users
    with client() as c:
        login(c)
        me = users.by_name("admin")["id"]
        token = csrf(c, "/settings/users")
        assert c.post(f"/settings/users/{me}/disable", data={"csrf_token": token}).status_code == 400
        assert c.post(f"/settings/users/{me}/role", data={"csrf_token": token, "role": "user"}).status_code == 400


def test_role_change_takes_effect_immediately():
    from app import users
    with client() as admin, client() as bob:
        users.create("bob", "green-walrus-72", "admin")
        login(bob, "bob", "green-walrus-72")
        assert bob.get("/settings/vcenter").status_code == 200
        login(admin)
        uid = users.by_name("bob")["id"]
        admin.post(f"/settings/users/{uid}/role", data={"csrf_token": csrf(admin, "/settings/users"), "role": "user"})
        assert bob.get("/settings/vcenter", follow_redirects=False).status_code == 303  # session ended


def test_invalid_stored_setting_is_ignored():
    from app import config, db
    db.save_settings({"template_max_age_days": 99999, "default_cpus": 4}, set(), by="test")
    config.get_settings.cache_clear()
    s = config.get_settings()
    assert s.template_max_age_days == 30 and s.default_cpus == 4


def test_iso_picker(monkeypatch):
    from app import vsphere
    monkeypatch.setattr(vsphere, "list_isos", lambda: ["[ds1] ISO/ubuntu.iso"])
    with client() as c:
        login(c)
        r = c.get("/images/defs/ubuntu-26.04/isos")
        assert "Installer ISO" in r.text and '<option value="[ds1] ISO/ubuntu.iso"' in r.text


def test_create_with_console_password_is_not_echoed(monkeypatch):
    monkeypatch.setattr("app.main.vcenter_configured", lambda s=None: True)
    with client() as c:
        login(c)
        r = c.post("/vms", data={"name": "Bad_Name", "template": "t", "cpus": 2, "memory_mb": 2048, "disk_gb": 40,
                                 "console_password": "correct horse battery",
                                 "console_password_confirm": "correct horse battery",
                                 "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "correct horse battery" not in r.text
        r = c.post("/vms", data={"name": "good-name", "template": "t", "cpus": 2, "memory_mb": 2048, "disk_gb": 40,
                                 "console_password": "correct horse battery", "console_password_confirm": "nope",
                                 "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "passwords don&#39;t match" in r.text


def test_build_key_on_templates_tab(monkeypatch, tmp_path):
    from app import packer
    monkeypatch.setattr(packer, "LEGACY_KEY", tmp_path / "none")
    with client() as c:
        login(c)
        page = c.get("/settings/templates").text
        assert "ssh-ed25519 " in page and "PRIVATE KEY" not in page
        before = packer.public_key()
        r = c.post("/settings/templates/regenerate-key", data={"csrf_token": csrf(c, "/settings/templates")})
        assert r.status_code == 200 and packer.public_key() != before
