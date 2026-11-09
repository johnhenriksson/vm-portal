"""Scheduled backups: a full backup every few hours, written to BACKUP_DIR and encrypted
to age public keys, keeping the newest N.

Public-key encryption means the portal can write backups it can never read: the secret
keys stay offline, so someone who takes over the portal can't open the backups in
BACKUP_DIR. Each run is an ordinary job (visible under Jobs); its outcome is stored as
backup_status and shown under Settings → Backup and, when backups fall behind, on the
dashboard.
"""
import asyncio
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db, jobs
from ..config import get_bootstrap, get_settings
from . import archive, collect

log = logging.getLogger(__name__)
CHECK_SECONDS = 60
STATUS_KEY = "backup_status"
NAME_RE = re.compile(r"^vm-portal-full-([0-9a-f]{8})-([0-9]{8}T[0-9]{6}Z)\.vmpbak$")

_tasks: dict[asyncio.AbstractEventLoop, asyncio.Task] = {}   # one per running app


def status() -> dict:
    return db.get_meta(STATUS_KEY, {}) or {}


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def next_due(now: datetime | None = None) -> datetime | None:
    s = get_settings()
    if not s.backup_enabled:
        return None
    st = status()
    last = _parse(st.get("last_attempt"))
    if last is None:
        return now or datetime.now(timezone.utc)
    hours = min(1, s.backup_interval_hours) if st.get("last_error") else s.backup_interval_hours  # retry sooner
    return last + timedelta(hours=hours)


def health() -> dict:
    """What the dashboard and Settings show: ok, or why not."""
    s, st = get_settings(), status()
    if not s.backup_enabled:
        return {"state": "off"}
    last_ok = _parse(st.get("last_success"))
    if st.get("last_error"):
        return {"state": "failing", "error": st["last_error"], "last_success": last_ok}
    if last_ok and datetime.now(timezone.utc) - last_ok > timedelta(hours=2 * s.backup_interval_hours + 1):
        return {"state": "late", "last_success": last_ok}
    return {"state": "ok" if last_ok else "pending", "last_success": last_ok}


def check_dir() -> Path:
    d = get_bootstrap().backup_dir
    if not d.is_dir():
        raise ValueError(f"The backup folder {d} doesn't exist: mount a volume there (BACKUP_DIR)")
    if d.is_symlink() or d.resolve() == get_bootstrap().data_dir.resolve() or \
            get_bootstrap().data_dir.resolve() in d.resolve().parents:
        raise ValueError(f"The backup folder {d} must not be inside the data volume")
    if not os.access(d, os.W_OK):
        raise ValueError(f"The portal can't write to {d} (it runs as uid {os.getuid()})")
    return d


def _write(d: Path, data: bytes) -> Path:
    """Write atomically, 0600, never over an existing file or through a link."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    final = d / f"vm-portal-full-{collect.installation_id()[:8]}-{stamp}{archive.SUFFIX}"
    tmp = d / f".tmp-{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if final.exists():
            raise ValueError(f"{final.name} already exists")
        tmp.rename(final)
    finally:
        tmp.unlink(missing_ok=True)
    return final


def prune(d: Path, keep: int) -> list[str]:
    """Delete this installation's oldest scheduled backups beyond `keep`. Only files this
    portal names this way are touched: never other files, other portals' backups or links."""
    mine = collect.installation_id()[:8]
    ours = sorted(p for p in d.iterdir()
                  if (m := NAME_RE.match(p.name)) and m.group(1) == mine and p.is_file() and not p.is_symlink())
    removed = []
    for p in ours[:-keep] if keep > 0 else []:
        p.unlink(missing_ok=True)
        removed.append(p.name)
    return removed


def run_backup(by: str = "schedule") -> tuple[Path, list[str]]:
    """Blocking: make one scheduled backup now. Returns the file and the pruned names."""
    s = get_settings()
    if not s.backup_recipients:
        raise ValueError("No age public keys to encrypt to")
    d = check_dir()
    files, meta = collect.full(by, include_logs=s.backup_include_logs)
    data = archive.write("full", files, recipients=s.backup_recipients, **meta)
    if len(data) > get_bootstrap().backup_max_mb * 1024 * 1024:
        raise ValueError("The backup is larger than BACKUP_MAX_MB, so it couldn't be restored: raise the limit "
                         "or leave the job logs out")
    path = _write(d, data)
    return path, prune(d, s.backup_keep)


def submit(by: str = "schedule", user_id: int | None = None) -> int:
    """Run a scheduled backup as a job (also used by "Back up now")."""
    st = status()
    st["last_attempt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.set_meta(STATUS_KEY, st)

    async def run(job_id: int, jl: jobs.JobLog) -> None:
        jl.step(f"Full backup to {get_bootstrap().backup_dir}, encrypted to "
                f"{len(get_settings().backup_recipients)} public key(s)")
        try:
            path, removed = await asyncio.to_thread(run_backup, by)
        except Exception as e:
            st = status()
            st["last_error"] = str(e)[:500]
            db.set_meta(STATUS_KEY, st)
            db.audit("backup.scheduled_failed", str(e)[:300], username=by)
            raise jobs.JobFailed(str(e)) from None
        size = path.stat().st_size
        jl.write(f"Wrote {path.name} ({size // 1024} KB)\n")
        for name in removed:
            jl.write(f"Deleted old backup {name}\n")
        st = status()
        st.update({"last_success": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "last_file": path.name, "last_size": size, "last_error": None})
        db.set_meta(STATUS_KEY, st)
        db.audit("backup.scheduled", f"{path.name}, {size // 1024} KB", username=by)

    return jobs.submit("backup", "scheduled", run, lock_key="backup", user_id=user_id)


def maybe_run() -> int | None:
    """Start a scheduled backup when one is due and nothing is in the way."""
    due = next_due()
    if due is None or due > datetime.now(timezone.utc) or jobs.paused or jobs.is_locked("backup"):
        return None
    if collect.busy_vms():
        return None   # a VM's state is being written; try again next minute
    return submit()


async def _loop() -> None:
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        try:
            maybe_run()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("Scheduled backup check failed")


def start() -> None:
    loop = asyncio.get_running_loop()
    if loop not in _tasks:
        _tasks[loop] = loop.create_task(_loop())


async def stop() -> None:
    task = _tasks.pop(asyncio.get_running_loop(), None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
