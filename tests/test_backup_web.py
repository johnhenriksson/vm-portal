import re
import shutil

import pytest

from app import db, jobs, provision, users
from app.backup import archive
from backup_helpers import good_facts, make_vm, state
from test_web import client, csrf, login

PASS = "a long enough backup passphrase"
ADMIN_PW = "test-password-123"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    from app import vsphere

    async def no_delay():
        return None
    monkeypatch.setattr("app.main.login_failed_delay", no_delay)
    monkeypatch.setattr(vsphere, "vms_by_name", lambda fresh=False: {})
    monkeypatch.setattr(vsphere, "templates", lambda: [])
    facts = {"vm-42": good_facts()}
    monkeypatch.setattr(vsphere, "vm_facts", lambda moid: facts.get(moid))
    return facts


def download(c, **form):
    data = {"what": "full", "encrypt": "passphrase", "passphrase": PASS, "confirm": ADMIN_PW,
            "csrf_token": csrf(c, "/settings/backup"), **form}
    return c.post("/settings/backup/download", data=data)


def open_backup(c, blob, secret=PASS, confirm=ADMIN_PW):
    return c.post("/settings/backup/open", data={"secret": secret, "confirm": confirm,
                                                  "csrf_token": csrf(c, "/settings/backup")},
                  files={"file": ("x.vmpbak", blob, "application/octet-stream")})


def token(page):
    return re.search(r'name="token" value="([^"]+)"', page.text).group(1)


def test_backup_tab_is_for_admins_only():
    with client() as c:
        login(c)
        assert c.get("/settings/backup").status_code == 200
        users.create("bob", "correct horse battery", "user")
    with client() as c:
        login(c, "bob", "correct horse battery")
        assert c.get("/settings/backup").status_code == 403
        for path in ("/settings/backup/download", "/settings/backup/open", "/settings/backup/restore",
                     "/settings/backup/import-vms", "/settings/backup/check-vms", "/settings/backup/run-now"):
            assert c.post(path, data={"csrf_token": csrf(c, "/")}).status_code == 403


def test_download_needs_password_and_a_strong_passphrase():
    with client() as c:
        login(c)
        r = download(c, confirm="wrong")
        assert r.status_code == 400 and "password" in r.text and r.headers["content-type"].startswith("text/html")
        r = download(c, passphrase="short")
        assert r.status_code == 400 and "16 characters" in r.text
        r = download(c)
        assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
        assert re.match(r'attachment; filename="vm-portal-full-[0-9a-f]{8}-[0-9T]+Z\.vmpbak"',
                        r.headers["content-disposition"])
        assert r.headers["cache-control"] == "no-store"
        b = archive.read(r.content, PASS)
        assert b.kind == "full" and b.manifest["created_by"] == "admin"
        assert db.one("SELECT detail FROM audit WHERE action='backup.downloaded'")["detail"].startswith("full")
        assert PASS not in str(db.query("SELECT * FROM audit"))


def test_export_and_import_vms_through_the_ui(fakes):
    with client() as c:
        login(c)
        make_vm()
        r = download(c, what="vms", vms="web-01")
        assert r.status_code == 200 and archive.read(r.content, PASS).kind == "vms"
        blob = r.content
        # The VM disappears from this portal; import it back.
        db.execute("DELETE FROM vms")
        shutil.rmtree(provision.vm_dir("web-01"))
        page = open_backup(c, blob)
        assert page.status_code == 200 and "web-01" in page.text and "Ready" in page.text
        assert "Restore everything" not in page.text
        r = c.post("/settings/backup/import-vms", data={"token": token(page), "names": "web-01",
                                                        "csrf_token": csrf(c, "/settings/backup")})
        assert r.status_code == 200 and "Imported web-01" in r.text
        assert db.active_vm("web-01") and provision.vm_moid("web-01") == "vm-42"
        # The token was used up.
        r = c.post("/settings/backup/import-vms", data={"token": token(page), "names": "web-01",
                                                        "csrf_token": csrf(c, "/settings/backup")})
        assert r.status_code == 410


def test_open_rejects_wrong_secret_and_password():
    with client() as c:
        login(c)
        blob = download(c).content
        r = open_backup(c, blob, secret="not the passphrase at all")
        assert r.status_code == 400 and "decrypt the backup: wrong passphrase" in r.text
        r = open_backup(c, blob, confirm="wrong")
        assert r.status_code == 400 and "password" in r.text
        r = open_backup(c, b"just some file")
        assert r.status_code == 400 and "a vm-portal backup" in r.text


def test_full_restore_logs_everyone_out(fakes):
    with client() as c, client() as other:
        login(c)
        users.create("alice", "correct horse battery", "user")
        login(other, "alice", "correct horse battery")
        blob = download(c).content
        users.create("carol", "correct horse battery", "user")   # after the backup
        page = open_backup(c, blob)
        assert "Restore everything" in page.text and "this installation" in page.text
        r = c.post("/settings/backup/restore", data={"token": token(page), "typed": "restore",
                                                     "csrf_token": csrf(c, "/settings/backup")})
        assert r.status_code == 400 and "Type RESTORE" in r.text
        r = c.post("/settings/backup/restore", data={"token": token(page), "typed": "RESTORE",
                                                     "csrf_token": csrf(c, "/settings/backup")},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?restored=1"
        assert users.by_name("carol") is None
        # Both the restoring admin and every other session are logged out.
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"
        assert other.get("/", follow_redirects=False).headers["location"] == "/login"
        assert "was restored" in c.get("/login?restored=1").text
        assert login(other, "alice", "correct horse battery").status_code == 303


def test_an_opened_backup_belongs_to_its_session():
    with client() as c, client() as other:
        login(c)
        login(other)
        page = open_backup(c, download(c).content)
        r = other.post("/settings/backup/restore", data={"token": token(page), "typed": "RESTORE",
                                                         "csrf_token": csrf(other, "/settings/backup")})
        assert r.status_code == 410
        # ...and the attempt also closed it for the first session.
        r = c.post("/settings/backup/restore", data={"token": token(page), "typed": "RESTORE",
                                                     "csrf_token": csrf(c, "/settings/backup")})
        assert r.status_code == 410


def test_uploads_are_limited_before_parsing(monkeypatch):
    monkeypatch.setenv("BACKUP_MAX_MB", "16")
    from app import config
    config.reset_caches()
    with client() as c:
        login(c)
        tok = csrf(c, "/settings/backup")
        r = c.post("/settings/backup/open", data={"csrf_token": tok}, files={"file": ("a", b"x" * (17 * 2**20))})
        assert r.status_code == 413 and "BACKUP_MAX_MB" in r.text
    # Without a declared length (chunked), multipart is refused outright.
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.auth import check_upload_size
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [
        (b"content-type", b"multipart/form-data; boundary=x"), (b"transfer-encoding", b"chunked")]})
    with pytest.raises(HTTPException) as e:
        check_upload_size(req)
    assert e.value.status_code == 411


def test_requests_wait_while_a_restore_runs():
    with client() as c:
        login(c)
        jobs.paused = "A backup is being restored."
        try:
            r = c.get("/")
            assert r.status_code == 503 and r.headers["retry-after"] == "30"
            assert c.get("/healthz").status_code == 200
        finally:
            jobs.paused = None
        with pytest.raises(ValueError, match="being restored"):
            jobs.paused = "being restored"
            try:
                jobs.submit("x", "y", None, lock_key="z")
            finally:
                jobs.paused = None


def test_state_history_rollback(fakes):
    with client() as c:
        login(c)
        d = make_vm()
        provision.keep_state_history(d)
        (d / "terraform.tfstate").write_text("damaged{")
        page = c.get("/vms/web-01")
        assert "Roll back" in page.text
        version = re.search(r"/state-history/([0-9TZ]+\.tfstate)/restore", page.text).group(1)
        url = f"/vms/web-01/state-history/{version}/restore"
        assert c.post(url, data={"confirm": "wrong", "csrf_token": csrf(c, "/vms/web-01")}).status_code == 400
        r = c.post(url, data={"confirm": ADMIN_PW, "csrf_token": csrf(c, "/vms/web-01")}, follow_redirects=False)
        assert r.status_code == 303 and provision.vm_moid("web-01") == "vm-42"
        # A version recording another VM id is refused.
        (d / "history" / version).write_bytes(state(moid="vm-77"))
        r = c.post(url, data={"confirm": ADMIN_PW, "csrf_token": csrf(c, "/vms/web-01")})
        assert r.status_code == 400 and "another VM" in r.text
        assert c.post("/vms/web-01/state-history/../../etc/restore",
                      data={"confirm": ADMIN_PW, "csrf_token": csrf(c, "/vms/web-01")}).status_code == 404


def test_restore_during_first_run_setup(monkeypatch, data_dir, tmp_path_factory):
    with client() as c:
        login(c)
        users.create("boss", "correct horse battery", "admin")
        blob = download(c).content
    # A brand-new installation without an admin.
    from app import auth, config
    monkeypatch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("fresh")))
    monkeypatch.delenv("ADMIN_PASSWORD")
    config.reset_caches()
    db._conn = None
    auth.reset_session_epoch()
    with client() as c:
        assert "Restore a full backup instead" in c.get("/setup").text
        tok = csrf(c, "/setup/restore")
        r = c.post("/setup/restore", data={"token": "wrong", "secret": PASS, "csrf_token": tok},
                   files={"file": ("x.vmpbak", blob)})
        assert r.status_code == 403
        r = c.post("/setup/restore", data={"token": users.setup_token, "secret": PASS, "csrf_token": tok},
                   files={"file": ("x.vmpbak", blob)})
        assert r.status_code == 200 and "Restore everything" in r.text and "Import VMs" not in r.text
        r = c.post("/setup/restore/confirm", data={"token": token(r), "typed": "RESTORE",
                                                   "csrf_token": csrf(c, "/setup/restore")}, follow_redirects=False)
        assert r.status_code == 303
        assert login(c, "boss", "correct horse battery").status_code == 303
        assert c.get("/setup/restore", follow_redirects=False).status_code == 303  # only while there are no users


def test_dashboard_warns_admins_when_backups_fail(monkeypatch):
    from app.backup import schedule
    monkeypatch.setattr(schedule, "health", lambda: {"state": "failing", "error": "disk full", "last_success": None})
    with client() as c:
        login(c)
        assert "Scheduled backups are failing: disk full" in c.get("/").text
