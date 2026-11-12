"""Images pages (admin): templates on every backend, image definitions, per-image settings and builds."""
import asyncio
import fnmatch
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from . import backends, db, feedback, images, packer
from .auth import client_ip, require_admin

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


def render(*args, **kwargs):
    from .main import render as _render
    return _render(*args, **kwargs)


async def on_backend(b, fn, *args):
    from .main import on_backend as _on_backend
    return await _on_backend(b, fn, *args)


async def all_templates():
    from .main import all_templates as _all
    return await _all()


def _backend_or_404(backend_id) -> backends.Backend:
    b = backends.get(backend_id)
    if b is None:
        raise HTTPException(404, "No such backend")
    return b


def _audit(request: Request, user: dict, action: str, detail: str = "") -> None:
    db.audit(action, detail, username=user["username"], ip=client_ip(request))


def group_templates(tmpls: list) -> list[dict]:
    """Templates grouped by the image they were built from, newest first per group (and
    per backend: the newest on each backend is the default there)."""
    catalog = images.catalog()
    groups: dict[str, dict] = {}
    for t in tmpls:
        img = images.for_template(t.name, catalog)
        key = img.id if img else "~other"
        g = groups.setdefault(key, {"image": img, "templates": []})
        g["templates"].append(t)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for g in groups.values():
        g["templates"].sort(key=lambda t: t.created or epoch, reverse=True)
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


def newest_per_backend(groups: list[dict]) -> set[tuple[str, str]]:
    """(backend name, template name) of each image's newest template on each backend."""
    out = set()
    for g in groups:
        seen = set()
        for t in g["templates"]:
            if getattr(t, "backend", "") not in seen:
                seen.add(getattr(t, "backend", ""))
                out.add((getattr(t, "backend", ""), t.name))
    return out


@router.get("/images")
async def images_page(request: Request, tab: str = "templates"):
    tmpls, errs = await all_templates()
    catalog = images.catalog()
    defs = []
    for img in catalog:
        cfg = images.config(img) if img.usable else None
        prefix = img.manifest.template_prefix if img.usable else None
        defs.append({"img": img, "cfg": cfg, "problems": packer.preflight(img, cfg) if img.usable else [img.error],
                     "building": packer.building(img.id), "targets": _targets(img, cfg) if img.usable else [],
                     "last": (_last_builds(1, prefix) or [None])[0] if prefix else None})
    groups = group_templates(tmpls)
    return render(request, "images.html", tab=tab, groups=groups, defaults=newest_per_backend(groups), defs=defs,
                  builds=_last_builds(), error="; ".join(errs) or None, shared_problems=packer.preflight(),
                  backends={b.name: b for b in backends.all_backends()})


def _targets(img: images.ImageDef, cfg) -> list[dict]:
    """The backends this image can be built on, each with what stops a build there."""
    return [{"backend": b, "problems": packer.preflight(img, cfg, b), "building": packer.building(img.id, b.id)}
            for b in packer.build_backends(img)]


@router.post("/images/templates/{backend_id}/{name}/delete")
async def template_delete(request: Request, backend_id: int, name: str, user: dict = Depends(require_admin)):
    b = _backend_or_404(backend_id)
    tmpls, err = await on_backend(b, b.platform.templates)
    if err or tmpls is None:
        return feedback.redirect(request, "/images", error=err or f"{b.name} is not configured")
    group = next((g for g in group_templates(tmpls) if any(t.name == name for t in g["templates"])), None)
    if group is None:
        raise HTTPException(404, "Unknown template")
    if group["templates"][0].name == name:
        return feedback.redirect(request, "/images", error="The newest template of an image can't be deleted:"
                                                           " new VMs are made from it")
    _, err = await on_backend(b, b.platform.delete_template, name)
    if err:
        return feedback.redirect(request, "/images", error=err)
    _audit(request, user, "image.template_deleted", f"{name} on {b.name}")
    return feedback.redirect(request, "/images", notice=f"Deleted template {name} from {b.name}. VMs made from it"
                                                        " aren't affected.")


# --- one image definition ----------------------------------------------------------

def _render_image(request: Request, img: images.ImageDef, status_code: int = 200, cfg=None, **ctx):
    cfg = cfg or (images.config(img) if img.usable else None)
    prefix = img.manifest.template_prefix if img.usable else None
    return render(request, "image_detail.html", status_code=status_code, img=img, cfg=cfg,
                  problems=packer.preflight(img, cfg) if img.usable else [img.error],
                  targets=_targets(img, cfg) if img.usable and cfg else [],
                  iso_backends=[b for b in backends.all_backends() if b.configured and images.builds_on(img, b)]
                  if img.usable else [],
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
    current = images.config(img)
    isos = dict(current.isos)
    for b in backends.all_backends():
        value = form.get(f"iso_{b.id}")
        if isinstance(value, str):
            isos[str(b.id)] = value
    data = {
        "enabled": form.get("enabled") == "on",
        "iso_url": current.iso_url,
        "isos": isos,
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
    form = await request.form()
    targets = packer.build_backends(img) if img.usable else []
    b = next((t for t in targets if str(t.id) == str(form.get("backend_id", ""))), None) \
        if form.get("backend_id") else (targets[0] if len(targets) == 1 else None)
    if b is None:
        raise HTTPException(400, "Choose a backend to build on" if targets else
                            "No enabled backend can build this image")
    if packer.building(img.id, b.id):
        raise HTTPException(409, f"A build of this image is already running on {b.name}")
    problems = packer.preflight(img, None, b) + packer.preflight()
    if problems:
        raise HTTPException(400, "; ".join(problems))
    job_id = packer.start_build(img, b, user_id=user["id"])
    _audit(request, user, "image.build", f"{img.id} on {b.name}")
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/images/defs/{image_id}/isos")
async def image_isos(request: Request, image_id: str, backend: str = ""):
    """htmx: ISO picker for this image on one backend, preselecting one that matches the
    manifest's pattern."""
    img = _image_or_404(image_id)
    b = _backend_or_404(backend)
    cfg = images.config(img) if img.usable else None
    value = images.iso_for(cfg, b) if cfg else ""
    isos, error = None, None
    try:
        isos = await asyncio.wait_for(asyncio.to_thread(b.platform.list_isos, b), timeout=90)
    except Exception as e:  # noqa: BLE001 - shown next to a plain text field
        error = getattr(e, "msg", None) or str(e)
    if isos and not value and img.usable and img.manifest.iso.match:
        value = next((i for i in isos if fnmatch.fnmatch(b.platform.iso_name(i), img.manifest.iso.match)), "")
    return render(request, "_iso_picker.html", isos=isos, value=value, error_text=error, backend=b,
                  hint=img.manifest.iso.hint if img.usable else "")

