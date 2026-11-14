"""Whether a new VM's first-boot setup (cloud-init: users, keys, the compose app) worked.

cloud-init writes its outcome to /run/cloud-init/result.json when it finishes. The portal
reads that file through the backend (a platform's `first_boot`), never by having the guest
call in, so VMs need no route to the portal and the portal no endpoint for them.

State, in the vms table (firstboot_state):
  NULL      not tracked (VMs from before this, other guests, platforms that can't read it)
  waiting   watching for the result
  done      cloud-init finished without errors
  failed    cloud-init reported errors (firstboot_detail has them and the log's last lines)
  unknown   no result in time, or the backend can't read it (firstboot_detail says why)

The watcher runs in the background while any VM is waiting, and starts again on boot, so
a restart doesn't lose a VM that's mid-setup.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger(__name__)

POLL_SECONDS = 15
# Clone, boot, then pulling a compose app's images can take a while on a slow line.
TIMEOUT = timedelta(minutes=30)
RESULT_PATH = "/run/cloud-init/result.json"
OUTPUT_LOG = "/var/log/cloud-init-output.log"
LOG_LINES = 20
LINE_CHARS = 300
MAX_ERRORS = 10


@dataclass
class Result:
    ok: bool
    errors: list[str] = field(default_factory=list)
    log: str = ""          # the last lines of cloud-init's output log (only when it failed)


class Unreadable(Exception):
    """The backend can't read the result at all (a missing permission): waiting won't help."""


def _clean(s: str, limit: int) -> str:
    s = "".join(c if c.isprintable() or c == "\t" else "?" for c in str(s))
    return s[:limit]


def parse_result(text: str) -> Result:
    """cloud-init's result.json: {"v1": {"datasource": ..., "errors": [...]}}."""
    try:
        v1 = (json.loads(text) or {}).get("v1") or {}
    except (ValueError, AttributeError):
        raise ValueError("cloud-init's result.json isn't valid JSON") from None
    errors = v1.get("errors") or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    return Result(ok=not errors, errors=[_clean(e, 500) for e in errors[:MAX_ERRORS]])


def tail(text: str, lines: int = LOG_LINES) -> str:
    return "\n".join(_clean(line, LINE_CHARS) for line in text.splitlines()[-lines:])


# --- state -------------------------------------------------------------------------------

def tracked(platform) -> bool:
    return callable(getattr(platform, "first_boot", None))


def detail(vm: dict) -> dict:
    try:
        return json.loads(vm.get("firstboot_detail") or "{}")
    except ValueError:
        return {}


def record(vm_id: int, state: str, info: dict | None = None) -> None:
    db.execute("UPDATE vms SET firstboot_state=?, firstboot_detail=? WHERE id=?",
               (state, json.dumps(info or {}), vm_id))


def record_result(vm_id: int, r: Result) -> str:
    state = "done" if r.ok else "failed"
    record(vm_id, state, {"errors": r.errors, "log": r.log} if not r.ok else None)
    return state


def _since(vm: dict) -> datetime:
    try:
        t = datetime.fromisoformat(vm.get("firstboot_since") or vm["created_at"])
    except (KeyError, TypeError, ValueError):
        return datetime.now(timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def check(vm: dict) -> str:
    """Ask the backend once (blocking: run it in a thread). Returns the VM's state now."""
    from . import backends, provision
    b = backends.of_vm(vm)
    if b is None or not tracked(b.platform):
        return vm.get("firstboot_state") or "unknown"
    try:
        r = b.platform.first_boot(b, vm["name"], provision.vm_moid(vm["name"]))
    except Unreadable as e:
        record(vm["id"], "unknown", {"reason": str(e)})
        return "unknown"
    if r is not None:
        return record_result(vm["id"], r)
    if datetime.now(timezone.utc) - _since(vm) > TIMEOUT:
        record(vm["id"], "unknown", {"reason": f"No result from cloud-init {int(TIMEOUT.total_seconds() // 60)}"
                                               " minutes after the VM was created."})
        return "unknown"
    return "waiting"


def restart_watch(vm_id: int) -> None:
    """Watch a VM again from now (the VM page's Check again, after a timeout)."""
    db.execute("UPDATE vms SET firstboot_state='waiting', firstboot_detail=NULL, firstboot_since=? WHERE id=?",
               (db.now(), vm_id))
    ensure_watching()


# --- the watcher -------------------------------------------------------------------------

_task: asyncio.Task | None = None


def _waiting() -> list[dict]:
    # Only VMs whose create job is done: before that there is nothing to ask.
    return db.query("SELECT * FROM vms WHERE firstboot_state='waiting' AND status='ready'")


async def tick() -> None:
    for vm in _waiting():
        try:
            await asyncio.to_thread(check, vm)
        except Exception as e:  # noqa: BLE001 - the backend may be down; try again next round
            log.warning("first-boot check of %s failed: %s", vm["name"], e)
            if datetime.now(timezone.utc) - _since(vm) > TIMEOUT:
                record(vm["id"], "unknown", {"reason": f"Couldn't ask {vm['name']}'s backend: {e}"[:300]})


async def _watch() -> None:
    while db.query("SELECT 1 FROM vms WHERE firstboot_state='waiting' AND status IN ('provisioning', 'ready')"):
        await asyncio.sleep(POLL_SECONDS)
        try:
            await tick()
        except Exception:  # noqa: BLE001 - keep watching
            log.exception("watching first boots failed")


def ensure_watching() -> None:
    global _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _task is None or _task.done() or _task.get_loop() is not loop:
        _task = loop.create_task(_watch())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
