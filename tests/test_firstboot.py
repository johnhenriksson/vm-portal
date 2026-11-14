"""The first-boot watcher, against a stand-in platform (each platform's reader has its own tests)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import backends, db, firstboot


class Platform:
    """Answers what `answers` holds next: a Result, None (nothing yet) or an exception."""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0

    def first_boot(self, b, name, vm_id):
        self.asked += 1
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture
def vm(monkeypatch):
    p = Platform(None)

    class B:
        platform = p
        configured = True
        name = "test"
    monkeypatch.setattr(backends, "of_vm", lambda vm: B)
    vid = db.execute("INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, created_at,"
                     " firstboot_state, firstboot_since) VALUES ('app-01', 'ready', 't', 1, 1024, 10, 'dhcp', ?,"
                     " 'waiting', ?)", (db.now(), db.now()))
    return vid, p


def state(vid):
    return db.one("SELECT firstboot_state, firstboot_detail FROM vms WHERE id=?", (vid,))


def test_parse_result():
    assert firstboot.parse_result('{"v1": {"errors": []}}').ok
    r = firstboot.parse_result(json.dumps({"v1": {"errors": ["x" * 900] * 50}}))
    assert not r.ok and len(r.errors) == firstboot.MAX_ERRORS and len(r.errors[0]) == 500
    with pytest.raises(ValueError):
        firstboot.parse_result("not json")


def test_waits_then_records_the_outcome(vm):
    vid, p = vm
    asyncio.run(firstboot.tick())
    assert state(vid)["firstboot_state"] == "waiting"
    p.answers = [firstboot.Result(ok=False, errors=["boom"], log="last line")]
    asyncio.run(firstboot.tick())
    s = state(vid)
    assert s["firstboot_state"] == "failed" and json.loads(s["firstboot_detail"]) == {"errors": ["boom"], "log": "last line"}
    asyncio.run(firstboot.tick())
    assert p.asked == 2                                                  # never asked again once known


def test_not_ready_vms_are_not_asked(vm):
    vid, p = vm
    db.execute("UPDATE vms SET status='provisioning' WHERE id=?", (vid,))
    asyncio.run(firstboot.tick())
    assert p.asked == 0


def test_gives_up_after_the_timeout_and_can_start_again(vm):
    vid, p = vm
    old = (datetime.now(timezone.utc) - firstboot.TIMEOUT - timedelta(minutes=1)).isoformat()
    db.execute("UPDATE vms SET firstboot_since=? WHERE id=?", (old, vid))
    asyncio.run(firstboot.tick())
    s = state(vid)
    assert s["firstboot_state"] == "unknown" and "30 minutes" in json.loads(s["firstboot_detail"])["reason"]
    firstboot.restart_watch(vid)
    assert state(vid)["firstboot_state"] == "waiting"
    p.answers = [firstboot.Result(ok=True)]
    asyncio.run(firstboot.tick())
    assert state(vid)["firstboot_state"] == "done"


def test_a_missing_permission_is_unknown_at_once(vm):
    vid, p = vm
    p.answers = [firstboot.Unreadable("give it FileRead")]
    asyncio.run(firstboot.tick())
    s = state(vid)
    assert s["firstboot_state"] == "unknown" and json.loads(s["firstboot_detail"]) == {"reason": "give it FileRead"}


def test_a_backend_that_is_down_is_retried(vm):
    vid, p = vm
    p.answers = [OSError("connection refused"), firstboot.Result(ok=True)]
    asyncio.run(firstboot.tick())
    assert state(vid)["firstboot_state"] == "waiting"
    asyncio.run(firstboot.tick())
    assert state(vid)["firstboot_state"] == "done"
