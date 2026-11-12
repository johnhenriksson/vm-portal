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
    img = images.get("acme-1")
    with pytest.raises(ValueError):
        editor.resolve(img, "link.sh")
    assert "link.sh" not in editor.list_files(img)


def test_editing_is_off_by_default(data_dir):
    make_custom(data_dir)
    with client() as c:
        login(c)
        assert c.get("/images/defs/acme-1/files").status_code == 200  # viewing is fine
        r = c.post("/images/defs/acme-1/files/save", data={"path": "build.pkr.hcl", "content": "x",
                                                               "csrf_token": csrf(c, "/images")})
        assert r.status_code == 403
        assert (data_dir / "images/acme-1/build.pkr.hcl").read_text() == "# empty\n"


def test_builtin_is_read_only(editing):
    with client() as c:
        login(c)
        r = c.post("/images/defs/ubuntu-26.04/files/save", data={"path": "ubuntu.pkr.hcl", "content": "x",
                                                                  "csrf_token": csrf(c, "/images")})
        assert r.status_code == 403


def test_edit_snapshot_restore_and_audit(editing, data_dir, monkeypatch):
    monkeypatch.setattr(editor, "packer_check", lambda img: (True, "ok"))
    make_custom(data_dir)
    with client() as c:
        login(c)
        tok = csrf(c, "/images")
        r = c.post("/images/defs/acme-1/files/save", data={"path": "build.pkr.hcl", "content": "# v2\n",
                                                               "csrf_token": tok})
        assert r.status_code == 200 and (data_dir / "images/acme-1/build.pkr.hcl").read_text() == "# v2\n"
        snaps = list((data_dir / "images/.history/acme-1").iterdir())
        assert len(snaps) == 1 and (snaps[0] / "build.pkr.hcl").read_text() == "# empty\n"
        # An invalid manifest is refused and the old one kept.
        r = c.post("/images/defs/acme-1/files/save", data={"path": "image.yaml", "content": "guest: nope\n",
                                                               "csrf_token": tok})
        assert r.status_code == 400 and images.get("acme-1").usable
        c.post(f"/images/defs/acme-1/history/{snaps[0].name}/restore", data={"csrf_token": tok})
        assert (data_dir / "images/acme-1/build.pkr.hcl").read_text() == "# empty\n"
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
    img = images.get("acme-1")
    with pytest.raises(ValueError):
        editor.check_content(img, "big.sh", "x" * (editor.MAX_FILE + 1))
    with pytest.raises(ValueError):
        editor.check_content(img, "bin.sh", "a\x00b")


def test_create_vm_respects_image_features(data_dir, monkeypatch):
    from app.platforms import vsphere
    make_custom(data_dir, features=[])
    t = vsphere.VMInfo("acme-1-20261105-1200", True)
    monkeypatch.setattr(vsphere, "templates", lambda b: [t])
    with client() as c:
        login(c)
        page = c.get("/vms/new").text
        assert 'data-docker="no"' in page and "acme-1-20261105-1200" in page
        r = c.post("/vms", data={"name": "deb01", "template": t.name, "cpus": 2, "memory_mb": 2048,
                                 "disk_gb": 40, "compose_yaml": "services:\n  a:\n    image: x\n",
                                 "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "no Docker" in r.text


def test_disk_cannot_be_smaller_than_the_templates(data_dir, monkeypatch):
    """A clone's disk is never smaller than its template's, so the wizard doesn't offer it
    and the server refuses it, instead of quietly creating a bigger disk."""
    from app.platforms import vsphere
    make_custom(data_dir)
    known = vsphere.VMInfo("acme-1-20261105-1200", True, disk_gb=50)
    unknown = vsphere.VMInfo("acme-1-20261101-1200", True)       # falls back to the image's build size
    monkeypatch.setattr(vsphere, "templates", lambda b: [known, unknown])
    with client() as c:
        login(c)
        page = c.get("/vms/new").text
        assert 'data-min-disk="50"' in page and 'data-min-disk="25"' in page
        assert 'name="disk_gb" id="disk_gb" min="10" data-floor="10" value="50"' in page   # default raised to fit
        base = {"name": "deb01", "cpus": 2, "memory_mb": 2048, "ssh_keys": "ssh-ed25519 AAAAkey a@b"}
        r = c.post("/vms", data={**base, "template": known.name, "disk_gb": 40, "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "smaller than 50 GB" in r.text
        r = c.post("/vms", data={**base, "template": unknown.name, "disk_gb": 20, "csrf_token": csrf(c, "/vms/new")})
        assert r.status_code == 400 and "smaller than 25 GB" in r.text


def test_editor_form_targets_the_open_file(editing, data_dir):
    """The page's hidden path field must be the file, not the URL (regression)."""
    import re
    make_custom(data_dir)
    with client() as c:
        login(c)
        page = c.get("/images/defs/acme-1/files?path=build.pkr.hcl").text
        assert re.search(r'name="path" value="build.pkr.hcl"', page)
        assert "# empty" in page


def test_new_definition_from_a_starter_keeps_comments_and_skips_symlinks(editing, data_dir, monkeypatch):
    with client() as c:
        login(c)
        tok = csrf(c, "/images?tab=definitions")
        assert "New image definition" in c.get("/images?tab=definitions").text
        r = c.post("/images/defs/new", data={"source": "rocky-10", "new_id": "alma-10", "name": 'Alma "10"',
                                             "template_prefix": "alma-10", "csrf_token": tok},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/images/defs/alma-10/files"
        text = (data_dir / "images/alma-10/image.yaml").read_text()
        assert text.startswith("# Created from rocky-10") and "# the template uses net.ifnames=0" in text
        img = images.get("alma-10")
        assert img.usable and img.manifest.name == 'Alma "10"' and img.manifest.template_prefix == "alma-10"
        assert (data_dir / "images/alma-10/http/ks.pkrtpl.hcl").is_file()
        # Ids and prefixes stay unique.
        r = c.post("/images/defs/new", data={"source": "rocky-10", "new_id": "alma-11", "name": "x",
                                             "template_prefix": "alma-10", "csrf_token": tok})
        assert r.status_code == 400
        # A symlink planted in a custom definition is never copied (it could point at keys).
        (data_dir / "images/alma-10/leak.sh").symlink_to(data_dir / "encryption.key")
        c.post("/images/defs/new", data={"source": "alma-10", "new_id": "alma-12", "name": "x",
                                         "template_prefix": "alma-12", "csrf_token": tok})
        assert images.get("alma-12") and not (data_dir / "images/alma-12/leak.sh").exists()


def test_new_definition_needs_editing_enabled():
    with client() as c:
        login(c)
        r = c.post("/images/defs/new", data={"source": "rocky-10", "new_id": "alma-10", "name": "x",
                                             "template_prefix": "alma-10", "csrf_token": csrf(c, "/images")})
        assert r.status_code == 403
        assert "New image definition" not in c.get("/images?tab=definitions").text


def test_rename_manifest_falls_back_when_layout_is_unusual():
    text = "{name: a, template_prefix: b, os: {family: linux, distro: x}}\n"
    out = editor.rename_manifest(text, "New", "new-prefix")
    import yaml as _y
    data = _y.safe_load(out)
    assert data["name"] == "New" and data["template_prefix"] == "new-prefix" and data["os"]["distro"] == "x"
