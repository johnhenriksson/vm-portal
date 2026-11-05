"""Create and destroy VMs with Terraform; one working directory + state per VM."""
import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import bcrypt
import yaml

from . import config, db, guests, jobs
from .config import get_settings

# OpenTofu (MPL-2.0) in the image; plain Terraform also works for local development.
TOFU = os.environ.get("IAC_BINARY") or ("tofu" if shutil.which("tofu") else "terraform")
LEGACY_PROVIDER = "registry.terraform.io/vmware/vsphere"   # in state written by Terraform
TOFU_PROVIDER = "registry.opentofu.org/vmware/vsphere"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
SSH_KEY_RE = config.SSH_KEY_RE


@dataclass
class VMRequest:
    name: str
    template: str
    cpus: int
    memory_mb: int
    disk_gb: int
    ip: str | None = None                       # None = DHCP
    ssh_keys: list[str] = field(default_factory=list)
    compose_yaml: str = ""
    console_password_hash: str | None = None    # bcrypt; enables login on the local console only
    login_user: str = "ubuntu"                  # user that gets the keys (from the image definition)
    guest: str = guests.DEFAULT                 # guest driver (from the image definition)
    features: tuple[str, ...] = ("docker",)     # image features (from the image definition)
    image_id: str | None = None
    network_interface: str = "en*"              # template's NIC name or glob (from the image definition)


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError("Name must be 3-32 chars: lowercase letters, digits and '-', starting with a letter")
    return name


def parse_ssh_keys(text: str) -> list[str]:
    keys = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    for k in keys:
        if not SSH_KEY_RE.match(k):
            raise ValueError(f"Not a valid SSH public key: {k[:40]}…")
    return keys


def hash_console_password(password: str, confirm: str) -> str | None:
    """bcrypt hash for /etc/shadow, or None when no console password was given.

    The plain password is never stored. bcrypt ($2b$) is supported by Ubuntu's libxcrypt.
    """
    if not password and not confirm:
        return None
    from .users import validate_password
    if password != confirm:
        raise ValueError("The console passwords don't match")
    validate_password(password)
    if len(password.encode()) > 72:
        raise ValueError("The console password can be at most 72 bytes")
    if any(ord(c) < 32 for c in password):
        raise ValueError("The console password can't contain control characters")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()


def validate_compose(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"compose.yaml is not valid YAML: {e}") from None
    if not isinstance(doc, dict) or "services" not in doc:
        raise ValueError("compose.yaml must have a top-level 'services' key")
    return text + "\n"


def render_metadata(req: VMRequest) -> str:
    return guests.get(req.guest).tfvars(req)["metadata"]


def render_userdata(req: VMRequest) -> str:
    return guests.get(req.guest).tfvars(req)["userdata"]


def tf_env() -> dict[str, str]:
    s = get_settings()
    return {
        "TF_VAR_vsphere_password": s.vsphere_password,
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
    }


def vm_dir(name: str) -> Path:
    return get_settings().vms_dir / name


def tfvars(req: VMRequest) -> dict:
    s = get_settings()
    return {
        "vsphere_server": s.vsphere_server,
        "vsphere_user": s.vsphere_user,
        "vsphere_insecure": s.vsphere_insecure,
        "datacenter": s.vsphere_datacenter,
        "cluster": s.vsphere_cluster,
        "datastore": s.vsphere_datastore,
        "network": s.vsphere_network,
        "folder": s.vsphere_folder,
        "template": req.template,
        "name": req.name,
        "cpus": req.cpus,
        "memory_mb": req.memory_mb,
        "disk_gb": req.disk_gb,
        **guests.get(req.guest).tfvars(req),
        "annotation": f"Created by vm-portal from {req.template}",
    }


def create(req: VMRequest, owner_id: int | None = None) -> int:
    """Record the VM and start a Terraform apply job. Returns the job id."""
    d = vm_dir(req.name)
    if d.exists():
        raise ValueError(f"A Terraform directory for {req.name!r} already exists ({d})")
    vm_id = db.execute(
        "INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, ip, has_compose, created_at,"
        " owner_id, console_login, vm_user, image_id) VALUES (?, 'provisioning', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (req.name, req.template, req.cpus, req.memory_mb, req.disk_gb,
         "static" if req.ip else "dhcp", req.ip, int(bool(req.compose_yaml)), db.now(), owner_id,
         int(bool(req.console_password_hash)), req.login_user, req.image_id),
    )

    async def run(job_id: int, log: jobs.JobLog) -> None:
        tf_module = get_settings().terraform_module_dir
        log.step(f"Preparing Terraform workspace {d}")
        d.mkdir(parents=True)
        await log.run([TOFU, "init", "-no-color", "-input=false", f"-from-module={tf_module}"], cwd=d, env=tf_env())
        tfvars_path = d / "terraform.tfvars.json"
        tfvars_path.touch(mode=0o600)  # holds the console password hash
        tfvars_path.write_text(json.dumps(tfvars(req), indent=2))
        log.step(f"Cloning {req.template} -> {req.name}")
        await log.run([TOFU, "apply", "-no-color", "-input=false", "-auto-approve"], cwd=d, env=tf_env())
        out = await _output(d)
        ip = out.get("ip") or req.ip
        db.execute("UPDATE vms SET ip=? WHERE id=?", (ip, vm_id))
        log.step(f"VM ready at {ip}: ssh {req.login_user}@{ip}")

    job_id = jobs.submit(
        "vm-create", req.name, run, lock_key=f"vm:{req.name}", user_id=owner_id,
        on_success=lambda: db.execute("UPDATE vms SET status='ready' WHERE id=?", (vm_id,)),
        on_failure=lambda: db.execute("UPDATE vms SET status='failed' WHERE id=?", (vm_id,)),
    )
    db.execute("UPDATE vms SET last_job_id=? WHERE id=?", (job_id, vm_id))
    return job_id


def destroy(vm: dict, user_id: int | None = None) -> int:
    d = vm_dir(vm["name"])
    db.execute("UPDATE vms SET status='destroying' WHERE id=?", (vm["id"],))

    async def run(job_id: int, log: jobs.JobLog) -> None:
        if not (d / "terraform.tfstate").exists():
            # Provisioning failed before Terraform created anything.
            log.step("No Terraform state; nothing to destroy in vCenter")
        else:
            log.step(f"Destroying {vm['name']}")
            # Re-init first: the directory may come from an older image (or from Terraform).
            await log.run([TOFU, "init", "-no-color", "-input=false", "-upgrade"], cwd=d, env=tf_env())
            if TOFU.endswith("tofu") and LEGACY_PROVIDER in (d / "terraform.tfstate").read_text():
                log.step("Moving Terraform-era state to the OpenTofu provider address (backup kept)")
                await log.run([TOFU, "state", "replace-provider", "-no-color", "-auto-approve",
                               LEGACY_PROVIDER, TOFU_PROVIDER], cwd=d, env=tf_env())
            await log.run([TOFU, "destroy", "-no-color", "-input=false", "-auto-approve"], cwd=d, env=tf_env())
        archive = get_settings().data_dir / "destroyed" / f"{vm['name']}-{vm['id']}"
        if d.exists():
            archive.parent.mkdir(exist_ok=True)
            shutil.move(d, archive)
            shutil.rmtree(archive / ".terraform", ignore_errors=True)
            log.write(f"Terraform files archived to {archive}\n")

    job_id = jobs.submit(
        "vm-destroy", vm["name"], run, lock_key=f"vm:{vm['name']}", user_id=user_id,
        on_success=lambda: db.execute("UPDATE vms SET status='destroyed' WHERE id=?", (vm["id"],)),
        on_failure=lambda: db.execute("UPDATE vms SET status='failed' WHERE id=?", (vm["id"],)),
    )
    db.execute("UPDATE vms SET last_job_id=? WHERE id=?", (job_id, vm["id"]))
    return job_id


async def _output(d: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        TOFU, "output", "-json", cwd=d, env=jobs.child_env(tf_env()),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return {k: v.get("value") for k, v in json.loads(out or b"{}").items()}
    except json.JSONDecodeError:
        return {}
