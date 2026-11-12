import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def no_vcenter(monkeypatch):
    from app.platforms import vsphere

    async def no_delay():
        return None
    monkeypatch.setattr("app.main.login_failed_delay", no_delay)
    monkeypatch.setattr(vsphere, "vms_by_name", lambda b, fresh=False: {})
    monkeypatch.setattr(vsphere, "templates", lambda b: [])
    monkeypatch.setattr(vsphere, "name_exists", lambda b, name: False)


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


def test_login_lockout_does_not_lock_out_the_owner_elsewhere():
    from app import users
    for _ in range(5):
        users.login_failed("admin", "203.0.113.9")   # an attacker's IP
    assert users.login_retry_after("admin", "203.0.113.9")
    assert not users.login_retry_after("admin", "testclient")  # the owner's IP is not blocked
    with client() as c:
        assert login(c).status_code == 303
    for i in range(25):                                # spread over many IPs: per-username cap
        users.login_failed("admin", f"198.51.100.{i}")
    assert users.login_retry_after("admin", "192.0.2.1")


def test_password_change_is_throttled():
    with client() as c:
        login(c)
        token = csrf(c, "/account")
        for _ in range(5):
            r = c.post("/account/password", data={"csrf_token": token, "current": "guess-guess-guess",
                                                  "password": "x", "confirm": "x"})
            assert "current password is wrong" in r.text
        r = c.post("/account/password", data={"csrf_token": token, "current": "test-password-123",
                                              "password": "x", "confirm": "x"})
        assert "Too many wrong passwords" in r.text


def test_create_rejects_bad_name(monkeypatch):
    from app import config
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


def test_logout_revokes_a_copied_cookie_but_not_other_sessions():
    with client() as c, client() as other, client() as thief:
        login(c)
        login(other)
        thief.cookies.set("vmportal_session", c.cookies.get("vmportal_session"))
        assert thief.get("/", follow_redirects=False).status_code == 200
        c.post("/logout", data={"csrf_token": csrf(c, "/")})
        assert thief.get("/", follow_redirects=False).status_code == 303   # copy is dead
        assert other.get("/", follow_redirects=False).status_code == 200   # other device still logged in


def test_console_guard_ends_on_logout():
    from app import auth, users
    with client() as c:
        login(c)
        session = {"uid": users.by_name("admin")["id"], "sv": users.by_name("admin")["session_version"],
                   "sid": "handshake-sid"}
        assert auth.session_user(session) is not None
        from app import db
        db.revoke_session("handshake-sid", auth.SESSION_MAX_AGE)
        assert auth.session_user(session) is None   # what the console guard re-checks


def test_password_change_ends_other_sessions():
    from app import users
    with client() as a, client() as b:
        login(a)
        login(b)
        uid = users.by_name("admin")["id"]
        users.set_password(uid, "a-brand-new-password")
        assert a.get("/", follow_redirects=False).status_code == 303


def no_env_backend(monkeypatch):
    """Drop the vCenter the environment describes (conftest), so a test can set one up in the UI."""
    import os
    from app import config
    for k in list(os.environ):
        if k.startswith("VSPHERE_"):
            monkeypatch.delenv(k)
    config.get_settings.cache_clear()


def add_backend(c, name="Lab vCenter", kind="vsphere") -> int:
    r = c.post("/settings/backends", data={"csrf_token": csrf(c, "/settings/backends"), "name": name, "type": kind},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_settings_secret_is_encrypted_and_never_rendered(data_dir, monkeypatch):
    from app import backends, db
    no_env_backend(monkeypatch)
    with client() as c:
        login(c)
        bid = add_backend(c)
        token = csrf(c, f"/settings/backends/{bid}")
        form = {"name": "Lab vCenter", "enabled": "on", "vsphere_server": "vc2.example", "vsphere_user": "svc",
                "vsphere_password": "s3cret-pw", "vsphere_datacenter": "DC", "vsphere_cluster": "C",
                "vsphere_datastore": "DS", "vsphere_network": "N", "csrf_token": token}
        r = c.post(f"/settings/backends/{bid}", data=form)
        assert r.status_code == 200, r.text
        assert "s3cret-pw" not in r.text and "saved" in r.text
        row = db.one("SELECT config, secrets FROM backends WHERE id=?", (bid,))
        assert "s3cret-pw" not in row["secrets"] + row["config"] and "vsphere_password" in row["secrets"]
        assert backends.get(bid).cfg.vsphere_password == "s3cret-pw" and backends.get(bid).configured
        assert "s3cret-pw" not in (data_dir / "portal.db").read_bytes().decode(errors="ignore")
        # Changing the server without re-entering the password is refused.
        r = c.post(f"/settings/backends/{bid}", data={**form, "vsphere_server": "evil.example", "vsphere_password": ""})
        assert r.status_code == 400 and "Re-enter the password" in r.text
        assert backends.get(bid).cfg.vsphere_server == "vc2.example"
        audit = db.query("SELECT detail FROM audit WHERE action='backend.changed'")
        assert audit and "s3cret-pw" not in audit[0]["detail"] and "vsphere_password" in audit[0]["detail"]


def test_env_values_are_locked():
    from backup_helpers import vc
    b = vc()
    assert b.from_env and "vsphere_server" in b.locked
    with client() as c:
        login(c)
        page = c.get(f"/settings/backends/{b.id}").text
        assert "Set by the environment" in page and "vc-secret" not in page
        assert "VSPHERE_SERVER" in page
        assert c.get("/settings", follow_redirects=False).headers["location"] == "/settings/backends"
        assert c.get("/settings/vcenter", follow_redirects=False).headers["location"] == f"/settings/backends/{b.id}"


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
    from app.platforms import vsphere
    seen = {}

    def fake(server, user, password, insecure, fresh=False):
        seen.update(server=server, password=password)
        return INV
    monkeypatch.setattr(vsphere, "inventory", fake)
    no_env_backend(monkeypatch)
    return seen


def test_placement_dropdowns(fake_inventory):
    with client() as c:
        login(c)
        bid = add_backend(c)
        url = f"/settings/backends/{bid}/placement"
        token = csrf(c, f"/settings/backends/{bid}")
        base = {"csrf_token": token, "vsphere_server": "vc.lab", "vsphere_user": "u", "vsphere_password": "pw"}
        r = c.post(url, data=base, headers={"HX-Request": "true"})
        assert r.status_code == 200 and "VMware vCenter &lt;b&gt;9" in r.text  # escaped
        assert fake_inventory == {"server": "vc.lab", "password": "pw"}
        assert '<option value="DC01"' in r.text and "cluster01" not in r.text  # nothing chosen yet
        assert f'hx-post="{url}"' in r.text   # choosing reloads this backend's dropdowns
        r = c.post(url, data={**base, "vsphere_datacenter": "DC01"})
        assert "cluster02" in r.text and "ds01" not in r.text
        r = c.post(url, data={**base, "vsphere_datacenter": "DC01", "vsphere_cluster": "cluster02"})
        assert 'value="ds02" selected' in r.text and 'value="VLAN200" selected' in r.text  # only choice
        assert "portal/dev" in r.text
        # Changing the datacenter resets the cluster.
        r = c.post(url, data={**base, "vsphere_datacenter": "DC02", "vsphere_cluster": "cluster02"},
                   headers={"HX-Trigger-Name": "vsphere_datacenter"})
        assert "not found in vCenter" not in r.text
        assert c.post(url, data={"vsphere_server": "x"}).status_code == 403  # no CSRF


def test_env_password_is_never_sent_to_another_server(fake_inventory, monkeypatch):
    # The password comes from the environment, the server from the UI: a portal from before
    # backends, whose stored vCenter settings became the backend the environment completes.
    import json
    from app import backends, config, db
    monkeypatch.setenv("VSPHERE_PASSWORD", "env-secret")
    config.get_settings.cache_clear()
    db.execute("INSERT INTO backends (name, type, from_env, config, created_at, updated_at) VALUES"
               " ('vCenter', 'vsphere', 1, ?, ?, ?)",
               (json.dumps({"vsphere_server": "vc.lab", "vsphere_user": "u"}), db.now(), db.now()))
    b = backends.all_backends()[0]
    assert b.cfg.vsphere_password == "env-secret" and "vsphere_password" in b.locked
    with client() as c:
        login(c)
        token = csrf(c, f"/settings/backends/{b.id}")
        evil = {"csrf_token": token, "name": "vCenter", "vsphere_server": "attacker.example", "vsphere_user": "u",
                "vsphere_insecure": "on"}
        r = c.post(f"/settings/backends/{b.id}/placement", data=evil)
        assert "set VSPHERE_SERVER in the environment" in r.text and fake_inventory == {}
        r = c.post(f"/settings/backends/{b.id}", data=evil)
        assert r.status_code == 400 and backends.get(b.id).cfg.vsphere_server == "vc.lab"
        # The configured server still works with the env password.
        c.post(f"/settings/backends/{b.id}/placement", data={**evil, "vsphere_server": "vc.lab"})
        assert fake_inventory == {"server": "vc.lab", "password": "env-secret"}


def test_placement_flags_missing_and_falls_back(fake_inventory, monkeypatch):
    from app.platforms import vsphere
    with client() as c:
        login(c)
        bid = add_backend(c)
        url = f"/settings/backends/{bid}/placement"
        base = {"csrf_token": csrf(c, f"/settings/backends/{bid}"), "vsphere_server": "vc.lab", "vsphere_user": "u",
                "vsphere_password": "pw", "vsphere_datacenter": "DC01", "vsphere_cluster": "gone"}
        r = c.post(url, data=base)
        assert "gone (not found in vCenter)" in r.text

        def boom(**kw):
            raise RuntimeError("no route to host")
        monkeypatch.setattr(vsphere, "inventory", boom)
        r = c.post(url, data=base)
        assert "no route to host" in r.text and 'name="vsphere_cluster" value="gone"' in r.text


def test_setup_wizard(fake_inventory):
    from app import backends
    with client() as c:
        login(c)
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/setup/backend"
        assert c.get("/setup/vcenter", follow_redirects=False).headers["location"] == "/setup/backend"
        token = csrf(c, "/setup/backend")
        assert "Where should VMs run?" in c.get("/setup/backend").text
        r = c.post("/setup/backend", data={"csrf_token": token, "type": "vsphere", "name": ""}, follow_redirects=False)
        assert r.headers["location"] == "/setup/backend"
        [b] = backends.all_backends()
        assert b.name == "vCenter" and b.type == "vsphere"
        r = c.post("/setup/backend", data={"csrf_token": token, "vsphere_server": "vc.lab",
                                           "vsphere_user": "u", "vsphere_password": "pw"})
        assert r.status_code == 400 and "Still missing" in r.text
        r = c.post("/setup/backend", data={"csrf_token": token, "vsphere_server": "vc.lab", "vsphere_user": "u",
                                           "vsphere_password": "pw", "vsphere_datacenter": "DC01",
                                           "vsphere_cluster": "cluster01", "vsphere_datastore": "ds01",
                                           "vsphere_network": "VLAN100"}, follow_redirects=False)
        assert r.headers["location"] == "/setup/network"
        assert backends.get(b.id).configured
        assert "Static IP pool on vCenter" in c.get("/setup/network").text
        r = c.post("/setup/network", data={"csrf_token": token, "ip_pool_start": "10.1.0.10",
                                           "ip_pool_end": "10.1.0.20", "ip_prefix": "24", "ip_gateway": "10.1.0.1",
                                           "ip_dns": ""}, follow_redirects=False)
        assert r.status_code == 303 and backends.get(b.id).cfg.ip_pool_end == "10.1.0.20"


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
        assert bob.get("/settings/backends").status_code == 200
        login(admin)
        uid = users.by_name("bob")["id"]
        admin.post(f"/settings/users/{uid}/role", data={"csrf_token": csrf(admin, "/settings/users"), "role": "user"})
        assert bob.get("/settings/backends", follow_redirects=False).status_code == 303  # session ended


def test_creating_admin_credentials_needs_the_acting_admins_password():
    # Otherwise a stolen admin session could mint an admin it knows the password of.
    from app import users
    with client() as c:
        login(c)
        token = csrf(c, "/settings/users")
        new = {"csrf_token": token, "username": "svc-backup", "password": "orange-falcon-93", "role": "admin"}
        r = c.post("/settings/users", data=new)
        assert r.status_code == 400 and users.by_name("svc-backup") is None
        r = c.post("/settings/users", data={**new, "confirm": "test-password-123"}, follow_redirects=False)
        assert r.status_code == 303 and users.by_name("svc-backup")["role"] == "admin"
        # A regular user needs no confirmation; promoting one and resetting a password do.
        c.post("/settings/users", data={**new, "username": "carol", "role": "user"})
        carol = users.by_name("carol")
        assert carol["role"] == "user"
        assert c.post(f"/settings/users/{carol['id']}/role", data={"csrf_token": token, "role": "admin"}).status_code == 400
        assert users.by_name("carol")["role"] == "user"
        c.post(f"/settings/users/{carol['id']}/role", data={"csrf_token": token, "role": "admin",
                                                           "confirm": "test-password-123"})
        assert users.by_name("carol")["role"] == "admin"
        r = c.post(f"/settings/users/{carol['id']}/password", data={"csrf_token": token,
                                                                   "password": "brand-new-secret-77"})
        assert r.status_code == 400 and users.by_name("carol")["password_hash"] == carol["password_hash"]


def test_invalid_stored_setting_is_ignored():
    from app import config, db
    db.save_settings({"template_max_age_days": 99999, "default_cpus": 4}, set(), by="test")
    config.get_settings.cache_clear()
    s = config.get_settings()
    assert s.template_max_age_days == 30 and s.default_cpus == 4


def test_iso_picker(monkeypatch):
    from app.platforms import vsphere
    monkeypatch.setattr(vsphere, "list_isos", lambda b: ["[ds1] ISO/ubuntu.iso"])
    with client() as c:
        login(c)
        from backup_helpers import vc
        r = c.get(f"/images/defs/ubuntu-26.04/isos?backend={vc().id}")
        assert "Installer ISO on vCenter" in r.text and '<option value="[ds1] ISO/ubuntu.iso"' in r.text
        assert f'name="iso_{vc().id}"' in r.text
        assert c.get("/images/defs/ubuntu-26.04/isos?backend=99").status_code == 404


def test_create_fails_closed_when_templates_cannot_be_listed(monkeypatch):
    # Without the list, `template` could name any VM in vCenter and Terraform would clone it.
    from app import provision
    from app.platforms import vsphere

    def down(b):
        raise RuntimeError("vCenter hiccup")
    monkeypatch.setattr(vsphere, "templates", down)
    created = []
    monkeypatch.setattr(provision, "create", lambda req, owner_id=None: created.append(req) or 1)
    with client() as c:
        login(c)
        r = c.post("/vms", data={"name": "good-name", "template": "domain-controller", "cpus": 2,
                                 "memory_mb": 2048, "disk_gb": 40, "ssh_keys": "ssh-ed25519 AAAAkey a@b",
                                 "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "vCenter hiccup" in r.text and not created


def test_create_with_console_password_is_not_echoed(monkeypatch):
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


def test_cancel_button_is_for_admins_and_builds_only():
    from app import db, users
    with client() as c:
        users.create("alice", "purple-otter-41", "user")
        alice = users.by_name("alice")
        build = db.create_job("packer-build", "rocky-10-x", None)
        create = db.create_job("vm-create", "alice-vm", alice["id"])
        login(c, "alice", "purple-otter-41")
        assert "Cancel build" not in c.get(f"/jobs/{create}").text
        r = c.post(f"/jobs/{build}/cancel", data={"csrf_token": csrf(c, "/")}, follow_redirects=False)
        assert r.status_code == 403 and db.get_job(build)["status"] == "queued"
        c.post("/logout", data={"csrf_token": csrf(c, "/")})
        login(c)
        assert "Cancel build" in c.get(f"/jobs/{build}").text
        assert "Cancel build" not in c.get(f"/jobs/{create}").text
        token = csrf(c, "/")
        assert c.post(f"/jobs/{create}/cancel", data={"csrf_token": token}).status_code == 400
        r = c.post(f"/jobs/{build}/cancel", data={"csrf_token": token}, follow_redirects=False)
        assert r.status_code == 303
        assert "Cancelling…" in c.get(f"/jobs/{build}").text
        assert "Cancel requested by admin" in jobs_log(build)
        db.set_job_status(build, "cancelled", 1)
        page = c.get(f"/jobs/{build}").text
        assert "badge cancelled" in page and "job-cancel" not in page


def jobs_log(job_id):
    from app import jobs
    return jobs.log_path(job_id).read_text()
