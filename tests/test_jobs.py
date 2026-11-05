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
