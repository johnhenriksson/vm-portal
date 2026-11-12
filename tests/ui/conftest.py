"""Browser tests: the portal is served for real and driven in headless Chromium (Playwright),
for what the other tests can't see: app.js, htmx swaps, and how the pages are laid out.

The backends are fakes (the fake Proxmox API of test_proxmox; vSphere answers nothing), so
no test needs a real cluster.

They are skipped when Playwright or a browser that starts isn't there, unless UI_REQUIRED=1
(CI sets it). To run them where Chromium's system libraries are missing:
tests/ui/run-in-docker.sh. UI_SHOTS=<dir> keeps a screenshot of every test's last page.
"""
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

REQUIRED = os.environ.get("UI_REQUIRED") == "1"
SHOTS = Path(os.environ["UI_SHOTS"]).resolve() if os.environ.get("UI_SHOTS") else None    # tests change directory
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    if REQUIRED:
        raise
    sync_playwright = None
    collect_ignore_glob = ["test_*.py"]     # nothing here can run without it


def pytest_report_header():
    if sync_playwright is None:
        return "browser tests (tests/ui): not run, Playwright isn't installed (pip install -r requirements-dev.txt)"


from test_proxmox import b, cluster, pve  # noqa: E402,F401 - the fake Proxmox API and a backend on it

PASSWORD = "test-password-123"      # the first admin's, from the environment (tests/conftest.py)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no browser, or it can't start here
            if REQUIRED:
                raise
            pytest.skip(f"Chromium doesn't start here ({str(e).splitlines()[0]}); see tests/ui/conftest.py")
        yield chromium
        chromium.close()


@pytest.fixture(autouse=True)
def quiet_vcenter(monkeypatch):
    """The environment's vCenter doesn't exist: it has no templates and no VMs."""
    from app.platforms import vsphere
    monkeypatch.setattr(vsphere, "templates", lambda b: [])
    monkeypatch.setattr(vsphere, "vms_by_name", lambda b, fresh=False: {})


@pytest.fixture
def portal(data_dir):
    """The app on a local port, with this test's (empty) data."""
    import uvicorn
    from app.main import app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the portal didn't start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture
def page(browser, portal, request):
    """A logged-in admin's browser tab. The test fails if the page logs a JavaScript error."""
    context = browser.new_context(base_url=portal, viewport={"width": 1280, "height": 900})
    context.set_default_timeout(10_000)
    tab = context.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(f"{e} ({(e.stack or '').splitlines()[1].strip() if e.stack and len(e.stack.splitlines()) > 1 else 'no stack'})"))
    tab.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    tab.goto("/login")
    tab.fill("input[name=username]", "admin")
    tab.fill("input[name=password]", PASSWORD)
    tab.click("form button")
    tab.wait_for_url(re.compile(r"/$"))
    yield tab
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        tab.screenshot(path=str(SHOTS / f"{request.node.name}.png"), full_page=True)
    context.close()
    # A 4xx/5xx the test asked for is logged as "Failed to load resource": not a script error.
    errors = [e for e in errors if "Failed to load resource" not in e]
    assert not errors, f"JavaScript errors on the page: {errors}"


def box(locator) -> dict:
    """Where an element is drawn (x, y, width, height), rounded."""
    return {k: round(v, 1) for k, v in locator.first.bounding_box().items()}


def style(locator, prop: str) -> str:
    return locator.first.evaluate("(e, p) => getComputedStyle(e).getPropertyValue(p)", prop)


def add_vm(backend, name: str = "web-01") -> None:
    """A VM the portal manages on the backend (web-01 runs on the fake cluster)."""
    from app import db
    db.execute("INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, created_at, backend_id)"
               " VALUES (?, 'ready', 't', 1, 1024, 25, 'dhcp', ?, ?)", (name, db.now(), backend.id))
