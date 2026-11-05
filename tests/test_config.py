from app.config import get_settings


def test_lists_from_env():
    s = get_settings()
    assert s.template_prefixes == ["ubuntu-2604-docker", "ubnt-packer"]
    assert s.ip_dns == ["10.0.0.1", "1.1.1.1"]
    assert s.default_ssh_keys == ["ssh-ed25519 AAAAkey1 a@b", "ssh-ed25519 AAAAkey2 c@d"]
