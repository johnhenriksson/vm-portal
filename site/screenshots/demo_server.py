"""Run the real portal with made-up data, a fake vCenter and a fake Proxmox VE cluster, for
the site's screenshots and videos.

    .venv/bin/python site/screenshots/demo_server.py /path/to/empty-scratch-dir [port]

Everything here is invented: documentation IP ranges (192.0.2.0/24, 198.51.100.0/24),
example.com names, fictional users. The fake vCenter and Proxmox cluster live only in this
script; the portal itself has no demo mode. Never point this at a real data directory: it writes to the one it's given.
"""
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

data = Path(sys.argv[1]).resolve()
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8099
if data.exists() and any(data.iterdir()):
    sys.exit(f"{data} is not empty; give an empty scratch directory")
data.mkdir(parents=True, exist_ok=True)
# Run from the scratch directory: the portal reads .env from the current directory, and a
# developer's .env (real vCenter, addresses, keys) must never end up in a screenshot or video.
os.chdir(data)

# Packer and xorriso aren't needed to render pages; stand-ins keep preflight checks quiet.
fake_bin = data / "bin"
fake_bin.mkdir()
for tool in ("packer", "xorriso"):
    (fake_bin / tool).write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / tool).chmod(0o755)

# A stand-in for OpenTofu, so a VM created in the demo (the video does) runs the portal's
# real create job and streams a believable log in a few seconds. Nothing leaves this machine.
FAKE_TOFU = '''
import json, os, pathlib, re, sys, time
cmd = sys.argv[1:2]
pve = "TF_VAR_proxmox_endpoint" in os.environ
def say(*lines, pause=0.0):
    for line in lines:
        print(line, flush=True)
        time.sleep(pause)
if cmd == ["init"] and pve:
    say("Initializing provider plugins...", "- Installing bpg/proxmox v0.121.0...",
        "- Installed bpg/proxmox v0.121.0 (signed)", "", "OpenTofu has been successfully initialized!", pause=0.25)
elif cmd == ["apply"] and pve:
    r = "proxmox_virtual_environment_vm.this"
    say(r + ": Creating...", pause=1.0)
    say(r + ": Still creating... [10s elapsed]", pause=1.0)
    say(r + ": Still creating... [20s elapsed]", pause=0.8)
    say(r + ": Creation complete after 27s [id=112]",
        "", "Apply complete! Resources: 1 added, 0 changed, 0 destroyed.", pause=0.2)
    pathlib.Path("terraform.tfstate").write_text(json.dumps({"version": 4, "resources": [
        {"mode": "managed", "type": "proxmox_virtual_environment_vm", "name": "this",
         "instances": [{"attributes": {"vm_id": 112, "node_name": "pve-01"}}]}]}))
elif cmd == ["init"]:
    say("Initializing modules...", "Initializing provider plugins...",
        "- Installing vmware/vsphere v2.17.0...", "- Installed vmware/vsphere v2.17.0 (signed)", "",
        "OpenTofu has been successfully initialized!", pause=0.25)
elif cmd == ["apply"]:
    say("data.vsphere_datacenter.this: Reading...", "data.vsphere_datacenter.this: Read complete after 0s",
        "data.vsphere_virtual_machine.template[0]: Read complete after 1s", "", pause=0.3)
    say("vsphere_virtual_machine.this: Creating...", pause=1.0)
    say("vsphere_virtual_machine.this: Still creating... [10s elapsed]", pause=1.0)
    say("vsphere_virtual_machine.this: Still creating... [20s elapsed]", pause=1.0)
    say("vsphere_virtual_machine.this: Still creating... [30s elapsed]", pause=0.8)
    say("vsphere_virtual_machine.this: Creation complete after 38s [id=4213c1f2-9d1e-8a6b-5c3f-1e2d3c4b5a69]",
        "", "Apply complete! Resources: 1 added, 0 changed, 0 destroyed.", pause=0.2)
    pathlib.Path("terraform.tfstate").write_text(json.dumps({"version": 4, "resources": [
        {"mode": "managed", "type": "vsphere_virtual_machine", "name": "this",
         "instances": [{"attributes": {"moid": "vm-1042"}}]}]}))
elif cmd == ["output"]:
    text = pathlib.Path("terraform.tfvars.json").read_text()
    ips = [ip for ip in re.findall(r"(?:192\\.0\\.2|198\\.51\\.100)\\.\\d+", text) if not ip.endswith(".1")]
    print(json.dumps({"ip": {"value": ips[0] if ips else ("198.51.100.44" if pve else "192.0.2.24")}}))
elif cmd == ["show"]:
    print("{}")
'''
(fake_bin / "tofu").write_text(f"#!{sys.executable}\n" + FAKE_TOFU)
(fake_bin / "tofu").chmod(0o755)

os.environ.update({
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "DATA_DIR": str(data),
    "SECRET_KEY": secrets.token_urlsafe(32),
    "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "demo-password-123",
    "VSPHERE_SERVER": "vcenter.example.com", "VSPHERE_USER": "svc-portal@vsphere.local",
    "VSPHERE_PASSWORD": "not-a-real-password", "VSPHERE_DATACENTER": "Datacenter",
    "VSPHERE_CLUSTER": "Cluster-01", "VSPHERE_DATASTORE": "vsanDatastore", "VSPHERE_NETWORK": "VM Network",
    "VSPHERE_FOLDER": "Portal",
    "IP_POOL_START": "192.0.2.20", "IP_POOL_END": "192.0.2.99", "IP_GATEWAY": "192.0.2.1",
    "IP_PREFIX": "24", "IP_DNS": "192.0.2.1",
    "DEFAULT_SSH_KEYS": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemoKeyOnlyForScreenshotsXXXXXXXXXXXXXXXX ops@example.com",
    "ALLOW_IMAGE_EDITING": "true",
})

from app import db, images, ipam, jobs, users  # noqa: E402
from app.platforms import proxmox, vsphere  # noqa: E402
from app.backup import schedule  # noqa: E402
from app.config import get_bootstrap, get_settings  # noqa: E402

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


def stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


TEMPLATES = [
    ("ubuntu-2604-docker-" + ago(days=3).strftime("%Y%m%d") + "-0300", ago(days=3)),
    ("ubuntu-2604-docker-" + ago(days=34).strftime("%Y%m%d") + "-0300", ago(days=34)),
    ("debian-13-docker-" + ago(days=1).strftime("%Y%m%d") + "-0300", ago(days=1)),
]
UBUNTU, _, DEBIAN = (t[0] for t in TEMPLATES)
# The Proxmox cluster has its own templates, built from the same definitions.
PVE_TEMPLATES = [
    ("rocky-10-" + ago(days=2).strftime("%Y%m%d") + "-0400", ago(days=2)),
    ("debian-13-docker-" + ago(days=1).strftime("%Y%m%d") + "-0400", ago(days=1)),
    ("ubuntu-2604-docker-" + ago(days=5).strftime("%Y%m%d") + "-0400", ago(days=5)),
]
PVE_ROCKY, PVE_DEBIAN, PVE_UBUNTU = (t[0] for t in PVE_TEMPLATES)
PVE_NODES = ["pve-01", "pve-02", "pve-03"]

# name, owner, template, image, cpus, mem, disk, ip, mode, power, age, compose, notes
VMS = [
    ("web-01", "alice", UBUNTU, "ubuntu-26.04", 2, 4096, 40, "192.0.2.21", "static", "poweredOn", 12, True,
     "Team wiki and docs site. Ask Alice before resizing."),
    ("ci-runner-01", "ops", UBUNTU, "ubuntu-26.04", 8, 16384, 160, "192.0.2.22", "static", "poweredOn", 30, True, ""),
    ("db-01", "alice", DEBIAN, "debian-13", 4, 8192, 80, "192.0.2.23", "static", "poweredOn", 1, False, ""),
    ("grafana", "bob", UBUNTU, "ubuntu-26.04", 2, 2048, 40, "192.0.2.57", "dhcp", "poweredOn", 6, True, ""),
    ("sandbox-bob", "bob", DEBIAN, "debian-13", 1, 2048, 20, "192.0.2.64", "dhcp", "poweredOff", 2, False, ""),
]
# The same columns, for the VMs on the Proxmox cluster; NODE says which node each is on.
PVE_VMS = [
    ("k3s-01", "ops", PVE_ROCKY, "rocky-10", 4, 8192, 80, "198.51.100.21", "static", "poweredOn", 9, False, ""),
    ("k3s-02", "ops", PVE_ROCKY, "rocky-10", 4, 8192, 80, "198.51.100.22", "static", "poweredOn", 9, False, ""),
    ("mqtt-01", "alice", PVE_DEBIAN, "debian-13", 1, 2048, 20, "198.51.100.23", "static", "poweredOn", 4, True, ""),
]
NODE = {"k3s-01": "pve-01", "k3s-02": "pve-02", "mqtt-01": "pve-03"}


# --- fake vCenter ---------------------------------------------------------------------

def _templates():
    return [vsphere.VMInfo(name, True, created=created) for name, created in TEMPLATES]


def _vms(b, rows=None):
    rows = VMS if rows is None else rows
    out = {}
    for name, *_rest in rows:
        ip, power = _rest[6], _rest[8]
        out[name] = vsphere.VMInfo(name, False, power_state=power, ip=ip if power == "poweredOn" else None,
                                   ips=[ip], tools_running=power == "poweredOn", host=NODE.get(name))
    for vm in db.active_vms():   # created during the demo: running, at the address the fake tofu gave it
        if vm["name"] not in out and vm["status"] == "ready" and vm["backend_id"] == b.id:
            out[vm["name"]] = vsphere.VMInfo(vm["name"], False, power_state="poweredOn", ip=vm["ip"],
                                             ips=[vm["ip"]] if vm["ip"] else [], tools_running=True)
    return out


ISOS = ["[vsanDatastore] iso/ubuntu-26.04-live-server-amd64.iso",
        "[vsanDatastore] iso/debian-13.2.0-amd64-netinst.iso",
        "[vsanDatastore] iso/Rocky-10.1-x86_64-minimal.iso"]

# Every call gets the backend (the one vCenter the environment above describes) first.
vsphere.templates = lambda b: _templates()
vsphere.vms_by_name = lambda b, fresh=False: _vms(b)
vsphere.all_vms = lambda b, fresh=False: [*_templates(), *_vms(b).values()]
vsphere.used_ips = lambda b: {vm[7] for vm in VMS}
vsphere.name_exists = lambda b, name: name in _vms(b)
vsphere.list_isos = lambda b: ISOS
vsphere.inventory = lambda *a, **kw: {
    "about": "VMware vCenter Server (simulated for screenshots)",
    "datacenters": [{"name": "Datacenter", "folders": ["Portal"], "clusters": [
        {"name": "Cluster-01", "networks": ["VM Network"],
         "datastores": [{"name": "vsanDatastore", "type": "vsan", "free_gb": 6120, "capacity_gb": 12288}]}]}]}
vsphere.power = lambda b, name, action, vm_id=None: None
vsphere.autostart = lambda b, name, moid=None: vsphere.Autostart(True, "esx-01.example.com", True)
vsphere.set_autostart = lambda b, name, moid, on: vsphere.Autostart(on, "esx-01.example.com", True)


# --- fake Proxmox VE cluster --------------------------------------------------------------

PVE_ISOS = ["cephfs:iso/ubuntu-26.04-live-server-amd64.iso", "cephfs:iso/debian-13.2.0-amd64-netinst.iso",
            "cephfs:iso/Rocky-10.1-x86_64-minimal.iso"]


def _pve_templates():
    return [proxmox.VMInfo(name, True, created=created, disk_gb=20, host="pve-01") for name, created in PVE_TEMPLATES]


def _pve_node(name):
    ceph = {"name": "ceph-vm", "type": "rbd", "content": ["images"], "free_gb": 3410, "capacity_gb": 5588,
            "shared": True}
    cephfs = {"name": "cephfs", "type": "cephfs", "content": ["backup", "iso", "vztmpl"], "free_gb": 3410,
              "capacity_gb": 5588, "shared": True}
    local = {"name": "local-lvm", "type": "lvmthin", "content": ["images", "rootdir"], "free_gb": 412,
             "capacity_gb": 464, "shared": False}
    return {"name": name, "online": True, "storages": [ceph, cephfs, local], "bridges": ["vmbr0", "vmbr1"]}


async def _pve_before_create(b, req, d, log):
    log.step(f"Uploading the first-boot seed ISO to {b.cfg.proxmox_iso_storage} on node "
             f"{getattr(req, 'location', None) or b.cfg.proxmox_node}")
    return {"template_vmid": 9002, "template_node": "pve-01", "bios": "ovmf", "disk_gb": req.disk_gb,
            "seed_iso": f"{b.cfg.proxmox_iso_storage}:iso/{proxmox.seed_names(req.name)[0]}"}


async def _pve_after_create(b, req, vm_id, log):
    log.step("Waiting for the VM's first boot, to remove the secrets from its seed ISO")
    log.write("Seed ISO replaced by a copy without keys, password hash or compose file\n")


proxmox.templates = lambda b: _pve_templates()
proxmox.vms_by_name = lambda b, fresh=False: _vms(b, PVE_VMS)
proxmox.all_vms = lambda b, fresh=False: [*_pve_templates(), *_vms(b, PVE_VMS).values()]
proxmox.used_ips = lambda b: {vm[7] for vm in PVE_VMS}
proxmox.name_exists = lambda b, name: name in _vms(b, PVE_VMS)
proxmox.list_isos = lambda b: PVE_ISOS
proxmox.locations = lambda b: PVE_NODES
proxmox.inventory = lambda *a, **kw: {
    "about": "Proxmox VE 9.2 (simulated for screenshots)", "nodes": [_pve_node(n) for n in PVE_NODES],
    "pools": ["portal"], "privileges": sorted([*proxmox.PRIVILEGES, "VM.GuestAgent.Audit"]),
    "missing_privileges": []}
proxmox.power = lambda b, name, action, vm_id=None: None
proxmox.autostart = lambda b, name, vm_id=None: proxmox.Autostart(True, NODE.get(name, "pve-01"), True)
proxmox.set_autostart = lambda b, name, vm_id, on: proxmox.Autostart(on, NODE.get(name, "pve-01"), True)
proxmox.before_create = _pve_before_create
proxmox.after_create = _pve_after_create


# As if /backups were mounted (nothing is written there: the demo never makes a scheduled backup).
schedule.check_dir = lambda: get_bootstrap().backup_dir

# Job logs show paths as they are in the container image, not this machine's.
_write = jobs.JobLog.write
jobs.JobLog.write = lambda self, text: _write(self, text.replace(str(data), "/data").replace(str(ROOT), "/app"))


async def _no_ping(ips):
    return set()

ipam.ping_alive = _no_ping
ipam.arp_seen = lambda: set()


# --- made-up records ---------------------------------------------------------------------

def seed() -> None:
    # Settings live in the portal, as after "Import from environment", so the settings
    # pages don't open with the "set by environment variables" notice.
    from app import backends, config
    values = {k: getattr(get_settings(), k) for k in config.env_overrides()}
    db.save_settings(values, config.SECRET_FIELDS, by="admin")
    backends.import_env("admin")              # the vCenter backend the environment describes
    for k in set(values) | (config.env_fields() & config.BACKEND_FIELDS):
        os.environ.pop(k.upper(), None)
    config.reset_caches()
    db.execute("UPDATE backends SET from_env=0")   # nothing is left in the environment
    [vcenter] = backends.all_backends()
    pve_id = backends.create("Edge cluster", "proxmox", {
        "proxmox_server": "pve.example.com", "proxmox_token_id": "vmportal@pve!vmportal",
        "proxmox_token_secret": secrets.token_hex(16), "proxmox_node": "pve-01", "proxmox_storage": "ceph-vm",
        "proxmox_iso_storage": "cephfs", "proxmox_bridge": "vmbr0", "proxmox_pool": "portal",
        "proxmox_nodes": ["pve-02", "pve-03"],
        "ip_pool_start": "198.51.100.20", "ip_pool_end": "198.51.100.99", "ip_gateway": "198.51.100.1",
        "ip_prefix": 24, "ip_dns": ["198.51.100.1"]}, by="admin")

    users.ensure_bootstrap()
    for name in ("alice", "bob", "ops"):
        users.create(name, secrets.token_urlsafe(16), "admin" if name == "ops" else "user")
    uid = {u["username"]: u["id"] for u in users.all_users()}

    for img_id, iso in (("ubuntu-26.04", 0), ("debian-13", 1), ("rocky-10", 2)):
        img = images.get(img_id)
        cfg = images.validate_config(img, {"isos": {str(vcenter.id): ISOS[iso], str(pve_id): PVE_ISOS[iso]}})
        images.save_config(img, cfg, by="admin")

    def add_vms(rows, backend_id, resource):
        for name, owner, tmpl, image_id, cpus, mem, disk, ip, mode, _power, days, compose, notes in rows:
            created = ago(days=days, hours=3)
            login = {"debian-13": "debian", "rocky-10": "rocky"}.get(image_id, "ubuntu")
            vm_id = db.execute(
                "INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, ip, has_compose,"
                " created_at, owner_id, console_login, vm_user, image_id, notes, autostart, backend_id) VALUES"
                " (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (name, tmpl, cpus, mem, disk, mode, ip, int(compose), stamp(created), uid[owner],
                 int(name == "web-01"), login, image_id, notes, backend_id))
            d = get_settings().vms_dir / name
            d.mkdir(parents=True)
            (d / "terraform.tfstate").write_text("{}")  # so the page offers Resize
            job("vm-create", name, uid[owner], created + timedelta(minutes=1), 3,
                CREATE_LOG.format(name=name, tmpl=tmpl, resource=resource, ip=ip, login=login))
            db.execute("UPDATE vms SET last_job_id=(SELECT MAX(id) FROM jobs) WHERE id=?", (vm_id,))

    add_vms(VMS, vcenter.id, "vsphere_virtual_machine")

    job("vm-resize", "web-01", uid["alice"], ago(days=2, hours=5), 2,
        "==> Resizing web-01: cpus 1 -> 2, memory_mb 2048 -> 4096\n")
    job("packer-build", DEBIAN, uid["ops"], ago(days=1, hours=1), 19, BUILD_LOG.format(tmpl=DEBIAN))
    job("packer-build", UBUNTU, uid["ops"], ago(days=3, hours=1), 24, BUILD_LOG.format(tmpl=UBUNTU))
    # After the ones above, so their job numbers (the screenshots open job 8) stay the same.
    add_vms(PVE_VMS, pve_id, "proxmox_virtual_environment_vm")


def job(kind, target, user_id, started: datetime, minutes: int, log: str) -> None:
    job_id = db.execute(
        "INSERT INTO jobs (kind, target, status, created_at, started_at, finished_at, exit_code, user_id)"
        " VALUES (?, ?, 'succeeded', ?, ?, ?, 0, ?)",
        (kind, target, stamp(started), stamp(started), stamp(started + timedelta(minutes=minutes)), user_id))
    (get_settings().jobs_dir / f"{job_id}.log").write_text(f"Job {job_id}: {kind} {target}\n{log}"
                                                          "\n==> Job finished successfully\n")


CREATE_LOG = """
==> Preparing Terraform workspace /data/vms/{name}
$ tofu init -no-color -input=false -from-module=/app/terraform/vm
OpenTofu has been successfully initialized!

==> Cloning {tmpl} -> {name}
$ tofu apply -no-color -input=false -auto-approve
{resource}.this: Creating...
{resource}.this: Still creating... [1m0s elapsed]
{resource}.this: Creation complete after 1m38s
Apply complete! Resources: 1 added, 0 changed, 0 destroyed.

==> VM ready at {ip}: ssh {login}@{ip}
"""

BUILD_LOG = """
==> Building {tmpl}
$ packer init .
$ packer build -color=false -timestamp-ui .
vsphere-iso.base: output will be in this color.

2026-11-01T03:00:04Z: ==> vsphere-iso.base: Creating VM...
2026-11-01T03:00:09Z: ==> vsphere-iso.base: Customizing hardware...
2026-11-01T03:00:10Z: ==> vsphere-iso.base: Mounting ISO images...
2026-11-01T03:00:11Z: ==> vsphere-iso.base: Creating CD disk...
2026-11-01T03:00:14Z: ==> vsphere-iso.base: Power on VM...
2026-11-01T03:00:20Z: ==> vsphere-iso.base: Typing boot command...
2026-11-01T03:00:41Z: ==> vsphere-iso.base: Waiting for IP...
2026-11-01T03:09:52Z: ==> vsphere-iso.base: IP address: 192.0.2.200
2026-11-01T03:09:52Z: ==> vsphere-iso.base: Using SSH communicator to connect: 192.0.2.200
2026-11-01T03:10:07Z: ==> vsphere-iso.base: Connected to SSH!
2026-11-01T03:10:07Z: ==> vsphere-iso.base: Provisioning with shell script: scripts/docker-engine.sh
2026-11-01T03:10:12Z:     vsphere-iso.base: + sudo apt-get update
2026-11-01T03:11:40Z:     vsphere-iso.base: Setting up docker-ce (5:29.0.1-1) ...
2026-11-01T03:11:44Z: ==> vsphere-iso.base: Uploading files/grow-root.sh => /tmp/grow-root.sh
2026-11-01T03:11:46Z: ==> vsphere-iso.base: Provisioning with shell script: scripts/cleanup.sh
2026-11-01T03:12:01Z:     vsphere-iso.base: + sudo cloud-init clean --logs --seed
2026-11-01T03:12:03Z:     vsphere-iso.base: + sudo truncate -s 0 /etc/machine-id
2026-11-01T03:12:09Z: ==> vsphere-iso.base: Executing shutdown command...
2026-11-01T03:12:41Z: ==> vsphere-iso.base: Eject CD-ROM drives...
2026-11-01T03:12:42Z: ==> vsphere-iso.base: Convert VM into template...
2026-11-01T03:12:44Z: Build 'vsphere-iso.base' finished after 12 minutes 40 seconds.

==> Builds finished. The artifacts of successful builds are:
--> vsphere-iso.base: {tmpl}

==> Pruning old templates (keeping 2)
"""

from app.main import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    seed()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
