import asyncio

from app import db, jobs, packer


def test_redact():
    assert jobs.redact("pw=vc-secret!") == "pw=********!"


async def test_job_runs_and_streams(tmp_path):
    async def fn(job_id, log):
        await log.run(["sh", "-c", "echo hello; printf 'no-newline'"], cwd=tmp_path)

    job_id = jobs.submit("test", "t", fn, lock_key="t")
    lines = [line async for line in jobs.stream(job_id)]
    assert db.get_job(job_id)["status"] == "succeeded"
    assert "hello" in lines and "no-newline" in lines


async def test_job_failure_marks_failed(tmp_path):
    called = []

    async def fn(job_id, log):
        await log.run(["sh", "-c", "echo vc-secret; exit 3"], cwd=tmp_path)

    job_id = jobs.submit("test", "t", fn, lock_key="t2", on_failure=lambda: called.append(1))
    while db.get_job(job_id)["status"] not in ("succeeded", "failed"):
        await asyncio.sleep(0.05)
    assert db.get_job(job_id)["status"] == "failed"
    assert called == [1]
    text = jobs.log_path(job_id).read_text()
    assert "vc-secret" not in text and "exited with code 3" in text


def test_prune_keeps_newest():
    assert packer.prune_candidates(["c", "b", "a"], 2) == ["a"]
    assert packer.prune_candidates(["c"], 0) == []   # never delete the only template


def test_child_env_excludes_portal_secrets(monkeypatch):
    for k in ("ENCRYPTION_KEY", "SECRET_KEY", "ADMIN_PASSWORD", "VSPHERE_PASSWORD"):
        monkeypatch.setenv(k, "leak-me")
    env = jobs.child_env({"PKR_VAR_x": "1"})
    assert "leak-me" not in env.values() and env["PKR_VAR_x"] == "1" and "PATH" in env


async def _finished(job_id):
    while db.get_job(job_id)["status"] not in jobs.FINISHED:
        await asyncio.sleep(0.05)
    return db.get_job(job_id)["status"]


async def test_cancel_stops_the_running_command_once(tmp_path):
    called = []

    async def fn(job_id, log):
        # Cleans up on SIGTERM like Packer does; a second signal would skip the cleanup.
        await log.run(["sh", "-c", "trap 'echo cleaned-up; exit 1' TERM; echo started; sleep 30 & wait"], cwd=tmp_path)
        await log.run(["sh", "-c", "echo next-step"], cwd=tmp_path)

    job_id = jobs.submit("test", "t", fn, lock_key="c1", on_failure=lambda: called.append(1))
    while "started" not in jobs.log_path(job_id).read_text():
        await asyncio.sleep(0.05)
    assert jobs.cancel(job_id, by="admin") and jobs.cancelling(job_id)
    assert not jobs.cancel(job_id)                       # asked already: no second signal
    assert await _finished(job_id) == "cancelled"
    text = jobs.log_path(job_id).read_text()
    assert "Cancel requested by admin" in text and "cleaned-up" in text and "next-step" not in text
    assert called == [1]
    assert not jobs.cancelling(job_id) and not jobs.cancel(job_id)   # finished jobs stay as they are


async def test_cancel_a_job_that_waits_its_turn(tmp_path):
    ran = []

    async def slow(job_id, log):
        await log.run(["sh", "-c", "sleep 0.5"], cwd=tmp_path)

    async def never(job_id, log):
        ran.append(1)

    first = jobs.submit("test", "t", slow, lock_key="c2")
    second = jobs.submit("test", "t", never, lock_key="c2")
    assert jobs.cancel(second)
    assert await _finished(second) == "cancelled" and not ran
    assert await _finished(first) == "succeeded"
