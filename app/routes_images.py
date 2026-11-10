"""Images pages (admin): vCenter templates, image definitions, per-image settings and builds."""
import asyncio
import fnmatch
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from . import db, images, packer
from .auth import client_ip, require_admin
from .platforms import current as platform

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


def render(*args, **kwargs):
    from .main import render as _render
    return _render(*args, **kwargs)


async def vcenter(fn, *args):
    from .main import vcenter as _vcenter
    return await _vcenter(fn, *args)


def _audit(request: Request, user: dict, action: str, detail: str = "") -> None:
    db.audit(action, detail, username=user["username"], ip=client_ip(request))


def group_templates(tmpls: list) -> list[dict]:
    """vCenter templates grouped by the image they were built from, newest first per group."""
    catalog = images.catalog()
    groups: dict[str, dict] = {}
    for t in tmpls:
        img = images.for_template(t.name, catalog)
        key = img.id if img else "~other"
        g = groups.setdefault(key, {"image": img, "templates": []})
        g["templates"].append(t)
    order = {img.id: i for i, img in enumerate(catalog)}
    return sorted(groups.values(), key=lambda g: order.get(g["image"].id, 999) if g["image"] else 1000)


def _image_or_404(image_id: str) -> images.ImageDef:
    img = images.get(image_id)
    if img is None:
        raise HTTPException(404, "No such image definition")
    return img


def _last_builds(limit: int = 20, target_prefix: str | None = None) -> list[dict]:
    if target_prefix:
        return db.query("SELECT * FROM jobs WHERE kind='packer-build' AND target LIKE ? ORDER BY id DESC LIMIT ?",
                        (target_prefix + "-%", limit))
    return db.query("SELECT * FROM jobs WHERE kind='packer-build' ORDER BY id DESC LIMIT ?", (limit,))


@router.get("/images")
async def images_page(request: Request, tab: str = "templates"):
    tmpls, err = await vcenter(platform().templates)
    catalog = images.catalog()
    defs = []
    for img in catalog:
        cfg = images.config(img) if img.usable else None
        prefix = img.manifest.template_prefix if img.usable else None
        defs.append({"img": img, "cfg": cfg, "problems": packer.preflight(img, cfg) if img.usable else [img.error],
                     "building": packer.building(img.id),
                     "last": (_last_builds(1, prefix) or [None])[0] if prefix else None})
    return render(request, "images.html", tab=tab, groups=group_templates(tmpls or []), defs=defs,
                  builds=_last_builds(), error=err, shared_problems=packer.preflight())


@router.post("/images/templates/{name}/delete")
async def template_delete(request: Request, name: str, user: dict = Depends(require_admin)):
    tmpls, err = await vcenter(platform().templates)
    if err or tmpls is None:
        raise HTTPException(502, err or "vCenter is not configured")
    group = next((g for g in group_templates(tmpls) if any(t.name == name for t in g["templates"])), None)
    if group is None:
        raise HTTPException(404, "Unknown template")
    if group["templates"][0].name == name:
        raise HTTPException(400, "Refusing to delete the newest template of an image")
    _, err = await vcenter(platform().delete_template, name)
    if err:
        raise HTTPException(502, err)
    _audit(request, user, "image.template_deleted", name)
    return RedirectResponse("/images", status_code=303)


# --- one image definition ----------------------------------------------------------

def _render_image(request: Request, img: images.ImageDef, status_code: int = 200, cfg=None, **ctx):
    cfg = cfg or (images.config(img) if img.usable else None)
    prefix = img.manifest.template_prefix if img.usable else None
    return render(request, "image_detail.html", status_code=status_code, img=img, cfg=cfg,
                  problems=packer.preflight(img, cfg) if img.usable else [img.error],
                  building=packer.building(img.id), builds=_last_builds(10, prefix) if prefix else [],
                  files=_files(img), **ctx)


def _files(img: images.ImageDef) -> list[str]:
    from .editor import list_files
    return list_files(img)


@router.get("/images/defs/{image_id}")
async def image_detail(request: Request, image_id: str):
    return _render_image(request, _image_or_404(image_id))


@router.post("/images/defs/{image_id}/settings")
async def image_settings(request: Request, image_id: str, user: dict = Depends(require_admin)):
    img = _image_or_404(image_id)
    if not img.usable:
        raise HTTPException(400, img.error)
    form = await request.form()
    data = {
        "enabled": form.get("enabled") == "on",
        "iso_url": str(form.get("iso_url", "")),
        "cpus": form.get("cpus"), "memory_mb": form.get("memory_mb"), "disk_mb": form.get("disk_mb"),
        "keep": form.get("keep"),
        "params": {p.name: str(form.get(f"param_{p.name}", "")).strip() for p in img.manifest.parameters},
    }
    try:
        cfg = images.validate_config(img, data)
    except (ValidationError, ValueError) as e:
        msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()) \
            if isinstance(e, ValidationError) else str(e)
        return _render_image(request, img, status_code=400, error=msg)
    images.save_config(img, cfg, by=user["username"])
    _audit(request, user, "image.settings_changed", img.id)
    return _render_image(request, img, cfg=cfg, notice="Image settings saved.")


@router.post("/images/defs/{image_id}/build")
async def image_build(request: Request, image_id: str, user: dict = Depends(require_admin)):
    img = _image_or_404(image_id)
    if packer.building(img.id):
        raise HTTPException(409, "A build of this image is already running")
    problems = packer.preflight(img)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    job_id = packer.start_build(img, user_id=user["id"])
    _audit(request, user, "image.build", img.id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/images/defs/{image_id}/isos")
async def image_isos(request: Request, image_id: str):
    """htmx: ISO picker for this image, preselecting one that matches the manifest's pattern."""
    img = _image_or_404(image_id)
    cfg = images.config(img) if img.usable else None
    value = cfg.iso_url if cfg else ""
    isos, error = None, None
    try:
        isos = await asyncio.wait_for(asyncio.to_thread(platform().list_isos), timeout=90)
    except Exception as e:  # noqa: BLE001 - shown next to a plain text field
        error = getattr(e, "msg", None) or str(e)
    if isos and not value and img.usable and img.manifest.iso.match:
        value = next((i for i in isos if fnmatch.fnmatch(i.split("]")[-1].strip().split("/")[-1],
                                                        img.manifest.iso.match)), "")
    return render(request, "_iso_picker.html", isos=isos, value=value, error_text=error,
                  hint=img.manifest.iso.hint if img.usable else "")

