import json

import pytest
import yaml
from pydantic import ValidationError

from app import images

BASE = {"name": "Test", "os": {"family": "linux", "distro": "testos", "version": "1"},
        "guest": "cloud-init", "template_prefix": "acme-1"}


def make_custom(data_dir, image_id="acme-1", **manifest):
    d = data_dir / "images" / image_id
    d.mkdir(parents=True)
    (d / "image.yaml").write_text(yaml.safe_dump({**BASE, **manifest}))
    (d / "build.pkr.hcl").write_text("# empty\n")
    return d


def test_builtin_catalog_loads():
    img = images.get("ubuntu-26.04")
    assert img and img.builtin and img.usable, img.error
    assert img.manifest.template_prefix == "ubuntu-2604-docker"
    assert images.for_template("ubuntu-2604-docker-20261105-1200").id == "ubuntu-26.04"
    assert images.for_template("ubuntu-2604-dockerish-1") is None


@pytest.mark.parametrize("bad", [
    {"guest": "nope"},                                                   # unknown driver
    {"template_prefix": "Bad Prefix"},
    {"parameters": [{"name": "vsphere_password", "label": "x", "pattern": "^.*$"}]},  # reserved
    {"parameters": [{"name": "x", "label": "x", "pattern": ".*"}]},      # not anchored
    {"parameters": [{"name": "x", "label": "x", "pattern": "^a$", "default": "b"}]},
    {"surprise": True},                                                  # unknown key
])
def test_manifest_validation(bad):
    with pytest.raises(ValidationError):
        images.Manifest.model_validate({**BASE, **bad})


def test_custom_images_and_conflicts(data_dir):
    make_custom(data_dir)
    make_custom(data_dir, "ubuntu-26.04")                       # id clash with built-in
    make_custom(data_dir, "dup-prefix", template_prefix="acme-1")
    cat = images.catalog()
    by_id = {i.id: i for i in cat if i.builtin or i.id != "ubuntu-26.04"}
    assert by_id["acme-1"].usable and not by_id["acme-1"].builtin
    clash = next(i for i in cat if i.id == "ubuntu-26.04" and not i.builtin)
    assert "built-in" in clash.error and images.get("ubuntu-26.04").builtin
    cat = {i.id: i for i in cat if i.id != "ubuntu-26.04"}
    assert "already used" in cat["dup-prefix"].error
    assert "acme-1" in images.template_prefixes()


def test_parameters_are_validated():
    img = images.get("ubuntu-26.04")
    cfg = images.validate_config(img, {"params": {"keyboard_layout": "se"}})
    assert cfg.params["keyboard_layout"] == "se" and cfg.params["ssh_username"] == "ubuntu"
    for evil in ("se\nruncmd: [reboot]", "us; rm -rf /", ""):
        with pytest.raises(ValueError):
            images.validate_config(img, {"params": {"keyboard_layout": evil}})
    # Unknown parameters are dropped, not passed to Packer.
    assert "evil" not in images.validate_config(img, {"params": {"evil": "x"}}).params


def test_legacy_settings_migrate_to_ubuntu_image():
    from app import db
    db.save_settings({"packer_iso_url": "[ds] ISO/u.iso", "packer_cpus": 4, "template_keep": 5,
                      "vm_user": "ubnt", "vsphere_server": "vc"}, set(), by="test")
    images.migrate_legacy()
    cfg = images.config(images.get("ubuntu-26.04"))
    assert (cfg.iso_url, cfg.cpus, cfg.keep) == ("[ds] ISO/u.iso", 4, 5)
    assert cfg.params["ssh_username"] == "ubnt" and cfg.params["keyboard_layout"] == "se"
    assert db.load_settings()["vsphere_insecure"] is True  # old default kept for the existing vCenter
    images.migrate_legacy()  # idempotent


def test_fresh_install_has_no_migration():
    from app import db
    images.migrate_legacy()
    assert db.one("SELECT 1 FROM image_settings") is None


def test_current_install_with_vms_keeps_tls_verification():
    # A current install stores vsphere_insecure only when it differs from the secure default,
    # so VMs and a vm_user without any pre-catalog key must not count as legacy.
    from app import db
    db.save_settings({"vm_user": "ops", "vsphere_server": "vc"}, set(), by="test")
    db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,created_at)"
               " VALUES ('web01','ready','t',2,4096,40,'dhcp',?)", (db.now(),))
    images.migrate_legacy()
    assert "vsphere_insecure" not in db.load_settings()
    assert db.one("SELECT 1 FROM image_settings") is None


def test_all_builtin_definitions_are_valid():
    builtin = {i.id: i for i in images.catalog() if i.builtin}
    assert {"ubuntu-26.04", "debian-13", "rocky-10"} <= set(builtin)
    for img in builtin.values():
        assert img.usable, img.error
    assert builtin["debian-13"].manifest.network_interface == "eth0"
    assert builtin["rocky-10"].manifest.features == []
    assert builtin["ubuntu-26.04"].manifest.network_interface == "en*"
