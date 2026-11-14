import json
import shutil

import pyrage
import pytest

from app import auth, backends, config, crypto, db, editor, images, provision, users
from app.backup import archive, collect, restore
from app.backup.archive import BackupError

from backup_helpers import make_vm, vc


@pytest.fixture(scope="module")
def keypair():
    ident = pyrage.x25519.Identity.generate()
    return str(ident), str(ident.to_public())


def custom_image(image_id="my-debian"):
    src = config.get_bootstrap().images_dir / "debian-13"
    dest = images.custom_dir() / image_id
    shutil.copytree(src, dest, ignore=editor._copy_ignore)
    text = (dest / images.MANIFEST).read_text()
    (dest / images.MANIFEST).write_text(editor.rename_manifest(text, "My Debian", "my-debian"))
    return dest


def populate():
    users.create("alice", "correct horse battery", "user")
    users.create("boss", "correct horse battery", "admin")
    db.save_settings({"default_cpus": 4}, set(), by="t")
    # A stored backend secret (the environment's VSPHERE_PASSWORD wins while the portal runs,
    # but what's stored is what a backup has).
    db.execute("UPDATE backends SET secrets=? WHERE id=?",
               (backends.encrypt_secrets({"vsphere_password": "stored-secret"}), vc().id))
    config.get_settings.cache_clear()
    make_vm(owner="alice")
    job = db.create_job("vm-create", "web-01", users.by_name("alice")["id"])
    (config.get_bootstrap().jobs_dir / f"{job}.log").write_text("cloning...\n")
    custom_image()
    db.audit("test.marker", "before backup")


def full_backup(recipients, include_logs=True):
    files, meta = collect.full("boss", include_logs=include_logs)
    return archive.write("full", files, recipients=recipients, **meta), files


def test_full_backup_contents_and_what_stays_out(keypair):
    populate()
    data, files = full_backup([keypair[1]])
    assert {"db/users.json", "db/vms.json", "db/settings.json", "secrets.json", "db/jobs.json", "db/audit.json",
            "db/image_settings.json", "vms/web-01/terraform.tfstate", "jobs/1.log",
            "images/my-debian/image.yaml", "db/backends.json"} <= set(files)
    assert json.loads(files["secrets.json"]) == {"backends": {str(vc().id): {"vsphere_password": "stored-secret"}}}
    [row] = json.loads(files["db/backends.json"])
    assert row["name"] == "vCenter" and row["type"] == "vsphere" and "secrets" not in row
    assert json.loads(files["db/settings.json"]) == {"default_cpus": 4}
    names = " ".join(files)
    for never in ("encryption.key", "session.key", "packer_ssh_key", "revoked", ".terraform", "history", ".plan"):
        assert never not in names
    assert crypto.fernet()._signing_key not in data and b"stored-secret" not in data
    assert "jobs/1.log" not in collect.full("boss")[0]  # logs only on request


def test_environment_secrets_never_go_into_a_backup():
    # conftest sets VSPHERE_PASSWORD in the environment only.
    files, _ = collect.full("x")
    assert json.loads(files["secrets.json"]) == {"backends": {}} and b"vc-secret" not in b"".join(files.values())


def test_full_round_trip_on_the_same_installation(keypair):
    populate()
    db.execute("UPDATE vms SET autostart=0, firstboot_state='failed', firstboot_since=created_at,"
               " firstboot_detail=?", ('{"errors": ["boom"], "log": "a\\nb"}',))
    inst = collect.installation_id()
    data, _ = full_backup([keypair[1]])
    # Things change after the backup...
    db.execute("DELETE FROM users WHERE username='alice'")
    db.execute("UPDATE vms SET notes='changed', autostart=1")
    db.save_settings({"default_cpus": 8}, set(), by="t")
    (provision.vm_dir("web-01") / "terraform.tfstate").write_text("damaged")
    old_epoch = auth.session_epoch()

    plan = restore.plan_full_restore(archive.read(data, keypair[0]), public_key_encrypted=True)
    assert plan.summary["users"] == 2 and plan.summary["vms"] == 1 and plan.summary["logs"] == 1
    assert any("comes from this installation" in w for w in plan.warnings)
    assert any("public key" in w for w in plan.warnings)
    restore.apply_full_restore(plan, by="boss")

    assert users.by_name("alice") and db.active_vm("web-01")["notes"] == "hi"
    assert db.active_vm("web-01")["autostart"] == 0
    assert db.active_vm("web-01")["firstboot_state"] == "failed"
    assert json.loads(db.active_vm("web-01")["firstboot_detail"])["log"] == "a\nb"
    config.get_settings.cache_clear()
    s = config.get_settings()
    assert s.default_cpus == 4 and backends.stored_rows()[0]["secrets"] == {"vsphere_password": "stored-secret"}
    assert db.active_vm("web-01")["backend_id"] == vc().id
    assert provision.vm_moid("web-01") == "vm-42"
    assert (config.get_bootstrap().jobs_dir / "1.log").read_text() == "cloning...\n"
    assert collect.installation_id() == inst
    assert auth.session_epoch() != old_epoch            # every earlier session is over
    assert db.one("SELECT 1 FROM audit WHERE action='test.marker'")
    assert db.one("SELECT 1 FROM audit WHERE action='backup.restored'")
    assert db.one("SELECT COUNT(*) AS n FROM revoked_sessions")["n"] == 0
    pre = config.get_bootstrap().data_dir / restore.PRE_RESTORE
    assert (pre / "portal.db").exists() and (pre / "vms" / "web-01" / "terraform.tfstate").read_text() == "damaged"
    assert not (config.get_bootstrap().data_dir / restore.STAGING).exists()
    assert (config.get_bootstrap().data_dir / "portal.db").stat().st_mode & 0o777 == 0o600


def test_restore_onto_another_installation_with_its_own_key(keypair, tmp_path_factory, monkeypatch):
    populate()
    data, _ = full_backup([keypair[1]])
    source_key = (config.get_bootstrap().data_dir / "encryption.key").read_bytes()
    # A fresh installation: new data volume, new encryption key.
    other = tmp_path_factory.mktemp("other")
    monkeypatch.setenv("DATA_DIR", str(other))
    config.reset_caches()
    db._conn = None
    auth.reset_session_epoch()
    target_inst = collect.installation_id()
    plan = restore.plan_full_restore(archive.read(data, keypair[0]))
    assert any("another installation" in w for w in plan.warnings)
    restore.apply_full_restore(plan, by="cli")
    assert (other / "encryption.key").read_bytes() != source_key
    row = db.one("SELECT secrets FROM backends")
    assert crypto.decrypt(json.loads(row["secrets"])["vsphere_password"]) == '"stored-secret"'
    assert collect.installation_id() == target_inst
    assert users.by_name("boss")["role"] == "admin"


def test_custom_images_come_back_disabled_when_editing_is_off(keypair):
    populate()
    data, _ = full_backup([keypair[1]])
    shutil.rmtree(images.custom_dir() / "my-debian")
    plan = restore.plan_full_restore(archive.read(data, keypair[0]))
    assert any("restored disabled" in w for w in plan.warnings)
    restore.apply_full_restore(plan, by="boss")
    img = images.get("my-debian")
    assert img and not img.builtin and images.config(img).enabled is False
    assert (images.custom_dir() / "my-debian" / images.MANIFEST).is_file()


def test_undo_restore(keypair):
    populate()
    data, _ = full_backup([keypair[1]])
    users.create("carol", "correct horse battery", "user")   # made after the backup
    restore.apply_full_restore(restore.plan_full_restore(archive.read(data, keypair[0])), by="boss")
    assert users.by_name("carol") is None
    restore.undo_full_restore("cli")
    assert users.by_name("carol") is not None
    restore.undo_full_restore("cli")                       # and back again
    assert users.by_name("carol") is None


def tampered(files, path, change):
    files = dict(files)
    files[path] = json.dumps(change(json.loads(files[path]))).encode()
    return archive.Backup({"kind": "full", "installation_id": "x"}, files)


@pytest.mark.parametrize("path, change, match", [
    ("db/users.json", lambda rows: [{**r, "role": "user"} for r in rows], "no active admin"),
    ("db/users.json", lambda rows: [{**r, "password_hash": "plain-password"} for r in rows], "password hash"),
    ("db/users.json", lambda rows: rows + [rows[0]], "duplicate"),
    ("db/users.json", lambda rows: [{**r, "username": "Robert'); DROP TABLE"} for r in rows], "Username"),
    ("db/settings.json", lambda s: {**s, "secret_key": "x"}, "unexpected keys"),
    ("db/settings.json", lambda s: {**s, "vsphere_password": "x"}, "unexpected keys"),
    ("db/settings.json", lambda s: {**s, "ip_gateway": "not-an-ip"}, "ip_gateway"),
    ("secrets.json", lambda s: {"encryption_key": "x"}, "unexpected keys"),
    ("db/vms.json", lambda rows: [{**r, "owner_id": 999} for r in rows], "isn't in the backup"),
    ("db/vms.json", lambda rows: [{**r, "status": "destroyed"} for r in rows], "state files"),
    ("db/vms.json", lambda rows: [{**r, "name": "x"} for r in rows], "invalid VM name"),
    ("db/jobs.json", lambda rows: [{**r, "user_id": 999} for r in rows], "unknown users"),
    ("db/jobs.json", lambda rows: [], "no such job"),
    ("db/audit.json", lambda rows: [{**r, "action": "x; DROP"} for r in rows], "invalid action"),
    ("db/image_settings.json", lambda rows: [{"image_id": "x/../y", "data": "{}", "updated_at": "2026-01-01"}],
     "invalid image id"),
])
def test_full_restore_validates_everything(path, change, match):
    populate()
    files, _ = collect.full("boss", include_logs=True)
    with pytest.raises(BackupError, match=match):
        restore.plan_full_restore(tampered(files, path, change))


def test_full_restore_refuses_other_content():
    populate()
    files, _ = collect.full("boss")
    with pytest.raises(BackupError, match="built-in image"):
        restore.plan_full_restore(archive.Backup({"kind": "full"}, {**files, "images/debian-13/image.yaml": b"x"}))
    with pytest.raises(BackupError, match="not a VM in the backup"):
        restore.plan_full_restore(archive.Backup({"kind": "full"}, {**files, "vms/other-01/terraform.tfstate": b"{}"}))
    with pytest.raises(BackupError, match="File type not allowed"):
        restore.plan_full_restore(archive.Backup({"kind": "full"}, {**files, "images/my-debian/run.exe": b"x"}))
    vm_files, meta = collect.vms(["web-01"], by="boss")
    with pytest.raises(BackupError, match="only holds VM state"):
        restore.plan_full_restore(archive.Backup({"kind": "vms"}, vm_files))


def test_full_backup_waits_for_running_vm_jobs(monkeypatch):
    monkeypatch.setattr(collect, "busy_vms", lambda: ["web-01"])
    with pytest.raises(ValueError, match="running job"):
        collect.full("boss")
