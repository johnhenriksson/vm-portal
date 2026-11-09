import asyncio
import os
from datetime import datetime, timedelta, timezone

import pyrage
import pytest

from app import config, db, jobs
from app.backup import archive, collect, schedule


@pytest.fixture
def keys():
    ident = pyrage.x25519.Identity.generate()
    return str(ident), str(ident.to_public())


@pytest.fixture
def backups(tmp_path_factory, monkeypatch, keys):
    d = tmp_path_factory.mktemp("backups")
    monkeypatch.setenv("BACKUP_DIR", str(d))
    monkeypatch.setenv("BACKUP_RECIPIENTS", keys[1])
    monkeypatch.setenv("BACKUP_ENABLED", "true")
    monkeypatch.setenv("BACKUP_KEEP", "3")
    config.reset_caches()
    return d


def test_scheduled_backup_is_written_privately_and_readable_only_with_the_secret_key(backups, keys):
    path, removed = schedule.run_backup()
    assert path.parent == backups and removed == []
    assert schedule.NAME_RE.match(path.name)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not [p for p in backups.iterdir() if p.name.startswith(".tmp-")]
    b = archive.read(path.read_bytes(), keys[0])
    assert b.kind == "full" and b.manifest["created_by"] == "schedule"
    with pytest.raises(archive.BackupError):
        archive.read(path.read_bytes(), str(pyrage.x25519.Identity.generate()))


def test_prune_keeps_the_newest_and_touches_nothing_else(backups):
    mine = collect.installation_id()[:8]
    names = [f"vm-portal-full-{mine}-2026010{i}T000000Z.vmpbak" for i in range(1, 6)]
    for n in names:
        (backups / n).write_bytes(b"x")
    others = ["vm-portal-full-deadbeef-20260101T000000Z.vmpbak", "notes.txt",
              f"vm-portal-vms-{mine}-20260101T000000Z.vmpbak"]
    for n in others:
        (backups / n).write_bytes(b"x")
    os.symlink("/etc/hostname", backups / f"vm-portal-full-{mine}-20250101T000000Z.vmpbak")
    assert schedule.prune(backups, 3) == names[:2]
    left = {p.name for p in backups.iterdir()}
    assert set(names[2:]) <= left and set(others) <= left
    assert os.path.exists("/etc/hostname")


def test_backup_dir_checks(backups, monkeypatch, data_dir):
    monkeypatch.setenv("BACKUP_DIR", str(data_dir / "inside"))
    (data_dir / "inside").mkdir()
    config.reset_caches()
    with pytest.raises(ValueError, match="inside the data volume"):
        schedule.check_dir()
    monkeypatch.setenv("BACKUP_DIR", "/nonexistent/backups")
    config.reset_caches()
    with pytest.raises(ValueError, match="doesn't exist"):
        schedule.check_dir()


def test_enabling_needs_a_public_key(monkeypatch):
    monkeypatch.setenv("BACKUP_ENABLED", "true")
    with pytest.raises(Exception, match="public key"):
        config.Settings()


def test_when_a_backup_is_due(backups, monkeypatch):
    started = []
    monkeypatch.setattr(schedule, "submit", lambda: started.append(1) or 1)
    assert schedule.maybe_run() == 1                     # never ran: due now
    now = datetime.now(timezone.utc)
    db.set_meta(schedule.STATUS_KEY, {"last_attempt": now.isoformat()})
    assert schedule.maybe_run() is None                  # just ran
    db.set_meta(schedule.STATUS_KEY, {"last_attempt": (now - timedelta(hours=25)).isoformat()})
    monkeypatch.setattr(collect, "busy_vms", lambda: ["web-01"])
    assert schedule.maybe_run() is None                  # waits for a VM job
    monkeypatch.setattr(collect, "busy_vms", lambda: [])
    jobs.paused = "restoring"
    assert schedule.maybe_run() is None                  # never during a restore
    jobs.paused = None
    assert schedule.maybe_run() == 1
    db.set_meta(schedule.STATUS_KEY, {"last_attempt": (now - timedelta(minutes=61)).isoformat(), "last_error": "x"})
    assert schedule.next_due() <= now                    # failures retry within the hour


def test_health(backups):
    assert schedule.health()["state"] == "pending"
    now = datetime.now(timezone.utc)
    db.set_meta(schedule.STATUS_KEY, {"last_success": (now - timedelta(hours=60)).isoformat()})
    assert schedule.health()["state"] == "late"
    db.set_meta(schedule.STATUS_KEY, {"last_success": now.isoformat(), "last_error": "disk full"})
    assert schedule.health() == {"state": "failing", "error": "disk full", "last_success": schedule._parse(now.isoformat())}


async def test_scheduled_backup_runs_as_a_job(backups):
    job_id = schedule.submit()
    for _ in range(200):
        if db.get_job(job_id)["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.05)
    assert db.get_job(job_id)["status"] == "succeeded"
    st = schedule.status()
    assert st["last_success"] and st["last_file"] and st["last_error"] is None
    assert (backups / st["last_file"]).is_file()
    assert db.one("SELECT 1 FROM audit WHERE action='backup.scheduled'")


async def test_failed_scheduled_backup_is_recorded(backups, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", "/nonexistent")
    config.reset_caches()
    job_id = schedule.submit()
    for _ in range(200):
        if db.get_job(job_id)["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.05)
    assert db.get_job(job_id)["status"] == "failed"
    assert "doesn't exist" in schedule.status()["last_error"]
    assert schedule.health()["state"] == "failing"
