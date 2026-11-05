"""Browse and (when enabled) edit image definitions in the web UI.

Security model
  * Editing Packer definitions is running code: a definition can execute
    commands in the portal container (shell-local), read files (file()) and
    environment variables (env()). So writing is off by default, admin-only, and
    every change is audited and snapshotted. An admin turns it on under
    Settings → Images, confirming with their password; ALLOW_IMAGE_EDITING=false
    in the environment locks it off. Packer runs with a minimal environment
    (jobs.child_env).
  * Built-in definitions are read-only; duplicate one to customise it.
  * Paths are validated component by component (no "..", no hidden files, no
    symlinks), file types are allowlisted, and sizes are capped.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from . import db, images, jobs
from .auth import client_ip, require_admin
from .config import CONTROL_RE, PREFIX_RE, get_settings

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALLOWED_SUFFIXES = (".pkr.hcl", ".pkrtpl.hcl", ".hcl", ".pkrtpl", ".json", ".yaml", ".yml", ".sh", ".ps1",
                    ".psm1", ".cmd", ".bat", ".cfg", ".conf", ".ks", ".seed", ".preseed", ".xml", ".inf",
                    ".service", ".txt", ".md")
ALLOWED_NAMES = {"user-data", "meta-data", "vendor-data", "network-config"}
DENIED_SUFFIXES = (".pkrvars.hcl", ".pkrvars.json", ".auto.pkrvars.hcl")
MAX_FILE = 256 * 1024
MAX_TOTAL = 2 * 1024 * 1024
MAX_FILES = 100
MAX_DEPTH = 4
KEEP_SNAPSHOTS = 20


def editing_enabled() -> bool:
    return get_settings().allow_image_editing


def render(*args, **kwargs):
    from .main import render as _render
    return _render(*args, **kwargs)


def _audit(request: Request, user: dict, action: str, detail: str) -> None:
    db.audit(action, detail, username=user["username"], ip=client_ip(request))


# --- paths and files -----------------------------------------------------------------

def check_path(rel: str) -> tuple[str, ...]:
    """Validate a definition-relative path; returns its components."""
    parts = tuple(rel.strip().split("/"))
    if not rel or len(parts) > MAX_DEPTH or any(not NAME_RE.match(p) for p in parts):
        raise ValueError("Use a relative path of plain names (letters, digits, '.', '_', '-'), at most 4 levels")
    name = parts[-1]
    if name.endswith(DENIED_SUFFIXES):
        raise ValueError("Variable files (*.pkrvars.*) can hold credentials and aren't allowed here")
    if not (name.endswith(ALLOWED_SUFFIXES) or name in ALLOWED_NAMES):
        raise ValueError(f"File type not allowed: {name}")
    return parts


def resolve(img: images.ImageDef, rel: str) -> Path:
    parts = check_path(rel)
    path = img.path
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symbolic links are not allowed")
    root = img.path.resolve()
    if root not in path.resolve().parents:
        raise ValueError("Path is outside the image definition")
    return path


def list_files(img: images.ImageDef) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(img.path):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "packer_cache"
                             and not os.path.islink(os.path.join(dirpath, d)))
        for f in sorted(filenames):
            full = Path(dirpath) / f
            if f.startswith(".") or full.is_symlink():
                continue
            out.append(str(full.relative_to(img.path)))
    return out


def _require_editable(img: images.ImageDef) -> None:
    if not editing_enabled():
        raise HTTPException(403, "Editing image definitions is disabled (Settings → Images)")
    if img.builtin:
        raise HTTPException(403, "Built-in image definitions are read-only. Duplicate it to make changes.")


def _image_or_404(image_id: str) -> images.ImageDef:
    img = images.get(image_id)
    if img is None:
        raise HTTPException(404, "No such image definition")
    return img


def history_dir(image_id: str) -> Path:
    return images.custom_dir() / ".history" / image_id


def snapshot(img: images.ImageDef) -> None:
    """Copy the definition aside before a change; keep the newest few."""
    dest = history_dir(img.id) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copytree(img.path, dest, ignore=_copy_ignore)
    for old in sorted(history_dir(img.id).iterdir())[:-KEEP_SNAPSHOTS]:
        shutil.rmtree(old, ignore_errors=True)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


_IGNORE = shutil.ignore_patterns(".*", "*.pkrvars.*", "packer.log", "packer_cache", "crash.log")


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    """Skip hidden files, local variable files and caches, and never follow a symlink out of the definition."""
    return set(_IGNORE(directory, names)) | {n for n in names if os.path.islink(os.path.join(directory, n))}


def rename_manifest(text: str, name: str, template_prefix: str) -> str:
    """Set name and template_prefix, keeping the manifest's comments and layout."""
    out, n = re.subn(r"(?m)^name:.*$", lambda _: "name: " + json.dumps(name), text, count=1)
    out, p = re.subn(r"(?m)^template_prefix:.*$", lambda _: "template_prefix: " + template_prefix, out, count=1)
    data = yaml.safe_load(out) if n and p else None
    if not isinstance(data, dict) or data.get("name") != name or data.get("template_prefix") != template_prefix:
        data = yaml.safe_load(text) or {}
        data.update(name=name, template_prefix=template_prefix)
        return yaml.safe_dump(data, sort_keys=False)
    return out


def check_content(img: images.ImageDef, rel: str, content: str) -> None:
    data = content.encode("utf-8")
    if len(data) > MAX_FILE:
        raise ValueError(f"Files are limited to {MAX_FILE // 1024} KiB")
    if "\x00" in content:
        raise ValueError("Binary content is not allowed")
    others = [f for f in list_files(img) if f != rel]
    if len(others) + 1 > MAX_FILES:
        raise ValueError(f"A definition can have at most {MAX_FILES} files")
    total = sum((img.path / f).stat().st_size for f in others) + len(data)
    if total > MAX_TOTAL:
        raise ValueError(f"A definition is limited to {MAX_TOTAL // (1024 * 1024)} MiB")
    if rel == images.MANIFEST:
        try:
            images.Manifest.model_validate(yaml.safe_load(content))
        except (yaml.YAMLError, ValidationError, ValueError) as e:
            raise ValueError(f"image.yaml is invalid: {e}") from None


def validation_env(img: images.ImageDef) -> dict[str, str]:
    """The variables a build would get, minus the secret: validating never connects to vCenter."""
    from . import packer
    cfg = images.config(img)
    if not cfg.iso_url:
        cfg = cfg.model_copy(update={"iso_url": "[datastore] iso/not-chosen-yet.iso"})
    env = packer.packer_env(img, cfg, f"{img.manifest.template_prefix}-validate")
    env["PKR_VAR_vsphere_password"] = "not-used-by-validate"
    # Builders require these; placeholders let a definition be checked before vCenter is set up.
    for key in ("server", "user", "datacenter", "cluster", "datastore", "network"):
        env[f"PKR_VAR_vsphere_{key}"] = env[f"PKR_VAR_vsphere_{key}"] or "not-configured"
    return env


def packer_check(img: images.ImageDef) -> tuple[bool, str]:
    """`packer validate` on a copy, first syntax only, then with the portal's variables.

    Validating evaluates variables, locals and templatefile() (so it catches a
    missing variable or a broken installer template) but runs no builder or
    provisioner and doesn't contact vCenter.
    """
    if not shutil.which("packer"):
        return True, "packer is not installed; not checked"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "def"
        shutil.copytree(img.path, work, ignore=_copy_ignore)
        steps = [("Syntax", ["packer", "validate", "-syntax-only", "."], jobs.child_env())]
        note = ""
        if img.usable:
            try:
                steps.append(("With the portal's variables", ["packer", "validate", "."],
                              jobs.child_env(validation_env(img))))
            except (OSError, ValueError) as e:
                note = f"\nNot checked with the portal's variables: {e}"
        for label, cmd, env in steps:
            try:
                r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                return False, f"{label}: packer validate timed out"
            out = (r.stdout + r.stderr).strip()
            if r.returncode != 0:
                return False, f"{label}: failed\n\n{out}"
    return True, "\n".join(f"{label}: OK" for label, *_ in steps) + note


# --- pages ------------------------------------------------------------------------------

def editor_context(img: images.ImageDef, path: str) -> dict:
    files = list_files(img)
    content, error = "", None
    if path not in files and files:
        path = images.MANIFEST if images.MANIFEST in files else files[0]
    if path in files:
        try:
            content = resolve(img, path).read_text(encoding="utf-8")
        except (ValueError, OSError, UnicodeDecodeError) as e:
            error = f"Can't open {path}: {e}"
    hist = history_dir(img.id)
    snaps = sorted((p.name for p in hist.iterdir()), reverse=True) if hist.is_dir() else []
    return {"img": img, "files": files, "path": path, "content": content, "error": error, "snapshots": snaps,
            "editable": editing_enabled() and not img.builtin, "editing_enabled": editing_enabled()}


def page(request: Request, img: images.ImageDef, path: str, status_code: int = 200, **overrides):
    ctx = {**editor_context(img, path), **overrides}
    return render(request, "image_editor.html", status_code=status_code, **ctx)


@router.get("/images/defs/{image_id}/files")
async def editor_page(request: Request, image_id: str, path: str = images.MANIFEST, restored: str = ""):
    img = _image_or_404(image_id)
    return page(request, img, path, notice=f"Restored snapshot {restored}." if restored else None)


def _back(img: images.ImageDef, path: str, **params) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse(f"/images/defs/{img.id}/files?{urlencode({'path': path, **params})}", status_code=303)


@router.post("/images/defs/{image_id}/files/save")
async def save_file(request: Request, image_id: str, user: dict = Depends(require_admin),
                    path: str = Form(...), content: str = Form("", max_length=MAX_FILE * 2)):
    img = _image_or_404(image_id)
    _require_editable(img)
    try:
        target = resolve(img, path)
        check_content(img, path, content)
    except ValueError as e:
        return page(request, img, path, 400, content=content, error=str(e))  # keep what was typed
    await asyncio.to_thread(snapshot, img)
    _write(target, content)
    _audit(request, user, "image.file_saved", f"{img.id}/{path}")
    img = images.get(img.id) or img
    ok, out = await asyncio.to_thread(packer_check, img)
    return page(request, img, path, notice=f"Saved {path}.", check_ok=ok, check_output=out)


@router.post("/images/defs/{image_id}/files/new")
async def new_file(request: Request, image_id: str, user: dict = Depends(require_admin), path: str = Form(...)):
    img = _image_or_404(image_id)
    _require_editable(img)
    try:
        target = resolve(img, path)
        if target.exists():
            raise ValueError(f"{path} already exists")
        check_content(img, path, "")
    except ValueError as e:
        return page(request, img, images.MANIFEST, 400, error=str(e))
    await asyncio.to_thread(snapshot, img)
    _write(target, "")
    _audit(request, user, "image.file_created", f"{img.id}/{path}")
    return _back(img, path)


@router.post("/images/defs/{image_id}/files/delete")
async def delete_file(request: Request, image_id: str, user: dict = Depends(require_admin), path: str = Form(...)):
    img = _image_or_404(image_id)
    _require_editable(img)
    if path == images.MANIFEST:
        raise HTTPException(400, "image.yaml can't be deleted")
    target = resolve(img, path)
    if not target.is_file():
        raise HTTPException(404, "No such file")
    await asyncio.to_thread(snapshot, img)
    target.unlink()
    _audit(request, user, "image.file_deleted", f"{img.id}/{path}")
    return _back(img, images.MANIFEST)


@router.post("/images/defs/{image_id}/validate")
async def validate(request: Request, image_id: str, path: str = Form(images.MANIFEST)):
    img = _image_or_404(image_id)
    ok, out = await asyncio.to_thread(packer_check, img)
    return page(request, img, path, check_ok=ok, check_output=out)


@router.post("/images/defs/{image_id}/history/{snap}/restore")
async def restore(request: Request, image_id: str, snap: str, user: dict = Depends(require_admin)):
    img = _image_or_404(image_id)
    _require_editable(img)
    src = history_dir(img.id) / snap
    if not re.fullmatch(r"\d{8}T\d{12}Z", snap) or not src.is_dir():
        raise HTTPException(404, "No such snapshot")
    try:
        images.Manifest.model_validate(yaml.safe_load((src / images.MANIFEST).read_text()))
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as e:
        raise HTTPException(400, f"That snapshot's image.yaml is invalid: {e}") from None
    await asyncio.to_thread(snapshot, img)
    tmp = img.path.with_name(f".restore-{img.id}")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=_copy_ignore)
    shutil.rmtree(img.path)
    tmp.rename(img.path)
    _audit(request, user, "image.restored", f"{img.id} to {snap}")
    return _back(img, images.MANIFEST, restored=snap)


# --- whole definitions ---------------------------------------------------------------------

@router.post("/images/defs/new")
async def new_definition(request: Request, user: dict = Depends(require_admin), source: str = Form(...),
                         new_id: str = Form(...), name: str = Form(...), template_prefix: str = Form(...)):
    """A new custom definition, started from any existing one (built-in starters or custom)."""
    return create_from(request, user, _image_or_404(source), new_id, name, template_prefix)


@router.post("/images/defs/{image_id}/duplicate")
async def duplicate(request: Request, image_id: str, user: dict = Depends(require_admin),
                    new_id: str = Form(...), name: str = Form(...), template_prefix: str = Form(...)):
    return create_from(request, user, _image_or_404(image_id), new_id, name, template_prefix)


def create_from(request: Request, user: dict, src: images.ImageDef, new_id: str, name: str,
                template_prefix: str) -> RedirectResponse:
    if not editing_enabled():
        raise HTTPException(403, "Editing image definitions is disabled (Settings → Images)")
    new_id, template_prefix, name = new_id.strip(), template_prefix.strip(), name.strip()
    catalog = images.catalog()
    errors = []
    if not images.ID_RE.match(new_id):
        errors.append("The id must be 2-48 lowercase letters, digits, '.' or '-'")
    if any(i.id == new_id for i in catalog):
        errors.append(f"An image with id {new_id!r} already exists")
    if not PREFIX_RE.match(template_prefix):
        errors.append("The template prefix must be 3-64 lowercase letters, digits, '.', '_' or '-'")
    if any(i.usable and i.manifest.template_prefix == template_prefix for i in catalog):
        errors.append(f"The template prefix {template_prefix!r} is already used")
    if not name or len(name) > 80 or CONTROL_RE.search(name):
        errors.append("Give it a name (one line, at most 80 characters)")
    if errors:
        raise HTTPException(400, "; ".join(errors))
    dest = images.custom_dir() / new_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src.path, dest, ignore=_copy_ignore)
    path = dest / images.MANIFEST
    _write(path, f"# Created from {src.id}\n" + rename_manifest(path.read_text(encoding="utf-8"), name, template_prefix))
    _audit(request, user, "image.created", f"{src.id} -> {new_id}")
    return RedirectResponse(f"/images/defs/{new_id}/files", status_code=303)


@router.post("/images/defs/{image_id}/delete")
async def delete_definition(request: Request, image_id: str, user: dict = Depends(require_admin),
                            confirm: str = Form("")):
    img = _image_or_404(image_id)
    _require_editable(img)
    if confirm != img.id:
        raise HTTPException(400, "Type the image id to confirm")
    trash = images.custom_dir() / ".deleted" / f"{img.id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    img.path.rename(trash)  # kept for recovery; templates already built stay in vCenter
    db.execute("DELETE FROM image_settings WHERE image_id=?", (img.id,))
    _audit(request, user, "image.deleted", img.id)
    return RedirectResponse("/images?tab=definitions", status_code=303)
