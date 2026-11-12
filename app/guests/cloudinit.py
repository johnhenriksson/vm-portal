"""cloud-init for Linux templates: the metadata and user-data a new VM gets. On vSphere they
travel as guestinfo (cloud-init's VMware datasource); on Proxmox VE on a NoCloud seed ISO
(platforms/proxmox.py turns this metadata into its files)."""
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
        md: dict = {
            # Unique per VM, so cloud-init always treats a clone as a new instance.
            "instance-id": f"{req.name}-{uuid.uuid4().hex[:8]}",
            "local-hostname": req.name,
            # Have cloud-init blank guestinfo.userdata in vCenter after reading it, so the
            # (hashed) console password and keys aren't left in the VM's advanced settings.
            "redact": ["userdata"],
            "cleanup-guestinfo": ["userdata"],  # name used by older cloud-init versions
        }
        iface = req.network_interface
        if not req.ip and req.image_id and iface.endswith("*"):
            # DHCP on the portal's netplan images (Ubuntu): ask with the NIC's MAC as the client
            # id. netplan's default is a DUID made from the machine-id, and a VM came back
            # from a reboot with another address that way. The MAC stays the VM's for life.
            # Only for the portal's own images: other templates keep cloud-init's fallback,
            # which works whatever renders their network.
            md["network"] = {"version": 2, "ethernets": {"nic0": {
                "match": {"name": iface}, "dhcp4": True, "dhcp-identifier": "mac"}}}
        if req.ip:
            # The network of the VM's backend (its pool settings), never a global one.
            if not req.ip_gateway:
                raise ValueError("A static IP needs the backend's gateway")
            dns = [x for x in req.ip_dns if x] or [req.ip_gateway]
            if iface.endswith("*"):
                # netplan (Ubuntu): cloud-init hands the config to netplan, which matches a glob.
                key, extra = "nic0", {"match": {"name": iface}, "routes": [{"to": "default", "via": req.ip_gateway}]}
            else:
                # ifupdown (Debian), NetworkManager/sysconfig (RHEL family): cloud-init converts the
                # config itself. Its converter rejects `to: default`, which leaves the VM with no
                # network at all, and renders routes for ifupdown with `route` (net-tools, not
                # installed). gateway4 becomes each renderer's native gateway setting. These
                # renderers only use the key as the interface name, so images give the exact name.
                key, extra = iface, {"gateway4": req.ip_gateway}
            md["network"] = {
                "version": 2,
                "ethernets": {
                    key: {
                        "dhcp4": False,
                        "addresses": [f"{req.ip}/{req.ip_prefix}"],
                        **extra,
                        "nameservers": {"addresses": dns},
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
