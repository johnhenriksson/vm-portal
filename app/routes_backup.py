"""Backup and restore in the browser: Settings → Backup, VM state history, and restoring
during first-run setup.

Every action needs the acting admin's password (throttled). An uploaded backup is
decrypted and checked in memory; the result waits in this process's memory (never on
disk in plain text) for the admin to confirm, bound to their session, for 15 minutes.
"""
import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile as StarletteUpload

from . import db, jobs, provision, users
from .admin import audit, confirm_password, render_settings
from .auth import client_ip, require_admin
from .backup import archive, collect, restore
from .backup.archive import BackupError
from .config import get_bootstrap, get_settings

router = APIRouter()
PENDING_SECONDS = 15 * 60
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def render(*args, **kwargs):
    from .main import render as _render
    return _render(*args, **kwargs)


@dataclass
class Pending:
    """An opened backup waiting for the admin's decision."""
    token: str
    owner: str                       # session id (admin) or setup nonce
    backup: archive.Backup
    filename: str
    passphrase: bool                 # encrypted with a passphrase (else: to public keys)
    full: restore.FullRestore | None = None
    full_error: str | None = None
    vm_imports: list[restore.VmImport] = field(default_factory=list)
    expires: float = field(default_factory=lambda: time.monotonic() + PENDING_SECONDS)


# One at a time: a backup can be large, and the plain text should live as briefly as possible.
_pending: dict[str, Pending] = {}


def _put(p: Pending) -> None:
    _pending.clear()
    _pending[p.token] = p


def _take(token: str, owner: str, *, keep: bool = False) -> Pending:
    p = _pending.get(token) if isinstance(token, str) and TOKEN_RE.match(token) else None
    if p is None or p.expires < time.monotonic() or not secrets.compare_digest(p.owner, owner):
        _pending.clear()
        raise HTTPException(410, "That backup is no longer open. Upload it again.")
    if not keep:
        _pending.pop(token, None)
    return p


def discard_pending() -> None:
    _pending.clear()


def _sid(request: Request) -> str:
    return str(request.session.get("sid", ""))


def filename(kind: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"vm-portal-{kind}-{collect.installation_id()[:8]}-{stamp}{archive.SUFFIX}"


async def _read_upload(file: UploadFile | None, secret: str) -> tuple[archive.Backup, bool, str]:
    if not isinstance(file, StarletteUpload) or not file.filename:
        raise ValueError("Choose a backup file")
    limit = get_bootstrap().backup_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"The file is larger than the limit ({get_bootstrap().backup_max_mb} MB, BACKUP_MAX_MB)")
    b = await asyncio.to_thread(archive.read, data, secret, limit)
    return b, archive.encrypted_to_passphrase(data), file.filename[:200]


# --- Settings → Backup ------------------------------------------------------------------

def backup_context(request: Request) -> dict:
    from .backup import schedule
    vms = [v for v in db.active_vms() if (provision.vm_dir(v["name"]) / "terraform.tfstate").is_file()]
    preselect = request.query_params.getlist("vm")
    try:
        schedule.check_dir()
        dir_error = None
    except ValueError as e:
        dir_error = str(e)
    return {"backup_vms": vms, "preselect": [n for n in preselect if any(v["name"] == n for v in vms)],
            "suggested_passphrase": secrets.token_urlsafe(24),
            "has_recipients": bool(get_settings().backup_recipients),
            "max_mb": get_bootstrap().backup_max_mb, "backup_dir": str(get_bootstrap().backup_dir),
            "backup_dir_error": dir_error, "backup_health": schedule.health(), "backup_status": schedule.status(),
            "backup_next": schedule.next_due(), "backup_running": jobs.is_locked("backup"),
            "pre_restore": (get_bootstrap().data_dir / restore.PRE_RESTORE / "RESTORED_AT").is_file()}


@router.post("/settings/backup/run-now")
async def run_now(request: Request, user: dict = Depends(require_admin)):
    from .backup import schedule
    try:
        if not get_settings().backup_recipients:
            raise ValueError("Add at least one age public key first")
        if jobs.is_locked("backup"):
            raise ValueError("A backup is already running")
        busy = collect.busy_vms()
        if busy:
            raise ValueError(f"Wait for the running job on {', '.join(busy)} to finish")
        job_id = schedule.submit(by=user["username"], user_id=user["id"])
    except ValueError as e:
        return render_settings(request, "backup", status_code=400, error=str(e))
    audit(request, user, "backup.run_now")
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/settings/backup/download")
async def download(request: Request, user: dict = Depends(require_admin), what: str = Form("full"),
                   vms: list[str] = Form([]), include_logs: str = Form(""), encrypt: str = Form("passphrase"),
                   passphrase: str = Form("", max_length=1024), confirm: str = Form("")):
    def fail(msg: str, code: int = 400):
        return render_settings(request, "backup", status_code=code, error=msg)

    try:
        await asyncio.to_thread(confirm_password, request, user, confirm, "backup.download",
                                "Enter your password to download a backup")
        if encrypt == "keys":
            recipients, passphrase = get_settings().backup_recipients, None
            if not recipients:
                raise ValueError("Add public keys under Scheduled backups first, or use a passphrase")
        else:
            recipients = None
            archive.check_passphrase(passphrase)
        if what == "vms":
            files, meta = collect.vms(vms, by=user["username"])
            kind = "vms"
        else:
            files, meta = await asyncio.to_thread(collect.full, user["username"], include_logs == "on")
            kind = "full"
        data = await asyncio.to_thread(archive.write, kind, files, passphrase=passphrase, recipients=recipients,
                                       **meta)
    except ValueError as e:
        return fail(str(e))
    if len(data) > get_bootstrap().backup_max_mb * 1024 * 1024:
        return fail("The backup is larger than BACKUP_MAX_MB, so it couldn't be restored: raise the limit, or "
                    "leave the job logs out")
    name = filename(kind)
    detail = f"{kind}{': ' + ', '.join(meta.get('vms', [])) if kind == 'vms' else ''}, {len(data) // 1024} KB, " \
             f"{'passphrase' if passphrase else 'public keys'}{', with logs' if include_logs == 'on' else ''}"
    audit(request, user, "backup.downloaded", detail[:300])
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/settings/backup/open")
async def open_backup(request: Request, user: dict = Depends(require_admin), file: UploadFile | None = File(None),
                      secret: str = Form("", max_length=1024), confirm: str = Form("")):
    try:
        await asyncio.to_thread(confirm_password, request, user, confirm, "backup.open",
                                "Enter your password to open a backup")
        b, passphrase, name = await _read_upload(file, secret)
    except ValueError as e:   # BackupError included
        audit(request, user, "backup.open_failed", str(e)[:200])
        return render_settings(request, "backup", status_code=400, error=str(e))
    p = Pending(secrets.token_urlsafe(24), _sid(request), b, name, passphrase)
    if b.kind == "full":
        try:
            p.full = await asyncio.to_thread(restore.plan_full_restore, b, public_key_encrypted=not passphrase)
        except BackupError as e:
            p.full_error = str(e)
    try:
        p.vm_imports = restore.plan_vm_imports(b, user)
        await asyncio.to_thread(restore.check_vcenter, p.vm_imports)
    except BackupError as e:
        p.full_error = p.full_error or str(e)
    _put(p)
    audit(request, user, "backup.opened", f"{b.kind} backup {name} from {b.manifest.get('created_at', '?')}")
    return render(request, "backup_preview.html", p=p, setup=False)


@router.post("/settings/backup/discard")
async def discard(request: Request, user: dict = Depends(require_admin)):
    discard_pending()
    return RedirectResponse("/settings/backup", status_code=303)


@router.post("/settings/backup/import-vms")
async def import_vms(request: Request, user: dict = Depends(require_admin), token: str = Form(""),
                     names: list[str] = Form([])):
    p = _take(token, _sid(request))
    if not names:
        return render_settings(request, "backup", status_code=400, error="Choose at least one VM to import")
    # The VMs may have changed since the preview: check again, then import without awaiting in between.
    fresh = restore.plan_vm_imports(p.backup, user)
    await asyncio.to_thread(restore.check_vcenter, fresh)
    try:
        done = restore.import_vms(fresh, names, user, p.filename)
    except ValueError as e:
        return render_settings(request, "backup", status_code=400, error=str(e))
    return render_settings(request, "backup", notice=f"Imported {', '.join(done)}. Check them on the VM list.")


async def _restore_full(p: Pending, by: str) -> None:
    if p.full is None:
        raise ValueError(p.full_error or "This backup can't be restored")
    if jobs.running():
        raise ValueError(f"Wait for the {jobs.running()} running job(s) to finish")
    jobs.paused = "A backup is being restored. Try again in a minute."
    try:
        await asyncio.to_thread(restore.apply_full_restore, p.full, by)
    finally:
        jobs.paused = None


@router.post("/settings/backup/restore")
async def restore_full(request: Request, user: dict = Depends(require_admin), token: str = Form(""),
                       typed: str = Form("")):
    p = _take(token, _sid(request), keep=True)
    if typed.strip() != "RESTORE":
        return render(request, "backup_preview.html", status_code=400, p=p, setup=False,
                      error="Type RESTORE to confirm")
    _take(token, _sid(request))
    ip = client_ip(request)
    try:
        await _restore_full(p, f"{user['username']} ({ip})")
    except ValueError as e:
        return render_settings(request, "backup", status_code=409, error=str(e))
    request.session.clear()
    return RedirectResponse("/login?restored=1", status_code=303)


@router.post("/settings/backup/check-vms")
async def check_vms(request: Request, user: dict = Depends(require_admin)):
    """After a restore or a move: does every VM's recorded id still match vCenter?"""
    entries = []
    for vm in db.active_vms():
        moid = provision.vm_moid(vm["name"])
        imp = restore.VmImport(vm["name"], None, b"", {}, moid or "", None)
        if not moid:
            imp.problems.append("No OpenTofu state: the portal can only power it on or off")
        entries.append(imp)
    await asyncio.to_thread(restore.check_vcenter, entries)
    return render_settings(request, "backup", vm_check=entries)


# --- VM state history ---------------------------------------------------------------------

@router.post("/vms/{name}/state-history/{version}/restore")
async def state_history_restore(request: Request, name: str, version: str, user: dict = Depends(require_admin),
                                confirm: str = Form("")):
    vm = db.active_vm(name)
    if not vm:
        raise HTTPException(404, f"No VM named {name!r}")
    d = provision.vm_dir(name)
    match = [p for p in provision.state_history_files(d) if p.name == version]
    if not match:
        raise HTTPException(404, "No such state version")
    if jobs.is_locked(f"vm:{name}"):
        raise HTTPException(409, "A job is running for this VM")
    try:
        await asyncio.to_thread(confirm_password, request, user, confirm, f"vm.state_rollback {name}",
                                "Enter your password to roll back the VM's state")
        data = match[0].read_bytes()
        moid = restore.check_state(name, data)
        current = provision.vm_moid(name)
        if current and current != moid:
            raise ValueError(f"That version belongs to another VM in vCenter ({moid}, now {current})")
        imp = restore.VmImport(name, None, data, {}, moid, None)
        await asyncio.to_thread(restore.check_vcenter, [imp])
        if imp.problems:
            raise ValueError(imp.problems[0])
        if jobs.is_locked(f"vm:{name}"):
            raise ValueError("A job started for this VM; try again when it's done")
        provision.keep_state_history(d)
        provision.write_private(d / "terraform.tfstate", data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    audit(request, user, "vm.state_rolled_back", f"{name} to {version}")
    return RedirectResponse(f"/vms/{name}", status_code=303)


# --- first-run setup: restore instead of starting empty -------------------------------------

def _setup_nonce(request: Request) -> str:
    nonce = request.session.get("setup_restore")
    if not isinstance(nonce, str):
        nonce = request.session["setup_restore"] = secrets.token_urlsafe(24)
    return nonce


@router.get("/setup/restore")
async def setup_restore_form(request: Request):
    if db.user_count():
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup_restore.html", max_mb=get_bootstrap().backup_max_mb)


@router.post("/setup/restore")
async def setup_restore_open(request: Request, token: str = Form(""), file: UploadFile | None = File(None),
                             secret: str = Form("", max_length=1024)):
    if db.user_count():
        return RedirectResponse("/login", status_code=303)
    ip = client_ip(request)

    def fail(msg: str, code: int = 400):
        return render(request, "setup_restore.html", status_code=code, error=msg,
                      max_mb=get_bootstrap().backup_max_mb)

    if users.ip_throttle.retry_after(ip):
        return fail("Too many attempts. Try again later.", 429)
    if not users.check_setup_token(token):
        users.ip_throttle.fail(ip)
        db.audit("setup.bad_token", ip=ip)
        return fail("Wrong setup token. It is printed in the container log.", 403)
    try:
        b, passphrase, name = await _read_upload(file, secret)
        if b.kind != "full":
            raise ValueError("This backup only holds VM state. Set up the portal first, then import the VMs "
                             "under Settings → Backup.")
        plan = await asyncio.to_thread(restore.plan_full_restore, b, public_key_encrypted=not passphrase)
    except ValueError as e:
        users.ip_throttle.fail(ip)
        return fail(str(e))
    p = Pending(secrets.token_urlsafe(24), _setup_nonce(request), b, name, passphrase, full=plan)
    _put(p)
    return render(request, "backup_preview.html", p=p, setup=True)


@router.post("/setup/restore/confirm")
async def setup_restore_confirm(request: Request, token: str = Form(""), typed: str = Form("")):
    if db.user_count():
        return RedirectResponse("/login", status_code=303)
    nonce = request.session.get("setup_restore")
    p = _take(token, nonce if isinstance(nonce, str) else "", keep=True)
    if typed.strip() != "RESTORE":
        return render(request, "backup_preview.html", status_code=400, p=p, setup=True, error="Type RESTORE to confirm")
    _take(token, nonce)
    try:
        await _restore_full(p, f"setup ({client_ip(request)})")
    except ValueError as e:
        return render(request, "setup_restore.html", status_code=409, error=str(e),
                      max_mb=get_bootstrap().backup_max_mb)
    request.session.clear()
    return RedirectResponse("/login?restored=1", status_code=303)
