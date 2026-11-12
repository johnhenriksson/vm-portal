import pytest
import yaml

from app import provision
from app.platforms import PLATFORMS
from app.provision import VMRequest

from backup_helpers import vc


def platform():
    return PLATFORMS["vsphere"]


def req(**kw):
    """As create() makes it: a static IP's network from the backend (conftest's pool)."""
    base = dict(name="app01", template="ubuntu-2604-docker-20261105-1200", cpus=2, memory_mb=4096, disk_gb=40)
    return provision.with_network(VMRequest(**{**base, **kw}), vc())


@pytest.mark.parametrize("name", ["a", "App01", "1app", "app_01", "app-", "a" * 33])
def test_bad_names(name):
    with pytest.raises(ValueError):
        provision.validate_name(name)


def test_good_name():
    assert provision.validate_name("app-01") == "app-01"


def test_ssh_keys():
    keys = provision.parse_ssh_keys("ssh-ed25519 AAAAC3Nz me@x\n\n# comment\nssh-rsa AAAAB3 other\n")
    assert keys == ["ssh-ed25519 AAAAC3Nz me@x", "ssh-rsa AAAAB3 other"]
    with pytest.raises(ValueError):
        provision.parse_ssh_keys("not a key")


def test_compose_validation():
    assert provision.validate_compose("") == ""
    assert provision.validate_compose("services:\n  a:\n    image: nginx") .endswith("\n")
    with pytest.raises(ValueError, match="services"):
        provision.validate_compose("foo: bar")
    with pytest.raises(ValueError, match="YAML"):
        provision.validate_compose("services: [")


def test_metadata_dhcp_has_no_network():
    """Templates the portal didn't build: cloud-init's own fallback, which works whatever
    renders their network."""
    md = yaml.safe_load(provision.render_metadata(req()))
    assert md["local-hostname"] == "app01"
    assert md["instance-id"].startswith("app01-")
    assert "network" not in md
    assert "network" not in yaml.safe_load(provision.render_metadata(req(image_id="debian-13", network_interface="eth0")))


def test_metadata_dhcp_on_netplan_images_keeps_its_address():
    """The DHCP client id is the NIC's MAC, not netplan's machine-id DUID, so the VM keeps
    its address across reboots (an Ubuntu VM came back from one with another address)."""
    md = yaml.safe_load(provision.render_metadata(req(image_id="ubuntu-26.04", network_interface="en*")))
    assert md["network"] == {"version": 2, "ethernets": {"nic0": {
        "match": {"name": "en*"}, "dhcp4": True, "dhcp-identifier": "mac"}}}


def test_metadata_static():
    md = yaml.safe_load(provision.render_metadata(req(ip="10.0.0.20")))
    eth = md["network"]["ethernets"]["nic0"]
    assert eth["addresses"] == ["10.0.0.20/24"]
    assert eth["routes"] == [{"to": "default", "via": "10.0.0.1"}]
    assert eth["nameservers"]["addresses"] == ["10.0.0.1", "1.1.1.1"]


def test_userdata_keys_and_compose():
    text = provision.render_userdata(req(ssh_keys=["ssh-ed25519 AAAAkey1 a@b", "ssh-ed25519 AAAAnew z@z"],
                                         compose_yaml="services:\n  a:\n    image: nginx\n"))
    assert text.startswith("#cloud-config\n")
    ud = yaml.safe_load(text)
    user = ud["users"][0]
    assert user["name"] == "ubuntu" and user["groups"] == ["docker"]
    assert user["ssh_authorized_keys"] == ["ssh-ed25519 AAAAkey1 a@b", "ssh-ed25519 AAAAkey2 c@d",
                                           "ssh-ed25519 AAAAnew z@z"]
    assert ud["write_files"][0]["path"] == "/opt/app/compose.yaml"
    assert ud["runcmd"] == [["docker", "compose", "-f", "/opt/app/compose.yaml", "up", "-d"]]


def test_tfvars_has_no_password():
    assert "vc-secret" not in str(provision.tfvars(req(), vc()))


def test_connection_settings_come_from_env_not_tfvars():
    # tfvars.json overrides TF_VAR_*, so a value frozen there would outlive a settings change.
    assert not set(platform().TF_CONNECTION_VARS) & provision.tfvars(req(), vc()).keys()
    env = provision.tf_env(vc())
    for k in platform().TF_CONNECTION_VARS:
        assert f"TF_VAR_{k}" in env
    assert env["TF_VAR_vsphere_insecure"] in ("true", "false")


def test_update_tfvars_can_drop_keys(tmp_path):
    import json
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps(
        {"vsphere_server": "old", "vsphere_insecure": True, "userdata": "keep"}))
    provision.update_tfvars(tmp_path, _drop=platform().TF_CONNECTION_VARS)
    assert json.loads((tmp_path / "terraform.tfvars.json").read_text()) == {"userdata": "keep"}


def test_console_password_hash_and_userdata():
    import bcrypt
    assert provision.hash_console_password("", "") is None
    with pytest.raises(ValueError):
        provision.hash_console_password("correct horse battery", "different horse battery")
    with pytest.raises(ValueError):
        provision.hash_console_password("short", "short")
    with pytest.raises(ValueError):
        provision.hash_console_password("ü" * 40, "ü" * 40)  # > 72 bytes
    h = provision.hash_console_password("correct horse battery", "correct horse battery")
    assert h.startswith("$2b$12$") and bcrypt.checkpw(b"correct horse battery", h.encode())

    ud = yaml.safe_load(provision.render_userdata(req(console_password_hash=h)))
    user = ud["users"][0]
    assert user["lock_passwd"] is False and user["hashed_passwd"] == h
    assert ud["ssh_pwauth"] is False
    assert "correct horse" not in provision.render_userdata(req(console_password_hash=h))
    no_docker = yaml.safe_load(provision.render_userdata(req(features=())))
    assert "groups" not in no_docker["users"][0]
    plain = yaml.safe_load(provision.render_userdata(req()))
    assert plain["users"][0]["lock_passwd"] is True and "hashed_passwd" not in plain["users"][0]


def test_metadata_redacts_userdata_in_vcenter():
    md = yaml.safe_load(provision.render_metadata(req()))
    assert md["redact"] == ["userdata"] and md["cleanup-guestinfo"] == ["userdata"]


def test_metadata_static_with_exact_interface_name():
    # Debian (ifupdown) and Rocky (NetworkManager) templates name the NIC eth0: no glob match.
    md = yaml.safe_load(provision.render_metadata(req(ip="10.0.0.20", network_interface="eth0")))
    eth = md["network"]["ethernets"]
    assert list(eth) == ["eth0"] and "match" not in eth["eth0"]
    assert eth["eth0"]["addresses"] == ["10.0.0.20/24"]
    # cloud-init's own converter (used for these renderers) fails on `to: default`: no network at all.
    assert eth["eth0"]["gateway4"] == "10.0.0.1" and "routes" not in eth["eth0"]


def test_vm_moid_from_terraform_state(data_dir):
    import json
    d = data_dir / "vms" / "web01"
    d.mkdir(parents=True)
    assert provision.vm_moid("web01") is None
    (d / "terraform.tfstate").write_text(json.dumps({"resources": [
        {"mode": "data", "type": "vsphere_virtual_machine", "name": "template",
         "instances": [{"attributes": {"moid": "vm-tmpl"}}]},
        {"mode": "managed", "type": "vsphere_virtual_machine", "name": "this",
         "instances": [{"attributes": {"moid": "vm-42"}}]}]}))
    assert provision.vm_moid("web01") == "vm-42"


def test_power_and_console_target_the_recorded_vm(monkeypatch):
    import pytest
    from app.platforms import vsphere

    class FakeVM:
        def __init__(self, moid, stub):
            self.moid, self.name = moid, {"vm-42": "web01"}.get(moid, "someone-else")
            self.config = type("C", (), {"template": False})()

    b = vc()
    monkeypatch.setattr(vsphere, "_connect", lambda b: type("SI", (), {"_stub": None})())
    monkeypatch.setattr(vsphere.vim, "VirtualMachine", FakeVM)
    assert vsphere._find_vm_locked(b, "web01", "vm-42").moid == "vm-42"
    with pytest.raises(LookupError):
        vsphere._find_vm_locked(b, "web01", "vm-77")  # the id now belongs to another VM

    # No id recorded: only an unambiguous, non-template match.
    rows = [{"name": "web01", "config.template": False, "_obj": "a"},
            {"name": "web01", "config.template": True, "_obj": "tmpl"}]
    monkeypatch.setattr(vsphere, "_collect", lambda b, props: rows)
    assert vsphere._find_vm_locked(b, "web01", None) == "a"
    rows.append({"name": "web01", "config.template": False, "_obj": "b"})
    with pytest.raises(LookupError, match="More than one"):
        vsphere._find_vm_locked(b, "web01", None)


def test_static_ip_network_comes_from_the_vms_backend(monkeypatch):
    """Not from global settings: since backends exist, those have no pool (a VM got no
    gateway and no DNS that way)."""
    from app import backends, config, db
    for k in ("IP_POOL_START", "IP_POOL_END", "IP_GATEWAY", "IP_PREFIX", "IP_DNS"):
        monkeypatch.delenv(k)
    config.get_settings.cache_clear()
    bid = backends.create("Lab", "vsphere", {"ip_pool_start": "10.20.30.100", "ip_pool_end": "10.20.30.200",
                                             "ip_gateway": "10.20.30.1", "ip_prefix": 23,
                                             "ip_dns": ["10.20.30.53"]}, by="t")
    r = provision.with_network(VMRequest(name="deb-01", template="t", cpus=1, memory_mb=1024, disk_gb=20,
                                         ip="10.20.30.152", network_interface="eth0"), backends.get(bid))
    eth = yaml.safe_load(provision.render_metadata(r))["network"]["ethernets"]["eth0"]
    assert eth["gateway4"] == "10.20.30.1" and eth["addresses"] == ["10.20.30.152/23"]
    assert eth["nameservers"]["addresses"] == ["10.20.30.53"]
    with pytest.raises(ValueError, match="no gateway"):
        provision.with_network(VMRequest(name="x01", template="t", cpus=1, memory_mb=1024, disk_gb=20,
                                         ip="10.0.0.5"), backends.get(backends.create("Bare", "vsphere", {}, by="t")))
    with pytest.raises(ValueError, match="gateway"):
        provision.render_metadata(VMRequest(name="x01", template="t", cpus=1, memory_mb=1024, disk_gb=20, ip="10.0.0.5"))
    assert db
