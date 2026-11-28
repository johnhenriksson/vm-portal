"""cloud-init for Linux templates: the metadata and user-data a new VM gets. On vSphere they
travel as guestinfo (cloud-init's VMware datasource); on Proxmox VE on a NoCloud seed ISO
(platforms/proxmox.py turns this metadata into its files)."""
import uuid

import yaml

from ..config import get_settings

# vSphere can't read files in a guest without the guest's own login, so there the VM reports
# its first boot itself: a one-shot service, ordered after cloud-final, puts a capped JSON
# summary into guestinfo with VMware Tools; the portal reads it from the VM's advanced
# settings and then blanks it (it can hold log lines). No route from the VM to the portal.
REPORT_KEY = "guestinfo.vmportal.firstboot"
REPORT_SCRIPT = "/usr/local/lib/vm-portal/firstboot-report"
REPORT_UNIT = "vm-portal-firstboot.service"
_REPORTER = r'''#!/usr/bin/python3
# vm-portal: hand cloud-init's outcome to the portal through VMware guestinfo, once.
import json, os, re, shutil, subprocess, sys, time

RESULT = "/run/cloud-init/result.json"
LOG = "/var/log/cloud-init-output.log"
KEY = "%(key)s"
LIMIT = 12000
FINISHED = re.compile(r"^Cloud-init v\. \S+ finished at ")

rpctool = shutil.which("vmware-rpctool")
if not rpctool:
    sys.exit("no vmware-rpctool (open-vm-tools): can't report")
for _ in range(120):            # cloud-final is done when this runs; allow a slow disk
    if os.path.exists(RESULT):
        break
    time.sleep(5)
try:
    with open(RESULT) as f:
        errors = ((json.load(f) or {}).get("v1") or {}).get("errors") or []
except (OSError, ValueError, AttributeError) as e:
    sys.exit("no readable %%s: %%s" %% (RESULT, e))
errors = [str(e)[:500] for e in (errors if isinstance(errors, list) else [errors])][:10]
lines = []
if errors:
    try:
        with open(LOG, errors="replace") as f:
            lines = f.read().splitlines()
        # Up to cloud-init's "finished" line: what follows (SSH host keys) would hide the error.
        ends = [i for i, line in enumerate(lines) if FINISHED.match(line)]
        lines = [line[:300] for line in (lines[:ends[-1] + 1] if ends else lines)[-20:]]
    except OSError:
        pass
while True:
    report = json.dumps({"v": 1, "errors": errors, "log": "\n".join(lines)})
    if len(report) <= LIMIT:
        break
    # Shrink, never cut: the portal must get valid JSON. Log lines go first, then errors.
    if lines:
        lines.pop(0)
    elif len(errors) > 1:
        errors.pop()
    else:
        errors = [errors[0][:100]]
subprocess.run([rpctool, "info-set %%s %%s" %% (KEY, report)], check=True)
''' % {"key": REPORT_KEY}
_REPORT_UNIT_TEXT = f"""[Unit]
Description=vm-portal: report the first-boot result through VMware guestinfo
After=cloud-final.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {REPORT_SCRIPT}
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=yes
"""


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
        files: list[dict] = []
        runcmd: list[list[str]] = []
        if req.firstboot_report == "guestinfo":
            files += [{"path": REPORT_SCRIPT, "permissions": "0755", "content": _REPORTER},
                      {"path": f"/etc/systemd/system/{REPORT_UNIT}", "permissions": "0644",
                       "content": _REPORT_UNIT_TEXT}]
            # First, so a failing command after it can't stop it. --no-block: the job waits
            # for cloud-final (which runs runcmd) to finish, so result.json is complete.
            runcmd += [["systemctl", "daemon-reload"], ["systemctl", "start", "--no-block", REPORT_UNIT]]
        if req.compose_yaml:
            files.append({"path": "/opt/app/compose.yaml", "permissions": "0644", "content": req.compose_yaml})
            runcmd.append(["docker", "compose", "-f", "/opt/app/compose.yaml", "up", "-d"])
        if files:
            ud["write_files"] = files
        if runcmd:
            ud["runcmd"] = runcmd
        return "#cloud-config\n" + yaml.safe_dump(ud, sort_keys=False)
