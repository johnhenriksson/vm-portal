"""Restoring from a backup: VM state import (this file's first half) and full restore."""
import json
import logging
import secrets as pysecrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import db, jobs, provision, users
from ..config import get_bootstrap
from .archive import Backup, BackupError
from .checks import check_state, check_tfvars
from .collect import vm_entries

log = logging.getLogger(__name__)


# --- VM state import ---------------------------------------------------------------------

@dataclass
class VmImport:
    name: str
    row: object                     # checks.VmRow
    state: bytes
    tfvars: dict
    moid: str
    owner: str | None               # username in the backup
    owner_id: int | None = None     # who owns it here
    replace: bool = False           # this portal already has the VM; its state gets replaced
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def plan_vm_imports(b: Backup, acting_user: dict) -> list[VmImport]:
    """Check each VM in the backup against this portal (not yet against vCenter)."""
    out = []
    for e in vm_entries(b):
        r = e["row"]
        problems: list[str] = []
        try:
            moid = check_state(r.name, e["state"])
            if e["tfvars"] is None:
                raise BackupError(f"{r.name}: the backup has no variables file for it")
            tfvars = check_tfvars(r.name, e["tfvars"])
        except BackupError as err:
            out.append(VmImport(r.name, r, e["state"], {}, "", e["owner"], problems=[str(err)]))
            continue
        imp = VmImport(r.name, r, e["state"], tfvars, moid, e["owner"])
        local = db.active_vm(r.name)
        if local:
            current = provision.vm_moid(r.name)
            if current and current != moid:
                problems.append(f"This portal already has a VM named {r.name} ({current}); the backup's is {moid}")
            else:
                imp.replace = True
                imp.owner_id = local["owner_id"]
                imp.notes.append("Already in this portal: its state is replaced (the current one is kept in its history)")
        else:
            if (get_bootstrap().vms_dir / r.name).exists():
                problems.append(f"A leftover folder for {r.name} is in the way; remove it from the data volume first")
            if r.ip and r.ip_mode == "static" and r.ip in db.reserved_ips():
                problems.append(f"{r.ip} is already used by another VM in this portal")
            u = users.by_name(e["owner"]) if e["owner"] else None
            if u and not u["disabled"]:
                imp.owner_id = u["id"]
            else:
                imp.owner_id = acting_user["id"]
                imp.notes.append(f"Owner {e['owner'] or '(none)'} isn't a user here: you become the owner")
        if jobs.is_locked(f"vm:{r.name}"):
            problems.append("A job is running for this VM")
        imp.problems = problems
        out.append(imp)
    return out


def check_vcenter(imports: list[VmImport]) -> None:
    """Blocking: confirm each VM's id belongs to that very VM, inside this portal's datacenter
    (and folder), and that vm-portal created it. Otherwise importing the state would hand the
    portal, and a later destroy, some other VM."""
    from ..platforms import current as platform
    for imp in imports:
        if not imp.problems:
            imp.problems.extend(platform().import_problems(imp.moid, imp.name, imp.tfvars))


def import_vms(imports: list[VmImport], names: list[str], user: dict, source: str) -> list[str]:
    """Write the chosen, checked VMs into this portal. Synchronous and without awaits, so no
    job can start in between. Returns the imported names."""
    chosen = [i for i in imports if i.name in set(names)]
    for imp in chosen:
        if not imp.ok:
            raise ValueError(f"{imp.name} can't be imported: {imp.problems[0]}")
        if jobs.is_locked(f"vm:{imp.name}"):
            raise ValueError(f"A job started for {imp.name}; try again when it's done")
    done = []
    for imp in chosen:
        d = provision.vm_dir(imp.name)
        r = imp.row
        if imp.replace:
            provision.keep_state_history(d)
        else:
            d.mkdir(mode=0o700)
        provision.write_private(d / "terraform.tfstate", imp.state)
        provision.write_private(d / "terraform.tfvars.json", json.dumps(imp.tfvars, indent=2).encode())
        if not imp.replace:
            db.execute(
                "INSERT INTO vms (name, status, template, cpus, memory_mb, disk_gb, ip_mode, ip, has_compose,"
                " created_at, owner_id, console_login, vm_user, image_id, notes, autostart)"
                " VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r.name, r.template, r.cpus, r.memory_mb, r.disk_gb, r.ip_mode, r.ip, r.has_compose,
                 r.created_at, imp.owner_id, r.console_login, r.vm_user, r.image_id, r.notes, r.autostart))
        db.audit("vm.state_imported", f"{imp.name} ({imp.moid}){' replaced' if imp.replace else ''} from {source}",
                 username=user["username"])
        done.append(imp.name)
    return done


# --- full restore ----------------------------------------------------------------------------

STAGING = ".restore-staging"
PRE_RESTORE = ".pre-restore"
SWAPPED = ("portal.db", "vms", "images", "jobs")   # what a full restore replaces in DATA_DIR


@dataclass
class FullRestore:
    backup: Backup
    users: list
    settings: dict
    secrets: dict
    vms: list
    states: dict                    # name -> (state bytes, tfvars dict)
    image_settings: list
    images: dict                    # id -> {relpath: bytes}
    jobs: list
    audit: list
    logs: dict                      # job id -> bytes
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        m = self.backup.manifest
        return {"created_at": m.get("created_at", ""), "created_by": m.get("created_by", ""),
                "installation_id": m.get("installation_id", ""), "users": len(self.users),
                "admins": sum(u.role == "admin" and not u.disabled for u in self.users),
                "vms": sum(v.status not in db.GONE for v in self.vms), "images": len(self.images),
                "jobs": len(self.jobs), "logs": len(self.logs)}


def _check_settings(values: dict) -> None:
    """Each stored value on its own (the environment may hide a bad one today, not tomorrow),
    then all of them together with the environment, as the portal will run them."""
    from pydantic import ValidationError
    from ..config import Settings, build

    class StoredOnly(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, *args, **kwargs):
            return (init_settings,)

    def error(err: dict) -> BackupError:
        where = ".".join(map(str, err["loc"]))
        return BackupError(f"Settings: {where + ': ' if where else ''}{err['msg']}")

    try:
        StoredOnly(**values)
    except ValidationError as e:
        field_errors = [err for err in e.errors() if err["loc"] and err["loc"][0] in values]
        if field_errors:
            raise error(field_errors[0]) from None
        # Otherwise only a combination the environment may complete: checked with it below.
    try:
        build(values)
    except ValidationError as e:
        raise error(e.errors()[0]) from None


def plan_full_restore(b: Backup, *, public_key_encrypted: bool = False) -> FullRestore:
    """Validate a full backup completely, before anything is changed."""
    from .. import editor, images
    from ..config import EDITABLE, SECRET_FIELDS, build, env_overrides
    from .checks import AuditRow, ImageSettingRow, JobRow, UserRow, VmRow, rows
    from .collect import installation_id

    if b.kind != "full":
        raise BackupError("This backup only holds VM state; use it to import VMs, not to restore the portal")
    warnings: list[str] = []

    user_rows = rows(UserRow, b.json("db/users.json", []), "Users")
    if len({u.id for u in user_rows}) != len(user_rows) or len({u.username for u in user_rows}) != len(user_rows):
        raise BackupError("Users: duplicate ids or usernames")
    if not any(u.role == "admin" and not u.disabled for u in user_rows):
        raise BackupError("The backup has no active admin, so nobody could log in after restoring it")
    user_ids = {u.id for u in user_rows}

    stored = b.json("db/settings.json", {})
    secrets = b.json("secrets.json", {})
    if not isinstance(stored, dict) or not isinstance(secrets, dict):
        raise BackupError("Settings: expected an object")
    unknown = sorted((set(stored) - EDITABLE) | (set(secrets) - SECRET_FIELDS))
    if unknown or set(stored) & SECRET_FIELDS:
        raise BackupError(f"Settings: unexpected keys {', '.join(unknown)[:200]}")
    _check_settings({**stored, **secrets})

    vm_rows = rows(VmRow, b.json("db/vms.json", []), "VMs")
    if any(v.owner_username is not None or v.id is None for v in vm_rows):
        raise BackupError("VMs: rows in the wrong format for a full backup")
    if len({v.id for v in vm_rows}) != len(vm_rows):
        raise BackupError("VMs: duplicate ids")
    active = [v.name for v in vm_rows if v.status not in db.GONE]
    if len(set(active)) != len(active):
        raise BackupError("VMs: the same name is used twice")
    states = {}
    for v in vm_rows:
        if v.owner_id is not None and v.owner_id not in user_ids:
            raise BackupError(f"VMs: {v.name} belongs to a user who isn't in the backup")
        state = b.files.get(f"vms/{v.name}/terraform.tfstate")
        tfvars = b.files.get(f"vms/{v.name}/terraform.tfvars.json")
        if state is None and tfvars is None:
            continue
        if v.status in db.GONE or state is None or tfvars is None:
            raise BackupError(f"VMs: unexpected or incomplete state files for {v.name}")
        check_state(v.name, state)
        states[v.name] = (state, check_tfvars(v.name, tfvars))
    for name in b.files:
        if name.startswith("vms/") and name.split("/")[1] not in states:
            raise BackupError(f"{name}: not a VM in the backup")

    job_rows = rows(JobRow, b.json("db/jobs.json", []), "Jobs")
    job_ids = {j.id for j in job_rows}
    if len(job_ids) != len(job_rows) or any(j.user_id is not None and j.user_id not in user_ids for j in job_rows):
        raise BackupError("Jobs: duplicate ids or unknown users")
    logs = {}
    for name, data in b.files.items():
        if name.startswith("jobs/"):
            jid = int(name.removeprefix("jobs/").removesuffix(".log"))
            if jid not in job_ids:
                raise BackupError(f"{name}: no such job in the backup")
            logs[jid] = data
    audit_rows = rows(AuditRow, b.json("db/audit.json", []), "Audit log")
    if len({a.id for a in audit_rows}) != len(audit_rows):
        raise BackupError("Audit log: duplicate ids")
    img_settings = rows(ImageSettingRow, b.json("db/image_settings.json", []), "Image settings")
    if len({i.image_id for i in img_settings}) != len(img_settings):
        raise BackupError("Image settings: duplicate images")

    builtin = {i.id for i in images.catalog() if i.builtin}
    defs: dict[str, dict[str, bytes]] = {}
    for name, data in b.files.items():
        if name.startswith("images/"):
            _, image_id, rel = name.split("/", 2)
            if image_id in builtin:
                raise BackupError(f"{name}: {image_id} is a built-in image here; it can't be replaced")
            try:
                editor.check_path(rel)
            except ValueError as e:
                raise BackupError(f"{name}: {e}") from None
            defs.setdefault(image_id, {})[rel] = data
    for image_id, files in defs.items():
        if images.MANIFEST not in files:
            raise BackupError(f"Image {image_id}: no {images.MANIFEST}")
        if len(files) > editor.MAX_FILES or sum(map(len, files.values())) > editor.MAX_TOTAL or \
                any(len(d) > editor.MAX_FILE for d in files.values()):
            raise BackupError(f"Image {image_id}: larger than the editor allows")

    # What to tell the admin before they confirm.
    if b.manifest.get("installation_id") != installation_id():
        warnings.append("This backup comes from another installation. If that portal is still running, shut it "
                        "down for good: two portals managing the same VMs will undo each other's changes.")
    else:
        warnings.append("This backup comes from this installation: everything since it was made is replaced.")
    if public_key_encrypted:
        warnings.append("This file is encrypted to a public key, and anyone with the public key can make such a "
                        "file. Only restore backups from your own backup storage.")
    effective = build({**stored, **secrets})
    if effective.vsphere_insecure:
        warnings.append("The restored settings turn off TLS certificate verification for vCenter.")
    if defs:
        if effective.allow_image_editing:
            warnings.append(f"{len(defs)} custom image definition(s) are restored. They are code that runs inside "
                            "the portal with the vCenter credentials: check them under Images.")
        else:
            warnings.append(f"{len(defs)} custom image definition(s) are restored disabled, because image editing is "
                            "off: review them before enabling.")
    overridden = sorted(set(stored) & env_overrides())
    if overridden:
        warnings.append("The environment overrides these restored settings: " + ", ".join(overridden))
    if not secrets.get("vsphere_password") and "vsphere_password" not in env_overrides():
        warnings.append("The backup has no vCenter password (it came from the environment there): set it in "
                        "Settings → vCenter or VSPHERE_PASSWORD.")
    warnings.append("Everyone is logged out, you included: log in with an admin account from the backup.")

    return FullRestore(b, user_rows, stored, secrets, vm_rows, states, img_settings, defs, job_rows,
                       audit_rows, logs, warnings)


def _build_staging(plan: FullRestore, by: str) -> Path:
    import sqlite3

    from .. import crypto
    from ..config import build
    from .collect import installation_id
    b = get_bootstrap()
    staging = b.data_dir / STAGING
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    local_inst = installation_id()
    editing = build({**plan.settings, **plan.secrets}).allow_image_editing

    c = sqlite3.connect(staging / "portal.db")
    try:
        db.init_schema(c)
        ts = db.now()
        for u in plan.users:
            c.execute("INSERT INTO users (id, username, password_hash, role, disabled, session_version, created_at,"
                      " last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (u.id, u.username, u.password_hash, u.role, u.disabled, u.session_version, u.created_at,
                       u.last_login_at))
        for k, v in plan.settings.items():
            c.execute("INSERT INTO settings (key, value, encrypted, updated_at, updated_by) VALUES (?, ?, 0, ?, ?)",
                      (k, json.dumps(v), ts, "restore"))
        for k, v in plan.secrets.items():   # re-encrypted with this portal's own key
            c.execute("INSERT INTO settings (key, value, encrypted, updated_at, updated_by) VALUES (?, ?, 1, ?, ?)",
                      (k, crypto.encrypt(json.dumps(v)), ts, "restore"))
        for k, v in (("installation_id", local_inst), ("session_epoch", pysecrets.token_urlsafe(16))):
            c.execute("INSERT INTO settings (key, value, encrypted, updated_at, updated_by) VALUES (?, ?, 0, ?, 'system')",
                      (k, json.dumps(v), ts))
        for v in plan.vms:
            c.execute("INSERT INTO vms (id, name, status, template, cpus, memory_mb, disk_gb, ip_mode, ip, has_compose,"
                      " created_at, last_job_id, owner_id, console_login, vm_user, image_id, notes, autostart)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (v.id, v.name, v.status, v.template, v.cpus, v.memory_mb, v.disk_gb, v.ip_mode, v.ip,
                       v.has_compose, v.created_at, v.last_job_id, v.owner_id, v.console_login, v.vm_user,
                       v.image_id, v.notes, v.autostart))
        for i in plan.image_settings:
            data = json.loads(i.data)
            if i.image_id in plan.images and not editing:
                data["enabled"] = False
            c.execute("INSERT INTO image_settings (image_id, data, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                      (i.image_id, json.dumps(data), i.updated_at, i.updated_by))
        if not editing:
            known = {i.image_id for i in plan.image_settings}
            for image_id in plan.images:
                if image_id not in known:
                    c.execute("INSERT INTO image_settings (image_id, data, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                              (image_id, json.dumps({"enabled": False}), ts, "restore"))
        for j in plan.jobs:
            c.execute("INSERT INTO jobs (id, kind, target, status, created_at, started_at, finished_at, exit_code,"
                      " user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (j.id, j.kind, j.target, j.status, j.created_at, j.started_at, j.finished_at, j.exit_code,
                       j.user_id))
        for a in plan.audit:
            c.execute("INSERT INTO audit (id, ts, username, ip, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
                      (a.id, a.ts, a.username, a.ip, a.action, a.detail))
        m = plan.backup.manifest
        c.execute("INSERT INTO audit (ts, username, action, detail) VALUES (?, ?, 'backup.restored', ?)",
                  (ts, by, f"full backup from {m.get('created_at', '?')} (installation "
                           f"{str(m.get('installation_id', '?'))[:8]})"))
        # Interrupted jobs in the backup can't resume.
        c.execute("UPDATE jobs SET status='failed', finished_at=? WHERE status IN ('queued','running')", (ts,))
        c.execute("UPDATE vms SET status='failed' WHERE status IN ('provisioning','destroying')")
        c.commit()
    finally:
        c.close()
    (staging / "portal.db").chmod(0o600)

    vms_dir = staging / "vms"
    vms_dir.mkdir(mode=0o700)
    for name, (state, tfvars) in plan.states.items():
        d = vms_dir / name
        d.mkdir(mode=0o700)
        provision.write_private(d / "terraform.tfstate", state)
        provision.write_private(d / "terraform.tfvars.json", json.dumps(tfvars, indent=2).encode())
    img_dir = staging / "images"
    img_dir.mkdir()
    for image_id, files in plan.images.items():
        for rel, data in files.items():
            path = img_dir / image_id / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o640)
    jobs_dir = staging / "jobs"
    jobs_dir.mkdir()
    for jid, data in plan.logs.items():
        (jobs_dir / f"{jid}.log").write_bytes(data)
    return staging


def apply_full_restore(plan: FullRestore, by: str) -> None:
    """Replace this portal's data with the backup's. Callers stop jobs and requests first
    (jobs.paused). The current data is kept in DATA_DIR/.pre-restore until the next restore."""
    from .. import auth, config
    from ..platforms import current as platform
    b = get_bootstrap()
    staging = _build_staging(plan, by)
    pre = b.data_dir / PRE_RESTORE
    shutil.rmtree(pre, ignore_errors=True)
    pre.mkdir(mode=0o700)
    db.close()
    platform().close()
    moved, placed = [], []
    try:
        for item in SWAPPED:
            if (b.data_dir / item).exists():
                (b.data_dir / item).rename(pre / item)
                moved.append(item)
        for item in SWAPPED:
            (staging / item).rename(b.data_dir / item)
            placed.append(item)
    except OSError:
        log.exception("Restore failed while swapping; putting the previous data back")
        for item in placed:
            target = b.data_dir / item
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        for item in moved:
            (pre / item).rename(b.data_dir / item)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        db.close()
        config.get_settings.cache_clear()
        auth.reset_session_epoch()
        users.setup_token = None
        users.clear_throttles()
    (pre / "RESTORED_AT").write_text(f"{db.now()} by {by}\n")
    log.warning("Restored a full backup; the previous data is in %s", pre)


def undo_full_restore(by: str) -> None:
    """Swap the data from before the last restore back in (command line, portal stopped)."""
    from .. import auth
    b = get_bootstrap()
    pre = b.data_dir / PRE_RESTORE
    if not (pre / "RESTORED_AT").is_file():
        raise ValueError(f"There's no previous data to go back to in {pre}")
    swap = b.data_dir / ".undo-swap"
    shutil.rmtree(swap, ignore_errors=True)
    swap.mkdir(mode=0o700)
    db.close()
    for item in SWAPPED:
        if (b.data_dir / item).exists():
            (b.data_dir / item).rename(swap / item)
        if (pre / item).exists():
            (pre / item).rename(b.data_dir / item)
    shutil.rmtree(pre)
    swap.rename(pre)   # the undone restore can itself be undone
    (pre / "RESTORED_AT").write_text(f"{db.now()} undone by {by}\n")
    db.audit("backup.restore_undone", "", username=by)
    db.set_meta("session_epoch", pysecrets.token_urlsafe(16))
    auth.reset_session_epoch()
