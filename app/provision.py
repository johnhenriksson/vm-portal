"""Create, resize and destroy VMs with OpenTofu; one working directory + state per VM."""
import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# vCenter connection variables. They always come from the current settings through the
# environment, never from a VM's tfvars file: values written there at create time would
# outlive a later change (TLS verification turned on, a new server) and pair the current
# password with the old connection. tfvars.json beats TF_VAR_*, so they must not be in it.
CONNECTION_VARS = ("vsphere_server", "vsphere_user", "vsphere_insecure")


def tf_env() -> dict[str, str]:
    s = get_settings()
    return {
        "TF_VAR_vsphere_server": s.vsphere_server,
        "TF_VAR_vsphere_user": s.vsphere_user,
        "TF_VAR_vsphere_password": s.vsphere_password,
        "TF_VAR_vsphere_insecure": "true" if s.vsphere_insecure else "false",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
    }


def vm_dir(name: str) -> Path:
    return get_settings().vms_dir / name


def tfvars(req: VMRequest) -> dict:
    s = get_settings()
    return {
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
            keep_state_history(d)
            await prepare_workspace(d, log)
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


# --- existing VMs ---------------------------------------------------------------------

STATE_HISTORY = 5
HISTORY_RE = re.compile(r"^[0-9]{8}T[0-9]{12}Z\.tfstate$")


def keep_state_history(d: Path) -> None:
    """Copy the VM's state aside before something changes it; keep the newest few.
    A damaged state can then be rolled back without a full restore."""
    src = d / "terraform.tfstate"
    if not src.is_file() or src.is_symlink():
        return
    hist = d / "history"
    hist.mkdir(mode=0o700, exist_ok=True)
    dest = hist / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".tfstate")
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # state holds cloud-init data
    with os.fdopen(fd, "wb") as fh:
        fh.write(src.read_bytes())
    for old in state_history_files(d)[:-STATE_HISTORY]:
        old.unlink(missing_ok=True)


def state_history_files(d: Path) -> list[Path]:
    hist = d / "history"
    if not hist.is_dir():
        return []
    return sorted(p for p in hist.iterdir() if HISTORY_RE.match(p.name) and p.is_file() and not p.is_symlink())


def write_private(path: Path, data: bytes) -> None:
    """Replace a file atomically with mode 0600 (state and variables hold cloud-init data)."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    tmp.replace(path)


def vm_moid(name: str) -> str | None:
    """The VM's vCenter managed object id, from its Terraform state (None if unknown)."""
    try:
        state = json.loads((vm_dir(name) / "terraform.tfstate").read_text())
    except (OSError, ValueError):
        return None
    for r in state.get("resources") or []:
        if r.get("type") == "vsphere_virtual_machine" and r.get("name") == "this" and r.get("mode") != "data":
            for inst in r.get("instances") or []:
                moid = (inst.get("attributes") or {}).get("moid")
                if isinstance(moid, str) and moid:
                    return moid
    return None


async def prepare_workspace(d: Path, log: jobs.JobLog) -> None:
    """Bring an existing VM's working directory up to date before planning against it.

    The directory may come from an older portal (older module copy, Terraform instead of
    OpenTofu), and the VM's template may have been pruned since it was cloned.
    """
    module = get_settings().terraform_module_dir
    for f in module.glob("*.tf"):
        shutil.copyfile(f, d / f.name)
    # Older portals wrote the connection settings into tfvars; drop them so the current
    # ones (from tf_env) apply.
    update_tfvars(d, _drop=CONNECTION_VARS)
    await log.run([TOFU, "init", "-no-color", "-input=false", "-upgrade"], cwd=d, env=tf_env())
    if TOFU.endswith("tofu") and LEGACY_PROVIDER in (d / "terraform.tfstate").read_text():
        log.step("Moving Terraform-era state to the OpenTofu provider address (backup kept)")
        await log.run([TOFU, "state", "replace-provider", "-no-color", "-auto-approve",
                       LEGACY_PROVIDER, TOFU_PROVIDER], cwd=d, env=tf_env())
    facts = template_facts(await _show_json(d))
    if facts:
        update_tfvars(d, template_facts=facts)
    else:
        log.write("Template facts not found in state; planning against the template itself\n")


def _vm_resource(show: dict) -> dict | None:
    """The VM's attributes from `tofu show -json` of the state."""
    for r in (show.get("values") or {}).get("root_module", {}).get("resources", []):
        if r.get("address") == "vsphere_virtual_machine.this":
            return r.get("values") or {}
    return None


def template_facts(show: dict) -> dict | None:
    vm = _vm_resource(show)
    try:
        return {"guest_id": vm["guest_id"], "scsi_type": vm["scsi_type"], "firmware": vm["firmware"],
                "adapter_type": vm["network_interface"][0]["adapter_type"],
                "uuid": vm["clone"][0]["template_uuid"]}
    except (TypeError, KeyError, IndexError):
        return None


def actual_size(show: dict) -> dict | None:
    vm = _vm_resource(show)
    try:
        return {"cpus": int(vm["num_cpus"]), "memory_mb": int(vm["memory"]), "disk_gb": int(vm["disk"][0]["size"])}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def update_tfvars(d: Path, _drop: tuple[str, ...] = (), **values) -> None:
    """Change some variables, keeping the rest (first-boot data, password hash) as created."""
    path = d / "terraform.tfvars.json"
    data = json.loads(path.read_text())
    for k in _drop:
        data.pop(k, None)
    data.update(values)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# Attributes a resize may change. Anything else in the plan (a manual change in vCenter
# that the plan would revert, a replacement) stops the job before it touches the VM.
RESIZABLE = {("num_cpus",), ("memory",), ("disk", 0, "size")}
# Attributes the provider recomputes on any update; they may show as unknown.
RECOMPUTED = {"change_version", "reboot_required", "imported", "default_ip_address", "guest_ip_addresses",
              "vmx_path", "moid", "power_state", "uuid", "vapp_transport"}
# Attributes that may go from unset in older state to the provider's default.
DEFAULTED = {"swap_placement_policy": "inherit"}


def _diff(before, after, unknown, path=()):
    """Paths where after differs from before (or is unknown until apply)."""
    if unknown is True:
        yield path
        return
    if isinstance(before, dict) and isinstance(after, dict):
        unknown = unknown if isinstance(unknown, dict) else {}
        for k in before.keys() | after.keys() | unknown.keys():
            yield from _diff(before.get(k), after.get(k), unknown.get(k), (*path, k))
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        unknown = unknown if isinstance(unknown, list) else []
        for i, (b, a) in enumerate(zip(before, after)):
            yield from _diff(b, a, unknown[i] if i < len(unknown) else None, (*path, i))
    elif before != after:
        yield path


def check_resize_plan(plan: dict) -> list[str]:
    """Problems that make a plan more than an in-place resize of this VM (empty = safe)."""
    problems = []
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions in (["no-op"], ["read"]):
            continue
        if rc.get("address") != "vsphere_virtual_machine.this" or actions != ["update"]:
            problems.append(f"{rc.get('address')}: {'/'.join(actions)}")
            continue
        ch = rc["change"]
        before, after = ch.get("before") or {}, ch.get("after") or {}
        for path in _diff(before, after, ch.get("after_unknown")):
            if path in RESIZABLE or (len(path) == 1 and path[0] in RECOMPUTED):
                continue
            if len(path) == 1 and not before.get(path[0]) and DEFAULTED.get(path[0]) == after.get(path[0]):
                continue
            problems.append("would change " + ".".join(map(str, path)))
    return problems


def resize(vm: dict, cpus: int, memory_mb: int, disk_gb: int, restart: bool, user_id: int | None = None) -> int:
    """Change a VM's vCPUs, memory and disk size. Returns the job id.

    CPU and memory changes need the VM off; with restart=True the provider shuts the guest
    down cleanly, changes it and powers it back on. A bigger disk is added online.
    """
    d = vm_dir(vm["name"])
    if not (d / "terraform.tfstate").exists():
        raise ValueError("This VM has no Terraform state, so the portal can't change it")
    if disk_gb < vm["disk_gb"]:
        raise ValueError("Disks can only grow")
    needs_off = (cpus, memory_mb) != (vm["cpus"], vm["memory_mb"])
    target = {"cpus": cpus, "memory_mb": memory_mb, "disk_gb": disk_gb}

    async def run(job_id: int, log: jobs.JobLog) -> None:
        from . import vsphere
        changes = ", ".join(f"{k} {vm[k]} -> {v}" for k, v in target.items() if vm[k] != v)
        log.step(f"Resizing {vm['name']}: {changes}")
        if needs_off and not restart:
            info = (await asyncio.to_thread(vsphere.vms_by_name, True)).get(vm["name"])
            if info is None or info.power_state != "poweredOff":
                raise jobs.JobFailed("Changing vCPUs or memory restarts the VM, and no restart was allowed")
        keep_state_history(d)
        await prepare_workspace(d, log)
        before = actual_size(await _show_json(d))
        if before and disk_gb < before["disk_gb"]:
            # The clone can be bigger than asked for when the template's disk was bigger.
            raise jobs.JobFailed(f"The disk is already {before['disk_gb']} GB and disks can only grow")
        # The plan file holds variable values, including the vCenter password: it goes in a
        # folder only the portal can open (whatever mode tofu gives the file) and is deleted
        # whatever happens. (No umask for the child: uvloop's subprocess API doesn't take one.)
        private = d / ".plan"
        shutil.rmtree(private, ignore_errors=True)
        private.mkdir(mode=0o700)
        plan = f"{private.name}/resize.tfplan"
        try:
            update_tfvars(d, **target)
            log.step("Planning")
            await log.run([TOFU, "plan", "-no-color", "-input=false", f"-out={plan}"], cwd=d, env=tf_env())
            problems = check_resize_plan(await _show_json(d, plan))
            if problems:
                raise jobs.JobFailed("The plan does more than resize the VM, so nothing was changed. This usually"
                                     " means the VM was changed in vCenter by hand:\n  " + "\n  ".join(problems))
            log.step("Applying" + (" (the VM shuts down and starts again)" if needs_off else ""))
            await log.run([TOFU, "apply", "-no-color", "-input=false", plan], cwd=d, env=tf_env())
        finally:
            shutil.rmtree(private, ignore_errors=True)
            # Record what the VM really has now, whether or not the apply got through.
            actual = actual_size(await _show_json(d))
            if actual:
                update_tfvars(d, **actual)
                db.execute("UPDATE vms SET cpus=?, memory_mb=?, disk_gb=? WHERE id=?",
                           (actual["cpus"], actual["memory_mb"], actual["disk_gb"], vm["id"]))
        if disk_gb > vm["disk_gb"]:
            log.write("The root filesystem grows into the new space on the next boot.\n")

    job_id = jobs.submit("vm-resize", vm["name"], run, lock_key=f"vm:{vm['name']}", user_id=user_id)
    db.execute("UPDATE vms SET last_job_id=? WHERE id=?", (job_id, vm["id"]))
    return job_id


async def _show_json(d: Path, plan: str | None = None) -> dict:
    proc = await asyncio.create_subprocess_exec(
        TOFU, "show", "-json", *([plan] if plan else []), cwd=d, env=jobs.child_env(tf_env()),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return json.loads(out or b"{}")
    except json.JSONDecodeError:
        return {}


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
