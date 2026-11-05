"""Settings that used to be environment-only: image editing, HTTPS-only cookies, the System tab."""
import re

from fastapi.testclient import TestClient

from app import config, db, images
from tests.test_web import client, csrf, login

PASSWORD = "test-password-123"


def save(c, section, **data):
    return c.post(f"/settings/{section}", data={"csrf_token": csrf(c, f"/settings/{section}"), **data})


def test_enabling_image_editing_needs_the_password():
    with client() as c:
        login(c)
        r = save(c, "templates", template_max_age_days="30", allow_image_editing="on")
        assert r.status_code == 400 and "Enter your password" in r.text
        assert not config.get_settings().allow_image_editing
        r = save(c, "templates", template_max_age_days="30", allow_image_editing="on", confirm_password="wrong")
        assert r.status_code == 400 and not config.get_settings().allow_image_editing
        r = save(c, "templates", template_max_age_days="30", allow_image_editing="on", confirm_password=PASSWORD)
        assert r.status_code == 200 and config.get_settings().allow_image_editing
        assert "Your password" not in r.text  # already on: no confirmation field
        # Turning it off again needs no password.
        r = save(c, "templates", template_max_age_days="30")
        assert r.status_code == 200 and not config.get_settings().allow_image_editing
    actions = [r["action"] for r in db.query("SELECT action FROM audit")]
    assert actions.count("settings.confirm_failed") == 2 and "settings.changed" in actions


def test_environment_locks_image_editing(monkeypatch):
    monkeypatch.setenv("ALLOW_IMAGE_EDITING", "false")
    config.reset_caches()
    with client() as c:
        login(c)
        page = c.get("/settings/templates").text
        assert re.search(r'name="allow_image_editing"[^>]*disabled', page)
        save(c, "templates", template_max_age_days="30", allow_image_editing="on", confirm_password=PASSWORD)
        assert not config.get_settings().allow_image_editing


def test_https_only_cannot_be_enabled_over_http():
    with client() as c:
        login(c)
        r = save(c, "security", https_only_cookies="on", confirm_password=PASSWORD)
        assert r.status_code == 400 and "HTTPS first" in r.text
        assert not config.get_settings().https_only_cookies


def test_https_only_over_https_sets_secure_cookies_and_hsts():
    from app.main import app
    with TestClient(app, base_url="https://testserver") as c:
        login(c)
        r = save(c, "security", https_only_cookies="on", confirm_password=PASSWORD)
        assert r.status_code == 200 and config.get_settings().https_only_cookies
        assert "Strict-Transport-Security" in r.headers
        r = c.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": csrf(c)},
                   follow_redirects=False)
        assert "secure" in r.headers["set-cookie"].lower()


def test_system_tab_lists_env_only_values_without_secrets():
    with client() as c:
        login(c)
        page = c.get("/settings/system").text
        for var in ("DATA_DIR", "ENCRYPTION_KEY", "SECRET_KEY", "PORT"):
            assert var in page
        assert "x" * 40 not in page and "vc-secret" not in page  # SECRET_KEY / vCenter password values
        assert "VSPHERE_SERVER" in page  # listed as an environment override


def test_packer_http_ip_setting():
    from app import packer
    with client() as c:
        login(c)
        r = save(c, "templates", template_max_age_days="30", packer_http_ip="not-an-ip")
        assert r.status_code == 400
        save(c, "templates", template_max_age_days="30", packer_http_ip="10.0.0.5")
    assert packer.http_ip() == "10.0.0.5"
    img = images.get("debian-13")
    env = packer.packer_env(img, images.config(img), "debian-13-docker-x")
    assert env["PKR_VAR_http_ip"] == "10.0.0.5"
