import json
import stat

import pytest
from cryptography.hazmat.primitives import serialization

from app import packer


@pytest.fixture(autouse=True)
def no_legacy_key(monkeypatch, tmp_path):
    monkeypatch.setattr(packer, "LEGACY_KEY", tmp_path / "no-such-key")


def test_generated_key_is_private_and_stable(data_dir):
    path = packer.ensure_key()
    assert path == data_dir / "packer_ssh_key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    pub = packer.public_key()
    assert pub.startswith("ssh-ed25519 ") and pub.endswith(" vm-portal-build")
    assert packer.public_key() == pub  # not regenerated on every call
    assert packer.fingerprint().startswith("SHA256:")

from backup_helpers import vc


def _ubuntu():
    from app import images
    img = images.get("ubuntu-26.04")
    return img, images.config(img)


def test_packer_env_passes_matching_key_pair(data_dir):
    img, cfg = _ubuntu()
    env = packer.packer_env(img, cfg, "ubuntu-2604-docker-20261105-1200", vc())
    assert env["PKR_VAR_vm_name"] == "ubuntu-2604-docker-20261105-1200"
    assert env["PKR_VAR_vsphere_insecure"] in ("true", "false")
    assert env["PKR_VAR_keyboard_layout"] == "us" and env["PKR_VAR_ssh_username"] == "ubuntu"
    keys = json.loads(env["PKR_VAR_ssh_authorized_keys"])
    priv = serialization.load_ssh_private_key(open(env["PKR_VAR_ssh_private_key_file"], "rb").read(), None)
    pub = priv.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    assert keys == [pub.decode() + " vm-portal-build"]


def test_regenerate(data_dir):
    before = packer.public_key()
    packer.regenerate_key()
    assert packer.public_key() != before


def test_external_key_is_used_not_regenerated(data_dir, monkeypatch, tmp_path):
    ext = tmp_path / "ext_key"
    monkeypatch.setattr(packer, "LEGACY_KEY", ext)
    assert packer.key_source()[1]  # no mounted key -> the portal generates one
    ext.write_bytes(b"not a key")
    assert packer.key_source() == (ext, False)
    assert any("can't be used" in p for p in packer.preflight())
    with pytest.raises(ValueError):
        packer.regenerate_key()


def test_preflight_per_image(data_dir):
    img, cfg = _ubuntu()
    problems = packer.preflight(img, cfg, vc())
    assert any("installer ISO for vCenter" in p for p in problems)
    assert not any("SSH key" in p for p in problems)


def test_validation_covers_every_platform_without_contacting_one(data_dir):
    from app import editor, images
    env = editor.validation_env(images.get("ubuntu-26.04"))
    assert env["PKR_VAR_vsphere_server"] == "192.0.2.1" and env["PKR_VAR_proxmox_url"].startswith("https://192.0.2.1")
    assert env["PKR_VAR_iso_url"] and "SSL_CERT_FILE" not in env      # no certificate fetched from anywhere
    assert "vc-secret" not in str(env)                                  # nor the real backend's secret
