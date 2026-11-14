"""Create, resize and destroy VMs with OpenTofu; one working directory + state per VM."""
import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import yaml

from . import backends, config, db, firstboot, guests, jobs
from .config import get_settings
from .platforms import PLATFORMS, for_state
from .platforms.base import CREATED_BY, usable_ipv4

# OpenTofu (MPL-2.0) in the image; plain Terraform also works for local development.
TOFU = os.environ.get("IAC_BINARY") or ("tofu" if shutil.which("tofu") else "terraform")

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
    autostart: bool = True                      # start with its host
    backend_id: int | None = None               # the backend it goes on (backends.py)
    location: str | None = None                 # where on the backend (a Proxmox node); None = its default
    # A static IP's network, from the backend's pool settings (create() fills them in).
    ip_prefix: int = 24
    ip_gateway: str = ""
    ip_dns: tuple[str, ...] = ()


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
    services = doc["services"] if isinstance(doc["services"], dict) else {}
    for name, svc in services.items():
        if isinstance(svc, dict) and (missing := _required_env_files(svc.get("env_file"))):
            # Only compose.yaml is written to the VM, so `docker compose up` would stop at
            # "env file ... not found" on first boot and nothing would start.
            raise ValueError(
                f"Service '{name}' reads env_file {', '.join(missing)}, but only compose.yaml is "
                "copied to the VM. Move those variables under the service's 'environment:' key, "
                "or mark the file 'required: false'.")
    return text + "\n"


def _required_env_files(env_file) -> list[str]:
    """The env files a service's env_file entry needs to exist (compose accepts a string,
    a list of strings, or a list of {path, required} mappings)."""
    entries = env_file if isinstance(env_file, list) else [env_file] if env_file else []
    out = []
    for e in entries:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict) and e.get("required", True) is not False and e.get("path"):
            out.append(str(e["path"]))
    return out


def render_metadata(req: VMRequest) -> str:
    return guests.get(req.guest).tfvars(req)["metadata"]


def render_userdata(req: VMRequest) -> str:
    return guests.get(req.guest).tfvars(req)["userdata"]


def tf_env(b: backends.Backend) -> dict[str, str]:
    """The backend's connection variables, plus what every run gets. Connection settings
    always come from the backend's current settings through the environment, never from a
    VM's tfvars file: values written there at create time would outlive a later change (TLS
    verification turned on, a new server) and pair the current password with the old one."""
    return {
        **b.platform.tf_env(b),
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
    }


def vm_dir(name: str) -> Path:
    return get_settings().vms_dir / name


def tfvars(req: VMRequest, b: backends.Backend) -> dict:
    return {
        **b.platform.tf_placement(b, req.location),
        "template": req.template,
        "name": req.name,
        "cpus": req.cpus,
        "memory_mb": req.memory_mb,
        "disk_gb": req.disk_gb,
        **guests.get(req.guest).tfvars(req),
        "annotation": f"{CREATED_BY} from {req.template}",
    }


def with_network(req: VMRequest, b: backends.Backend) -> VMRequest:
    """A static IP's prefix, gateway and DNS come from the pool of the backend the VM goes on."""
    if not req.ip:
        return req
    if not b.cfg.ip_gateway:
        raise ValueError(f"{b.name} has no gateway set, so it can't give a VM a static IP")
    return replace(req, ip_prefix=b.cfg.ip_prefix, ip_gateway=b.cfg.ip_gateway, ip_dns=tuple(b.cfg.ip_dns))


def create(req: VMRequest, owner_id: int | None = None) -> int:
    """Record the VM and start a Terraform apply job. Returns the job id."""
    b = backends.get(req.backend_id)
    if b is None:
        raise ValueError("Choose a backend for the VM")
    req = with_network(req, b)
    pf = b.platform
    d = vm_dir(req.name)
    if d.exists():
        raise ValueError(f"A Terraform directory for {req.name!r} already exists ({d})")
    vm_id = db.execute(
        "INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, ip, has_compose, created_at,"
        " owner_id, console_login, vm_user, image_id, autostart, backend_id)"
        " VALUES (?, 'provisioning', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (req.name, req.template, req.cpus, req.memory_mb, req.disk_gb,
         "static" if req.ip else "dhcp", req.ip, int(bool(req.compose_yaml)), db.now(), owner_id,
         int(bool(req.console_password_hash)), req.login_user, req.image_id, int(req.autostart), b.id),
    )

    async def run(job_id: int, log: jobs.JobLog) -> None:
        tf_module = pf.tf_module_dir()
        log.step(f"Preparing Terraform workspace {d} (backend {b.name})")
        d.mkdir(parents=True)
        env = await asyncio.to_thread(tf_env, b)
        await log.run([TOFU, "init", "-no-color", "-input=false", f"-from-module={tf_module}"], cwd=d, env=env)
        tfvars_path = d / "terraform.tfvars.json"
        tfvars_path.touch(mode=0o600)  # holds the console password hash
        values = tfvars(req, b)
        values.update(await pf.before_create(b, req, d, log))
        tfvars_path.write_text(json.dumps(values, indent=2))
        log.step(f"Cloning {req.template} -> {req.name}")
        await log.run([TOFU, "apply", "-no-color", "-input=false", "-auto-approve"], cwd=d, env=env)
        out = await _output(d, b)
        # OpenTofu's "default IP" can be IPv6 link-local before the guest has its IPv4.
        ip = usable_ipv4(out.get("ip")) or req.ip
        db.execute("UPDATE vms SET ip=? WHERE id=?", (ip, vm_id))
        # The clone can be bigger than asked for (a template's disk is never shrunk): record
        # what it got, or later plans would try to shrink it, which the providers refuse.
        await record_actual_size(d, b, vm_id, log)
        await pf.after_create(b, req, vm_moid(req.name), log)
        if req.autostart:
            await enable_autostart(b, req.name, log)
        log.step(f"VM ready at {ip}: ssh {req.login_user}@{ip}")

    job_id = jobs.submit(
        "vm-create", req.name, run, lock_key=f"vm:{req.name}", user_id=owner_id,
        on_success=lambda: db.execute("UPDATE vms SET status='ready' WHERE id=?", (vm_id,)),
        on_failure=lambda: db.execute("UPDATE vms SET status='failed' WHERE id=?", (vm_id,)),
    )
    db.execute("UPDATE vms SET last_job_id=? WHERE id=?", (job_id, vm_id))
    if req.guest == "cloud-init" and firstboot.tracked(pf):
        db.execute("UPDATE vms SET firstboot_state='waiting', firstboot_since=? WHERE id=?", (db.now(), vm_id))
        firstboot.ensure_watching()
    return job_id


HOST_AUTOSTART_OFF = "Host {host} has autostart turned off, so the VM won't start with it. {help}"


async def enable_autostart(b: backends.Backend, name: str, log: jobs.JobLog) -> None:
    """Have a new VM start with its host. A failure only warns: the VM itself is fine,
    and its page offers to try again."""
    log.step("Turning on autostart")
    try:
        st = await asyncio.to_thread(b.platform.set_autostart, b, name, vm_moid(name), True)
    except Exception as e:  # noqa: BLE001 - reported in the log, never fails the job
        log.write(f"Warning: couldn't turn on autostart ({e}). Try again from the VM page.\n")
        return
    if not st.host_enabled:
        log.write("Warning: " + HOST_AUTOSTART_OFF.format(host=st.host, help=b.platform.HOST_AUTOSTART_HELP).strip() + "\n")


def vm_backend(vm: dict) -> backends.Backend:
    b = backends.of_vm(vm)
    if b is None:
        raise ValueError(f"{vm['name']}'s backend no longer exists in this portal")
    return b


def destroy(vm: dict, user_id: int | None = None) -> int:
    d = vm_dir(vm["name"])
    b = vm_backend(vm)
    db.execute("UPDATE vms SET status='destroying' WHERE id=?", (vm["id"],))

    async def run(job_id: int, log: jobs.JobLog) -> None:
        platform_id = vm_moid(vm["name"])
        if not (d / "terraform.tfstate").exists():
            # Provisioning failed before Terraform created anything.
            log.step(f"No Terraform state; nothing to destroy on {b.name}")
        else:
            log.step(f"Destroying {vm['name']} (backend {b.name})")
            keep_state_history(d)
            await prepare_workspace(d, log, b)
            await log.run([TOFU, "destroy", "-no-color", "-input=false", "-auto-approve"], cwd=d,
                          env=await asyncio.to_thread(tf_env, b))
        await b.platform.after_destroy(b, vm["name"], platform_id, log)
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


def forget(vm: dict) -> Path | None:
    """Stop managing a VM without touching it on its backend (it moved to another portal).

    Its OpenTofu files are kept aside in DATA_DIR/forgotten, like a destroyed VM's, so a
    mistake can still be undone by importing a backup. The caller holds the VM's job lock."""
    d = vm_dir(vm["name"])
    kept = None
    if d.exists():
        kept = get_settings().data_dir / "forgotten" / f"{vm['name']}-{vm['id']}"
        kept.parent.mkdir(mode=0o700, exist_ok=True)
        if kept.exists():
            raise ValueError(f"{kept} already exists")
        shutil.move(d, kept)
        shutil.rmtree(kept / ".terraform", ignore_errors=True)
    db.execute("UPDATE vms SET status='forgotten' WHERE id=?", (vm["id"],))
    return kept


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
    """The VM's id on its platform (vCenter's managed object id, Proxmox's VMID), from its
    OpenTofu state (None if unknown). The state says which platform wrote it."""
    try:
        state = json.loads((vm_dir(name) / "terraform.tfstate").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    pf = for_state(state)
    return pf.state_vm_id(state) if pf else None


async def prepare_workspace(d: Path, log: jobs.JobLog, b: backends.Backend) -> None:
    """Bring an existing VM's working directory up to date before planning against it.

    The directory may come from an older portal (older module copy, Terraform instead of
    OpenTofu), and the VM's template may have been pruned since it was cloned.
    """
    pf = b.platform
    await pf.before_change(b, d, log)
    env = await asyncio.to_thread(tf_env, b)
    for f in pf.tf_module_dir().glob("*.tf"):
        shutil.copyfile(f, d / f.name)
    # Older portals wrote the connection settings into tfvars; drop them so the current
    # ones (from tf_env) apply.
    update_tfvars(d, _drop=pf.TF_CONNECTION_VARS)
    await log.run([TOFU, "init", "-no-color", "-input=false", "-upgrade"], cwd=d, env=env)
    legacy = pf.TF_LEGACY_PROVIDER
    if TOFU.endswith("tofu") and legacy and legacy in (d / "terraform.tfstate").read_text():
        log.step("Moving Terraform-era state to the OpenTofu provider address (backup kept)")
        await log.run([TOFU, "state", "replace-provider", "-no-color", "-auto-approve",
                       legacy, pf.TF_PROVIDER], cwd=d, env=env)
    show = await _show_json(d, b)
    facts = template_facts(show, pf)
    if facts:
        update_tfvars(d, template_facts=facts)
    elif pf.TF_FACTS:
        log.write("Template facts not found in state; planning against the template itself\n")
    # VMs created before the portal recorded the size they really got may have a smaller
    # disk in their variables than in reality; a plan would then try to shrink it.
    actual = actual_size(show, pf)
    tfvars = json.loads((d / "terraform.tfvars.json").read_text())
    if actual and isinstance(tfvars.get("disk_gb"), int) and actual["disk_gb"] > tfvars["disk_gb"]:
        log.write(f"The disk is {actual['disk_gb']} GB, not {tfvars['disk_gb']} GB as recorded: recording that\n")
        update_tfvars(d, disk_gb=actual["disk_gb"])
        db.execute("UPDATE vms SET disk_gb=? WHERE name=? AND status NOT IN ('destroyed', 'forgotten')",
                   (actual["disk_gb"], d.name))


async def record_actual_size(d: Path, b: backends.Backend, vm_id: int, log: jobs.JobLog) -> None:
    """Store the VM's real vCPUs, memory and disk size (from its state) in its variables
    and record."""
    actual = actual_size(await _show_json(d, b), b.platform)
    if not actual:
        return
    tfvars = json.loads((d / "terraform.tfvars.json").read_text())
    if any(tfvars.get(k) != v for k, v in actual.items()):
        if actual["disk_gb"] != tfvars.get("disk_gb"):
            log.write(f"The disk is {actual['disk_gb']} GB (the template's disk is never shrunk)\n")
        update_tfvars(d, **actual)
        db.execute("UPDATE vms SET cpus=?, memory_mb=?, disk_gb=? WHERE id=?",
                   (actual["cpus"], actual["memory_mb"], actual["disk_gb"], vm_id))


def _vm_resource(show: dict, pf) -> dict | None:
    """The VM's attributes from `tofu show -json` of the state."""
    for r in (show.get("values") or {}).get("root_module", {}).get("resources", []):
        if r.get("address") == ".".join(pf.TF_RESOURCE):
            return r.get("values") or {}
    return None


def template_facts(show: dict, pf=PLATFORMS["vsphere"]) -> dict | None:
    return pf.tf_template_facts(_vm_resource(show, pf))


def actual_size(show: dict, pf=PLATFORMS["vsphere"]) -> dict | None:
    return pf.tf_size(_vm_resource(show, pf))


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


def check_resize_plan(plan: dict, pf=PLATFORMS["vsphere"]) -> list[str]:
    """Problems that make a plan more than an in-place resize of this VM (empty = safe).

    A resize may only change the attributes the platform lists (TF_RESIZABLE). Anything else
    in the plan (a manual change on the platform that the plan would revert, a replacement)
    stops the job before it touches the VM."""
    problems = []
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions in (["no-op"], ["read"]):
            continue
        if rc.get("address") != ".".join(pf.TF_RESOURCE) or actions != ["update"]:
            problems.append(f"{rc.get('address')}: {'/'.join(actions)}")
            continue
        ch = rc["change"]
        before, after = ch.get("before") or {}, ch.get("after") or {}
        for path in _diff(before, after, ch.get("after_unknown")):
            if path in pf.TF_RESIZABLE or (len(path) == 1 and path[0] in pf.TF_RECOMPUTED):
                continue
            if len(path) == 1 and not before.get(path[0]) and pf.TF_DEFAULTED.get(path[0]) == after.get(path[0]):
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
    b = vm_backend(vm)
    pf = b.platform
    if disk_gb < vm["disk_gb"]:
        raise ValueError("Disks can only grow")
    needs_off = (cpus, memory_mb) != (vm["cpus"], vm["memory_mb"])
    target = {"cpus": cpus, "memory_mb": memory_mb, "disk_gb": disk_gb}

    async def run(job_id: int, log: jobs.JobLog) -> None:
        changes = ", ".join(f"{k} {vm[k]} -> {v}" for k, v in target.items() if vm[k] != v)
        log.step(f"Resizing {vm['name']}: {changes}")
        if needs_off and not restart:
            info = (await asyncio.to_thread(pf.vms_by_name, b, True)).get(vm["name"])
            if info is None or info.power_state != "poweredOff":
                raise jobs.JobFailed("Changing vCPUs or memory restarts the VM, and no restart was allowed")
        keep_state_history(d)
        await prepare_workspace(d, log, b)
        env = await asyncio.to_thread(tf_env, b)
        before = actual_size(await _show_json(d, b), pf)
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
            await log.run([TOFU, "plan", "-no-color", "-input=false", f"-out={plan}"], cwd=d, env=env)
            problems = check_resize_plan(await _show_json(d, b, plan), pf)
            if problems:
                raise jobs.JobFailed("The plan does more than resize the VM, so nothing was changed. This usually"
                                     f" means the VM was changed in {pf.API} by hand:\n  " + "\n  ".join(problems))
            log.step("Applying" + (" (the VM shuts down and starts again)" if needs_off else ""))
            await log.run([TOFU, "apply", "-no-color", "-input=false", plan], cwd=d, env=env)
        finally:
            shutil.rmtree(private, ignore_errors=True)
            # Record what the VM really has now, whether or not the apply got through.
            actual = actual_size(await _show_json(d, b), pf)
            if actual:
                update_tfvars(d, **actual)
                db.execute("UPDATE vms SET cpus=?, memory_mb=?, disk_gb=? WHERE id=?",
                           (actual["cpus"], actual["memory_mb"], actual["disk_gb"], vm["id"]))
        if disk_gb > vm["disk_gb"]:
            log.write("The root filesystem grows into the new space on the next boot.\n")

    job_id = jobs.submit("vm-resize", vm["name"], run, lock_key=f"vm:{vm['name']}", user_id=user_id)
    db.execute("UPDATE vms SET last_job_id=? WHERE id=?", (job_id, vm["id"]))
    return job_id


async def _show_json(d: Path, b: backends.Backend, plan: str | None = None) -> dict:
    env = await asyncio.to_thread(tf_env, b)
    proc = await asyncio.create_subprocess_exec(
        TOFU, "show", "-json", *([plan] if plan else []), cwd=d, env=jobs.child_env(env),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return json.loads(out or b"{}")
    except json.JSONDecodeError:
        return {}


async def _output(d: Path, b: backends.Backend) -> dict:
    env = await asyncio.to_thread(tf_env, b)
    proc = await asyncio.create_subprocess_exec(
        TOFU, "output", "-json", cwd=d, env=jobs.child_env(env),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return {k: v.get("value") for k, v in json.loads(out or b"{}").items()}
    except json.JSONDecodeError:
        return {}
