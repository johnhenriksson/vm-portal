"""Background jobs: run CLI tools, stream their output to a log file per job."""
import asyncio
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TextIO

from . import db
from .config import get_settings

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()
_locks: dict[str, asyncio.Lock] = {}
# Cancelling: the command each job is running right now, and the jobs asked to stop.
_procs: dict[int, asyncio.subprocess.Process] = {}
_cancelled: set[int] = set()
_logs: dict[int, "JobLog"] = {}

FINISHED = ("succeeded", "failed", "cancelled")


# Set while a backup is being restored: no job may start (they'd write into data being replaced).
paused: str | None = None


class JobFailed(Exception):
    pass


def running() -> int:
    return sum(1 for t in _tasks if not t.done())


def lock_for(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


def is_locked(key: str) -> bool:
    return key in _locks and _locks[key].locked()


def lock_keys() -> list[str]:
    return list(_locks)


# Child processes (terraform/tofu, packer) get only these from the portal's own
# environment, never its secrets (ENCRYPTION_KEY, SECRET_KEY, ADMIN_PASSWORD, …).
# Packer templates can read environment variables, so this matters.
INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
                 "PACKER_PLUGIN_PATH", "TF_CLI_CONFIG_FILE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "no_proxy")


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in INHERITED_ENV if k in os.environ}
    env.update({"CHECKPOINT_DISABLE": "1", "TF_IN_AUTOMATION": "1"})
    env.update(extra or {})
    return env


def log_path(job_id: int) -> Path:
    return get_settings().jobs_dir / f"{job_id}.log"


_backend_secrets: tuple[float, list[str]] = (0.0, [])


def _secrets() -> list[str]:
    """Never written to a job log: the portal's keys and every backend's secret (read at
    most every few seconds, as logs are written line by line)."""
    global _backend_secrets
    s = get_settings()
    if time.monotonic() - _backend_secrets[0] > 5:
        from . import backends
        from .config import BACKEND_SECRETS
        try:
            found = [getattr(b.cfg, k) for b in backends.all_backends() for k in BACKEND_SECRETS]
        except Exception:  # noqa: BLE001 - e.g. during a restore; keep the last known ones
            found = _backend_secrets[1]
        _backend_secrets = (time.monotonic(), [x for x in found if x])
    return [x for x in (*_backend_secrets[1], s.admin_password, s.secret_key, s.encryption_key) if x]


def redact(text: str) -> str:
    for secret in _secrets():
        text = text.replace(secret, "********")
    return text


class JobLog:
    def __init__(self, fh: TextIO, job_id: int | None = None):
        self.job_id = job_id
        self._fh = fh

    def write(self, text: str) -> None:
        self._fh.write(redact(text))
        self._fh.flush()

    def step(self, text: str) -> None:
        self.write(f"\n==> {text}\n")

    async def run(self, cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
        """Run a command, stream output into the log, raise JobFailed on non-zero exit."""
        self.write(f"$ {' '.join(cmd)}\n")
        full_env = child_env(env)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=full_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout
        if self.job_id is not None:
            _procs[self.job_id] = proc
            if self.job_id in _cancelled:   # cancelled between two commands
                proc.send_signal(signal.SIGTERM)
        try:
            async for line in proc.stdout:
                self.write(line.decode(errors="replace"))
            rc = await proc.wait()
        finally:
            if self.job_id is not None:
                _procs.pop(self.job_id, None)
        if self.job_id in _cancelled:
            raise JobFailed("cancelled")
        if rc != 0:
            raise JobFailed(f"{cmd[0]} exited with code {rc}")


JobFn = Callable[[int, JobLog], Awaitable[None]]


def submit(kind: str, target: str, fn: JobFn, *, lock_key: str, user_id: int | None = None,
           on_success: Callable[[], None] | None = None,
           on_failure: Callable[[], None] | None = None) -> int:
    """Create a job record and run fn in the background, one job per lock_key at a time."""
    if paused:
        raise ValueError(paused)
    job_id = db.create_job(kind, target, user_id)

    async def runner() -> None:
        try:
            async with lock_for(lock_key):
                with open(log_path(job_id), "a", encoding="utf-8") as fh:
                    jl = _logs[job_id] = JobLog(fh, job_id)
                    if job_id in _cancelled:   # cancelled while it waited its turn
                        jl.write("\n==> Job cancelled before it started\n")
                        db.set_job_status(job_id, "cancelled", 1)
                        if on_failure:
                            on_failure()
                        return
                    db.set_job_status(job_id, "running")
                    try:
                        await fn(job_id, jl)
                    except Exception as e:  # noqa: BLE001 - every failure ends up in the job log
                        if job_id in _cancelled:
                            jl.write("\n==> Job cancelled\n")
                            db.set_job_status(job_id, "cancelled", 1)
                        else:
                            if not isinstance(e, JobFailed):
                                log.exception("job %s crashed", job_id)
                            jl.write(f"\n!! Job failed: {e}\n")
                            db.set_job_status(job_id, "failed", 1)
                        if on_failure:
                            on_failure()
                        return
                    jl.write("\n==> Job finished successfully\n")
                    db.set_job_status(job_id, "succeeded", 0)
                    if on_success:
                        on_success()
        finally:
            _cancelled.discard(job_id)
            _logs.pop(job_id, None)

    log_path(job_id).write_text(f"Job {job_id}: {kind} {target}\n", encoding="utf-8")
    task = asyncio.get_running_loop().create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job_id


def cancel(job_id: int, by: str = "") -> bool:
    """Ask a queued or running job to stop; False if it's finished or was already asked.

    The running command gets one SIGTERM and is left to clean up after itself (Packer
    deletes its build VM). Only once: a second signal makes Packer quit without cleaning up.
    """
    job = db.get_job(job_id)
    if not job or job["status"] in FINISHED or job_id in _cancelled:
        return False
    _cancelled.add(job_id)
    note = f"\n==> Cancel requested{f' by {by}' if by else ''}; stopping and cleaning up\n"
    if jl := _logs.get(job_id):
        jl.write(note)
    else:
        with open(log_path(job_id), "a", encoding="utf-8") as fh:
            fh.write(note)
    if proc := _procs.get(job_id):
        proc.send_signal(signal.SIGTERM)
    return True


def cancelling(job_id: int) -> bool:
    return job_id in _cancelled


async def stream(job_id: int):
    """Yield complete log lines until the job is finished (for Server-Sent Events)."""
    path = log_path(job_id)
    pos = 0
    partial = ""
    while True:
        job = db.get_job(job_id)  # read status *before* the file, so no lines are missed
        if path.exists():
            with open(path, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
                pos = fh.tell()
            text = partial + data.decode("utf-8", errors="replace")
            *lines, partial = text.split("\n")
            for line in lines:
                yield line
        if not job or job["status"] in FINISHED:
            if partial:
                yield partial
            return
        await asyncio.sleep(0.5)
