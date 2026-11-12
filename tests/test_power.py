"""Power actions say what they're doing: in progress, done, or not happening."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import db, power, provision
from app.platforms import vsphere

from backup_helpers import make_vm
from tests.test_web import client, csrf, login

BOOT = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)


def info(state="poweredOn", tools=True, booted=BOOT):
    return vsphere.VMInfo("web-01", False, state, "10.0.0.11", tools_running=tools, booted=booted)


def pending(action, before=None, **kw):
    vm = {"id": 1, "name": "web-01", "backend_id": None}
    p = power.start(vm, action, "alice", before or info(**kw))
    assert p is not None
    return p


# --- deciding when an action has happened -------------------------------------------------

def test_reboot_is_done_once_the_tools_went_down_and_came_back():
    """vSphere: a guest reboot keeps the VM powered on and its boot time may not move."""
    p = pending("reboot")
    assert not power.judge(p, info())                       # not gone down yet
    assert not power.judge(p, info(tools=False))
    assert power.judge(p, info())


def test_reboot_is_done_once_the_guest_booted_again():
    p = pending("reboot")
    assert not power.judge(p, info(booted=BOOT + timedelta(seconds=5)))   # Proxmox's uptime jitter
    assert power.judge(p, info(booted=BOOT + timedelta(minutes=1)))
    p = pending("reboot", booted=None)                      # boot time unknown before: since the click
    assert power.judge(p, info(booted=datetime.now(timezone.utc)))


def test_reboot_without_guest_tools_waits_for_the_boot_time():
    p = pending("reboot", tools=False)
    assert not power.judge(p, info(tools=False))            # tools never answer: that proves nothing
    assert not power.judge(p, info("poweredOff", tools=False))
    assert power.judge(p, info(tools=False, booted=BOOT + timedelta(minutes=1)))


def test_shutdown_and_power_on():
    p = pending("shutdown")
    assert not power.judge(p, info()) and power.judge(p, info("poweredOff", tools=False))
    p = pending("on", info("poweredOff", tools=False))
    assert not power.judge(p, info(tools=False)) and power.judge(p, info())
    assert not power.judge(p, None)                         # not found: keep waiting
    assert power.start({"id": 1, "name": "web-01"}, "off", "alice") is None     # immediate
    assert power.get(1) is None


def test_tick_finishes_or_times_out(monkeypatch):
    make_vm()
    vm = db.active_vm("web-01")
    live = {"web-01": info()}
    monkeypatch.setattr(vsphere, "vms_by_name", lambda b, fresh=False: live)
    p = power.start(vm, "shutdown", "alice", info())
    asyncio.run(power.tick())
    assert p.running
    live["web-01"] = info("poweredOff", tools=False)
    asyncio.run(power.tick())
    assert p.outcome == "done" and "Shut down" in p.message and power.running(vm["id"]) is None

    p = power.start(vm, "reboot", "alice", info())
    live["web-01"] = info()
    p.started -= power.TIMEOUT_SECONDS + 1
    asyncio.run(power.tick())
    assert p.outcome == "timeout" and "hasn't reported back 5 minutes" in p.message
    p.finished -= power.KEEP_SECONDS + 1
    assert power.get(vm["id"]) is None                      # forgotten after a while


def test_timeout_applies_even_when_nothing_watches():
    p = pending("reboot")
    p.started -= power.TIMEOUT_SECONDS + 60
    assert power.get(1).outcome == "timeout"


# --- the VM page ------------------------------------------------------------------------

@pytest.fixture
def vm(monkeypatch):
    make_vm()
    state = {"live": info(), "calls": []}
    monkeypatch.setattr(vsphere, "vms_by_name", lambda b, fresh=False: {"web-01": state["live"]})
    monkeypatch.setattr(vsphere, "templates", lambda b: [])
    monkeypatch.setattr(vsphere, "autostart", lambda b, name, moid: vsphere.Autostart(False, "esx1", True))
    monkeypatch.setattr(vsphere, "power", lambda b, name, action, moid: state["calls"].append(action))
    monkeypatch.setattr(vsphere, "set_autostart", lambda b, name, moid, on: vsphere.Autostart(on, "esx1", True))
    monkeypatch.setattr(power, "_ensure_watching", lambda: None)     # the test drives it
    return state


def sig_of(page):
    return re.search(r'hx-get="/vms/web-01/status\?sig=([^"]+)"', page).group(1).replace("%7C", "|")


def test_reboot_shows_progress_then_the_result(vm):
    with client() as c:
        login(c, "alice", "correct horse battery")
        tok = csrf(c, "/vms/web-01")
        r = c.post("/vms/web-01/power/reboot", data={"csrf_token": tok}, follow_redirects=False)
        assert r.status_code == 303 and vm["calls"] == ["reboot"]
        page = c.get("/vms/web-01").text
        assert "Rebooting… waiting for the guest to come back" in page and 'every 3s' in page
        assert "Only a hard power off" in page and 'action="/vms/web-01/power/reboot"' not in page
        assert "Rebooting…" in c.get("/partials/vms").text                         # the VM list too

        r = c.post("/vms/web-01/power/reboot", data={"csrf_token": tok})
        assert r.status_code == 409 and "already in progress" in r.text and vm["calls"] == ["reboot"]

        sig = sig_of(page)
        r = c.get(f"/vms/web-01/status?sig={sig}", headers={"HX-Request": "true"})
        assert "HX-Refresh" not in r.headers and "Rebooting…" in r.text

        p = power.get(db.active_vm("web-01")["id"])
        power.finish(p, "done")
        r = c.get(f"/vms/web-01/status?sig={sig}", headers={"HX-Request": "true"})
        assert r.headers.get("HX-Refresh") == "true"
        page = c.get("/vms/web-01").text
        assert "Rebooted: the guest is back up." in page and "every 15s" in page
        assert page.count("Rebooted: the guest") == 1                               # once, not twice
        assert "Rebooted" not in c.get("/vms/web-01").text                          # and only once


def test_shutdown_that_never_happens_says_what_to_do(vm):
    with client() as c:
        login(c, "alice", "correct horse battery")
        c.post("/vms/web-01/power/shutdown", data={"csrf_token": csrf(c, "/vms/web-01")})
        page = c.get("/vms/web-01").text
        p = power.get(db.active_vm("web-01")["id"])
        power.finish(p, "timeout")
        c.get(f"/vms/web-01/status?sig={sig_of(page)}", headers={"HX-Request": "true"})
        assert "Use Power off (hard) to force it" in c.get("/vms/web-01").text


def test_hard_power_off_is_allowed_during_a_shutdown(vm):
    with client() as c:
        login(c, "alice", "correct horse battery")
        tok = csrf(c, "/vms/web-01")
        c.post("/vms/web-01/power/shutdown", data={"csrf_token": tok})
        r = c.post("/vms/web-01/power/off", data={"csrf_token": tok})
        assert r.status_code == 200 and "Powered off." in r.text and vm["calls"] == ["shutdown", "off"]
        assert power.get(db.active_vm("web-01")["id"]) is None


def test_guest_actions_need_the_tools_on_vsphere(vm):
    vm["live"] = info(tools=False)
    with client() as c:
        login(c, "alice", "correct horse battery")
        page = c.get("/vms/web-01").text
        assert "Guest tools aren't answering" in page and 'action="/vms/web-01/power/off"' in page
        r = c.post("/vms/web-01/power/reboot", data={"csrf_token": csrf(c, "/vms/web-01")})
        assert r.status_code == 409 and "Use Power off (hard) instead" in r.text and vm["calls"] == []
        assert "web-01" in r.text and "Overview" in r.text                  # on the VM page, not an error page


def test_failures_stay_on_the_vm_page(vm, monkeypatch):
    def refused(b, name, action, moid):
        raise RuntimeError("vCenter says no")
    monkeypatch.setattr(vsphere, "power", refused)
    with client() as c:
        login(c, "alice", "correct horse battery")
        r = c.post("/vms/web-01/power/reboot", data={"csrf_token": csrf(c, "/vms/web-01")})
        assert r.status_code == 502 and "vCenter says no" in r.text and "Overview" in r.text
        assert power.get(db.active_vm("web-01")["id"]) is None


def test_resize_and_destroy_wait_for_a_power_action(vm, monkeypatch):
    monkeypatch.setattr(provision, "resize", lambda *a, **k: pytest.fail("resized during a reboot"))
    with client() as c:
        login(c, "alice", "correct horse battery")
        tok = csrf(c, "/vms/web-01")
        c.post("/vms/web-01/power/reboot", data={"csrf_token": tok})
        r = c.post("/vms/web-01/resize", data={"cpus": 4, "memory_mb": 4096, "disk_gb": 40, "restart": "on",
                                               "csrf_token": tok})
        assert r.status_code == 409 and "Rebooting is in progress" in r.text
        assert 'name="cpus" min="1" max="32" value="4"' in r.text and "name=\"restart\" checked" in r.text
        r = c.post("/vms/web-01/destroy", data={"confirm": "web-01", "csrf_token": tok})
        assert r.status_code == 409 and "Rebooting is in progress" in r.text


def test_actions_say_they_worked_once(vm):
    with client() as c:
        login(c, "alice", "correct horse battery")
        tok = csrf(c, "/vms/web-01")
        r = c.post("/vms/web-01/autostart", data={"enabled": "on", "csrf_token": tok})
        assert "Autostart is on: web-01 starts with host esx1." in r.text
        assert "Autostart is on" not in c.get("/vms/web-01").text
        r = c.post("/vms/web-01/details", data={"notes": "new notes", "csrf_token": tok})
        assert "Saved the notes." in r.text
        r = c.post("/vms/web-01/resize", data={"cpus": 2, "memory_mb": 4096, "disk_gb": 40, "csrf_token": tok})
        assert "Nothing to change" in r.text


def test_polled_partials_leave_the_message_for_the_page(vm):
    with client() as c:
        login(c, "alice", "correct horse battery")
        tok = csrf(c, "/vms/web-01")
        c.post("/vms/web-01/autostart", data={"enabled": "on", "csrf_token": tok}, follow_redirects=False)
        assert "Autostart is on" not in c.get("/partials/vms", headers={"HX-Request": "true"}).text
        assert "Autostart is on" in c.get("/vms/web-01").text
