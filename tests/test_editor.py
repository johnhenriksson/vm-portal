import os

import pytest
import yaml

from app import editor, images
from tests.test_images import make_custom
from tests.test_web import client, csrf, login


@pytest.fixture
def editing(monkeypatch):
    from app import config
    monkeypatch.setenv("ALLOW_IMAGE_EDITING", "true")
    config.reset_caches()
    yield
    config.reset_caches()


@pytest.mark.parametrize("path", ["../x.sh", "/etc/passwd", "a/../../b.sh", ".hidden.sh", "a//b.sh",
                                  "secrets.auto.pkrvars.hcl", "x.pkrvars.hcl", "evil.exe", "a/b/c/d/e.sh", ""])
def test_bad_paths_are_rejected(path):
    with pytest.raises(ValueError):
        editor.check_path(path)


def test_good_paths():
    for p in ("build.pkr.hcl", "http/user-data", "scripts/setup.sh", "answer/autounattend.xml"):
        editor.check_path(p)


def test_symlinks_are_rejected(data_dir):
    d = make_custom(data_dir)
    (d / "link.sh").symlink_to("/etc/passwd")
    img = images.get("debian-13")
    with pytest.raises(ValueError):
        editor.resolve(img, "link.sh")
    assert "link.sh" not in editor.list_files(img)


def test_editing_is_off_by_default(data_dir):
    make_custom(data_dir)
    with client() as c:
        login(c)
        assert c.get("/images/defs/debian-13/files").status_code == 200  # viewing is fine
        r = c.post("/images/defs/debian-13/files/save", data={"path": "build.pkr.hcl", "content": "x",
                                                               "csrf_token": csrf(c, "/images")})
        assert r.status_code == 403
        assert (data_dir / "images/debian-13/build.pkr.hcl").read_text() == "# empty\n"


def test_builtin_is_read_only(editing):
    with client() as c:
        login(c)
        r = c.post("/images/defs/ubuntu-26.04/files/save", data={"path": "ubuntu.pkr.hcl", "content": "x",
                                                                  "csrf_token": csrf(c, "/images")})
        assert r.status_code == 403


def test_edit_snapshot_restore_and_audit(editing, data_dir, monkeypatch):
    monkeypatch.setattr(editor, "packer_syntax_check", lambda img: (True, "ok"))
    make_custom(data_dir)
    with client() as c:
        login(c)
        tok = csrf(c, "/images")
        r = c.post("/images/defs/debian-13/files/save", data={"path": "build.pkr.hcl", "content": "# v2\n",
                                                               "csrf_token": tok})
        assert r.status_code == 200 and (data_dir / "images/debian-13/build.pkr.hcl").read_text() == "# v2\n"
        snaps = list((data_dir / "images/.history/debian-13").iterdir())
        assert len(snaps) == 1 and (snaps[0] / "build.pkr.hcl").read_text() == "# empty\n"
        # An invalid manifest is refused and the old one kept.
        r = c.post("/images/defs/debian-13/files/save", data={"path": "image.yaml", "content": "guest: nope\n",
                                                               "csrf_token": tok})
        assert r.status_code == 400 and images.get("debian-13").usable
        c.post(f"/images/defs/debian-13/history/{snaps[0].name}/restore", data={"csrf_token": tok})
        assert (data_dir / "images/debian-13/build.pkr.hcl").read_text() == "# empty\n"
        from app import db
        actions = [r["action"] for r in db.query("SELECT action FROM audit")]
        assert "image.file_saved" in actions and "image.restored" in actions


def test_duplicate_and_delete(editing, data_dir):
    with client() as c:
        login(c)
        tok = csrf(c, "/images")
        r = c.post("/images/defs/ubuntu-26.04/duplicate", follow_redirects=False,
                   data={"new_id": "ubuntu-custom", "name": "My Ubuntu", "template_prefix": "my-ubuntu",
                         "csrf_token": tok})
        assert r.status_code == 303
        img = images.get("ubuntu-custom")
        assert img.usable and not img.builtin and img.manifest.template_prefix == "my-ubuntu"
        assert not list(img.path.glob("*.pkrvars.hcl"))  # the example is copied, real var files never
        r = c.post("/images/defs/ubuntu-26.04/duplicate",
                   data={"new_id": "ubuntu-custom", "name": "x", "template_prefix": "my-ubuntu", "csrf_token": tok})
        assert r.status_code == 400
        c.post("/images/defs/ubuntu-custom/delete", data={"confirm": "ubuntu-custom", "csrf_token": tok})
        assert images.get("ubuntu-custom") is None
        assert any((data_dir / "images/.deleted").iterdir())


def test_size_limits(editing, data_dir):
    make_custom(data_dir)
    img = images.get("debian-13")
    with pytest.raises(ValueError):
        editor.check_content(img, "big.sh", "x" * (editor.MAX_FILE + 1))
    with pytest.raises(ValueError):
        editor.check_content(img, "bin.sh", "a\x00b")


def test_create_vm_respects_image_features(data_dir, monkeypatch):
    from app import vsphere
    make_custom(data_dir, features=[])
    t = vsphere.VMInfo("debian-13-20261105-1200", True)
    monkeypatch.setattr(vsphere, "templates", lambda: [t])
    monkeypatch.setattr("app.main.vcenter_configured", lambda s=None: True)
    with client() as c:
        login(c)
        page = c.get("/vms/new").text
        assert 'data-docker="no"' in page and "debian-13-20261105-1200" in page
        r = c.post("/vms", data={"name": "deb01", "template": t.name, "cpus": 2, "memory_mb": 2048,
                                 "disk_gb": 40, "compose_yaml": "services:\n  a:\n    image: x\n",
                                 "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "no Docker" in r.text


def test_editor_form_targets_the_open_file(editing, data_dir):
    """The page's hidden path field must be the file, not the URL (regression)."""
    import re
    make_custom(data_dir)
    with client() as c:
        login(c)
        page = c.get("/images/defs/debian-13/files?path=build.pkr.hcl").text
        assert re.search(r'name="path" value="build.pkr.hcl"', page)
        assert "# empty" in page
