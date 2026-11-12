"""Power actions in progress: what was asked of a VM, and whether it has happened yet.

A guest reboot or shutdown only asks the guest; the backend's call returns at once, and on
vSphere a reboot never even changes the power state. So after the call the portal watches
the VM (every POLL_SECONDS, in the background, while anything is pending) until it has
done what was asked, or TIMEOUT_SECONDS pass, and the VM page and list say so meanwhile.

Kept in memory: the portal is one process, and a restart only forgets a few minutes of
"Rebooting…".
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

POLL_SECONDS = 3
TIMEOUT_SECONDS = 300
KEEP_SECONDS = 120          # how long a finished action is remembered, for its message
BOOT_SLACK = timedelta(seconds=15)   # Proxmox's boot time is worked out from a cached uptime

WATCHED = ("on", "shutdown", "reboot")
LABELS = {"on": "Starting", "shutdown": "Shutting down", "reboot": "Rebooting"}
WAITING = {"on": "Starting… waiting for the guest tools to answer",
           "shutdown": "Shutting down… waiting for the guest to power off",
           "reboot": "Rebooting… waiting for the guest to come back"}
DONE = {"on": "Started: the guest tools answer.", "shutdown": "Shut down: the VM is powered off.",
        "reboot": "Rebooted: the guest is back up.", "off": "Powered off."}
TIMED_OUT = {
    "on": "Powered on, but the guest tools haven't answered after {m} minutes. Open the console to see what it's doing.",
    "shutdown": "Still running after {m} minutes: the guest may be ignoring the shutdown request. "
                "Use Power off (hard) to force it.",
    "reboot": "The VM hasn't reported back {m} minutes after the reboot. Open the console to see what it's doing.",
}


@dataclass
class Pending:
    vm_id: int
    name: str
    backend_id: int | None
    action: str
    by: str
    asked: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started: float = field(default_factory=time.monotonic)
    boot_before: datetime | None = None     # the guest's boot time when asked
    had_tools: bool = True                  # the guest tools answered when asked (else: don't wait for them)
    tools_went_down: bool = False
    finished: float | None = None
    outcome: str | None = None              # "done" or "timeout"
    announced: bool = False                 # its outcome was shown as a message

    @property
    def running(self) -> bool:
        return self.finished is None

    @property
    def label(self) -> str:
        return LABELS.get(self.action, self.action)

    @property
    def message(self) -> str:
        if self.running:
            return WAITING[self.action]
        if self.outcome == "done":
            return DONE[self.action]
        return TIMED_OUT[self.action].format(m=TIMEOUT_SECONDS // 60)

    @property
    def elapsed(self) -> int:
        return int(time.monotonic() - self.started)


_pending: dict[int, Pending] = {}
_task: asyncio.Task | None = None


def get(vm_id: int) -> Pending | None:
    p = _pending.get(vm_id)
    if p and p.running and p.elapsed > TIMEOUT_SECONDS + 4 * POLL_SECONDS:
        finish(p, "timeout")        # nothing watched it (the watcher stopped): give up all the same
    if p and not p.running and time.monotonic() - p.finished > KEEP_SECONDS:
        _pending.pop(vm_id, None)
        return None
    return p


def running(vm_id: int) -> Pending | None:
    p = get(vm_id)
    return p if p and p.running else None


def start(vm: dict, action: str, by: str, info=None) -> Pending | None:
    """Note that `action` was just asked of the VM (whose state before was `info`); returns
    what is now watched, or None for an action that's over once its call returns."""
    if action not in WATCHED:
        _pending.pop(vm["id"], None)
        return None
    p = Pending(vm_id=vm["id"], name=vm["name"], backend_id=vm.get("backend_id"), action=action, by=by,
                boot_before=getattr(info, "booted", None),
                had_tools=bool(getattr(info, "tools_running", False)) or action == "on")
    _pending[vm["id"]] = p
    _ensure_watching()
    return p


def clear() -> None:
    """Forget everything (tests; a restore)."""
    _pending.clear()


def judge(p: Pending, info) -> bool:
    """Has the action happened, given the VM as it is now (None: not found)? Notes what it
    sees along the way (the tools going down during a reboot)."""
    if info is None:
        return False
    on = info.power_state == "poweredOn"
    if p.action == "shutdown":
        return info.power_state == "poweredOff"
    if p.action == "on":
        return on and info.tools_running
    # reboot: back up, with the tools answering (if they did before), and booted since (or
    # seen going down).
    tools_ok = info.tools_running or not p.had_tools
    if not on or not tools_ok:
        p.tools_went_down = True
        return False
    if info.booted and p.boot_before and info.booted > p.boot_before + BOOT_SLACK:
        return True
    if info.booted and not p.boot_before and info.booted > p.asked - BOOT_SLACK:
        return True
    return p.tools_went_down and p.had_tools


def finish(p: Pending, outcome: str) -> None:
    p.finished, p.outcome = time.monotonic(), outcome
    log.info("%s %s: %s after %ss", p.name, p.action, outcome, p.elapsed)


async def tick() -> None:
    """Look at every VM with an action in progress once (each backend asked once)."""
    from . import backends
    from .main import on_backend
    waiting = [p for p in list(_pending.values()) if p.running]
    by_backend: dict = {}
    for p in waiting:
        by_backend.setdefault(p.backend_id, []).append(p)
    for bid, ps in by_backend.items():
        b = backends.get(bid) if bid is not None else None
        live, err = await on_backend(b, b.platform.vms_by_name, True) if b else (None, None)
        for p in ps:
            if live is not None and judge(p, live.get(p.name)):
                finish(p, "done")
            elif time.monotonic() - p.started > TIMEOUT_SECONDS:
                finish(p, "timeout")


async def _watch() -> None:
    while any(p.running for p in _pending.values()):
        await asyncio.sleep(POLL_SECONDS)
        try:
            await tick()
        except Exception:  # noqa: BLE001 - keep watching; the next round may work
            log.exception("watching power actions failed")


def _ensure_watching() -> None:
    global _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return          # not in the app (a script); nothing watches, the timeout still applies
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
