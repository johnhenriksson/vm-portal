"""cloud-init via VMware guestinfo (Linux templates with cloud-init's VMware datasource)."""
import uuid

import yaml

from ..config import get_settings


class CloudInit:
    name = "cloud-init"
    label = "cloud-init (Linux)"
    supports_static_ip = True
    supports_console_password = True

    def tfvars(self, req) -> dict[str, str]:
        return {"metadata": self.metadata(req), "userdata": self.userdata(req)}

    def metadata(self, req) -> str:
        s = get_settings()
        md: dict = {
            # Unique per VM, so cloud-init always treats a clone as a new instance.
            "instance-id": f"{req.name}-{uuid.uuid4().hex[:8]}",
            "local-hostname": req.name,
            # Have cloud-init blank guestinfo.userdata in vCenter after reading it, so the
            # (hashed) console password and keys aren't left in the VM's advanced settings.
            "redact": ["userdata"],
            "cleanup-guestinfo": ["userdata"],  # name used by older cloud-init versions
        }
        if req.ip:
            iface = req.network_interface
            if iface.endswith("*"):
                # netplan (Ubuntu): cloud-init hands the config to netplan, which matches a glob.
                key, extra = "nic0", {"match": {"name": iface}, "routes": [{"to": "default", "via": s.ip_gateway}]}
            else:
                # ifupdown (Debian), NetworkManager/sysconfig (RHEL family): cloud-init converts the
                # config itself. Its converter rejects `to: default`, which leaves the VM with no
                # network at all, and renders routes for ifupdown with `route` (net-tools, not
                # installed). gateway4 becomes each renderer's native gateway setting. These
                # renderers only use the key as the interface name, so images give the exact name.
                key, extra = iface, {"gateway4": s.ip_gateway}
            md["network"] = {
                "version": 2,
                "ethernets": {
                    key: {
                        "dhcp4": False,
                        "addresses": [f"{req.ip}/{s.ip_prefix}"],
                        **extra,
                        "nameservers": {"addresses": s.ip_dns or [s.ip_gateway]},
                    }
                },
            }
        return yaml.safe_dump(md, sort_keys=False)

    def userdata(self, req) -> str:
        s = get_settings()
        keys = list(dict.fromkeys([*s.default_ssh_keys, *req.ssh_keys]))  # dedupe, keep order
        user: dict = {
            # Configure the template's existing user; listing users without "default"
            # stops cloud-init from also creating its distro default user.
            "name": req.login_user,
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "shell": "/bin/bash",
            "lock_passwd": True,
            "ssh_authorized_keys": keys,
        }
        if "docker" in req.features:
            user["groups"] = ["docker"]
        if req.console_password_hash:
            user.update(lock_passwd=False, hashed_passwd=req.console_password_hash)
        ud: dict = {
            "hostname": req.name,
            "fqdn": req.name,
            "manage_etc_hosts": True,
            "users": [user],
            "ssh_pwauth": False,  # SSH stays key-only, even with a console password
        }
        if req.compose_yaml:
            ud["write_files"] = [{"path": "/opt/app/compose.yaml", "permissions": "0644",
                                  "content": req.compose_yaml}]
            ud["runcmd"] = [["docker", "compose", "-f", "/opt/app/compose.yaml", "up", "-d"]]
        return "#cloud-config\n" + yaml.safe_dump(ud, sort_keys=False)
