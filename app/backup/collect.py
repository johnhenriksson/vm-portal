"""What goes into a backup, read from the live portal.

Never included: the encryption and session keys, the Packer build key, revoked
sessions, OpenTofu's provider caches and saved plans, and anything set by the
environment (an environment-only vCenter password or Proxmox token secret stays out
of every backup).
"""
import json
import re
import uuid

from .. import db, jobs
from ..config import get_bootstrap
from .archive import Backup

STATE_FILES = ("terraform.tfstate", "terraform.tfvars.json")


def installation_id() -> str:
    """A random id for this installation, so a backup shows where it came from."""
    row = db.one("SELECT value FROM settings WHERE key='installation_id'")
    if row:
        return json.loads(row["value"])
    value = uuid.uuid4().hex
    db.execute("INSERT OR IGNORE INTO settings (key, value, encrypted, updated_at, updated_by)"
               " VALUES ('installation_id', ?, 0, ?, 'system')", (json.dumps(value), db.now()))
    return json.loads(db.one("SELECT value FROM settings WHERE key='installation_id'")["value"])


def busy_vms() -> list[str]:
    """VMs with a job running: their state may be mid-write, so backups wait for them."""
    return sorted(k.removeprefix("vm:") for k in jobs.lock_keys() if k.startswith("vm:") and jobs.is_locked(k))


def _state_files(name: str) -> dict[str, bytes]:
    d = get_bootstrap().vms_dir / name
    out = {}
    for f in STATE_FILES:
        p = d / f
        if p.is_file() and not p.is_symlink():
            out[f"vms/{name}/{f}"] = p.read_bytes()
    return out


def meta(by: str) -> dict:
    return {"installation_id": installation_id(), "created_by": by, "app": "vm-portal"}


def vms(names: list[str], by: str) -> tuple[dict[str, bytes], dict]:
    """The chosen VMs' records and OpenTofu state. Owners go by username: ids mean nothing elsewhere."""
    names = sorted(set(names))
    if not names:
        raise ValueError("Choose at least one VM")
    busy = set(busy_vms()) & set(names)
    if busy:
        raise ValueError(f"Wait for the running job on {', '.join(sorted(busy))} to finish")
    from .. import backends
    owners = {u["id"]: u["username"] for u in db.query("SELECT id, username FROM users")}
    rows, files = [], {}
    for name in names:
        vm = db.active_vm(name)
        if not vm:
            raise ValueError(f"No VM named {name!r}")
        state = _state_files(name)
        if f"vms/{name}/terraform.tfstate" not in state:
            raise ValueError(f"{name} has no OpenTofu state to back up")
        files.update(state)
        b = backends.of_vm(vm)
        # Ids mean nothing on another portal: the owner and the backend go by name.
        rows.append({**vm, "owner_id": None, "last_job_id": None, "owner_username": owners.get(vm["owner_id"]),
                     "backend_id": None, "backend_name": b.name if b else None, "backend_type": b.type if b else None})
    files["db/vms.json"] = json.dumps(rows, indent=1).encode()
    return files, {**meta(by), "vms": names}


def vm_entries(b: Backup) -> list[dict]:
    """The VMs in a backup (of either kind) that have state, with their owner's username."""
    from .checks import VmRow, rows
    users = {u.get("id"): u.get("username") for u in (b.json("db/users.json") or []) if isinstance(u, dict)}
    out = []
    for r in rows(VmRow, b.json("db/vms.json", []), "VMs"):
        state = b.files.get(f"vms/{r.name}/terraform.tfstate")
        if r.status in db.GONE or state is None:
            continue
        owner = r.owner_username if b.kind == "vms" else users.get(r.owner_id)
        out.append({"row": r, "owner": owner if isinstance(owner, str) else None, "state": state,
                    "tfvars": b.files.get(f"vms/{r.name}/terraform.tfvars.json")})
    return out


# --- full backup ---------------------------------------------------------------------------

TABLES = ("users", "vms", "image_settings", "jobs", "audit")
LOG_RE = re.compile(r"^([0-9]{1,12})\.log$")


def _custom_images() -> dict[str, bytes]:
    """Custom image definitions (not their edit history), by the editor's own path rules."""
    from .. import editor, images
    out = {}
    root = images.custom_dir()
    if not root.is_dir():
        return out
    builtin = {i.id for i in images.catalog() if i.builtin}
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.is_symlink() or not images.ID_RE.match(d.name) or d.name in builtin:
            continue
        img = images.get(d.name)
        if img is None or img.builtin:
            continue
        for rel in editor.list_files(img):
            try:
                editor.check_path(rel)
            except ValueError:
                continue  # e.g. a packer.log: not part of the definition
            out[f"images/{d.name}/{rel}"] = (d / rel).read_bytes()
    return out


def _job_logs() -> dict[str, bytes]:
    from .archive import MAX_FILE_BYTES
    out = {}
    jobs_dir = get_bootstrap().jobs_dir
    for p in sorted(jobs_dir.iterdir()) if jobs_dir.is_dir() else []:
        if LOG_RE.match(p.name) and p.is_file() and not p.is_symlink() and p.stat().st_size <= MAX_FILE_BYTES:
            out[f"jobs/{p.name}"] = p.read_bytes()
    return out


def full(by: str, include_logs: bool = False) -> tuple[dict[str, bytes], dict]:
    """Everything needed to rebuild this portal: database rows, stored settings and secrets,
    the backends (with their stored secrets), the VMs' OpenTofu state, custom image
    definitions and, optionally, job logs."""
    from .. import backends
    from ..config import EDITABLE, SECRET_FIELDS
    busy = busy_vms()
    if busy:
        raise ValueError(f"Wait for the running job on {', '.join(busy)} to finish")
    inst = installation_id()
    tables = db.dump(TABLES)
    stored = {k: v for k, v in db.load_settings().items() if k in EDITABLE}
    files = {f"db/{t}.json": json.dumps(rows, indent=1).encode() for t, rows in tables.items()}
    files["db/settings.json"] = json.dumps({k: v for k, v in stored.items() if k not in SECRET_FIELDS},
                                           indent=1).encode()
    rows = backends.stored_rows()
    files["db/backends.json"] = json.dumps([{k: v for k, v in r.items() if k != "secrets"} for r in rows],
                                           indent=1).encode()
    files["secrets.json"] = json.dumps({**{k: v for k, v in stored.items() if k in SECRET_FIELDS},
                                        "backends": {str(r["id"]): r["secrets"] for r in rows if r["secrets"]}}
                                       ).encode()
    for vm in tables["vms"]:
        if vm["status"] not in db.GONE:
            files.update(_state_files(vm["name"]))
    files.update(_custom_images())
    if include_logs:
        files.update(_job_logs())
    return files, {"installation_id": inst, "created_by": by, "app": "vm-portal",
                   "counts": {"users": len(tables["users"]), "vms": sum(v["status"] not in db.GONE
                                                                          for v in tables["vms"]),
                              "images": len({n.split("/")[1] for n in files if n.startswith("images/")}),
                              "logs": include_logs}}
