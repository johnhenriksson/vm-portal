import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import crypto, db, guests, images, ipam, jobs, provision, users, vsphere
from .auth import (LoginRequired, client_ip, csrf_field, csrf_protect, current_user, require_admin,
                   require_user, start_session)
from .config import APP_DIR, PublicSettings, get_bootstrap, get_settings, vcenter_configured

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vm-portal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings()
    db.fail_interrupted_jobs()
    users.ensure_bootstrap()
    images.migrate_legacy()
    yield
    await asyncio.to_thread(vsphere.close)


boot = get_bootstrap()
app = FastAPI(title="VM Portal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(csrf_protect)])
app.add_middleware(
    SessionMiddleware,
    secret_key=boot.secret_key or crypto.session_key(),
    session_cookie="vmportal_session",
    same_site="strict",
    https_only=boot.https_only_cookies,
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.globals["csrf_field"] = csrf_field
auth = [Depends(require_user)]
admin_only = [Depends(require_admin)]

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' {ws}; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    host = request.headers.get("host", "")
    # 'self' doesn't cover ws:// / wss:// in every browser; allow this host's sockets explicitly.
    ws = f"wss://{host} ws://{host}" if host and all(c.isalnum() or c in ".-:[]" for c in host) else ""
    h.setdefault("Content-Security-Policy", CSP.format(ws=ws))
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not request.url.path.startswith("/static/"):
        h.setdefault("Cache-Control", "no-store")
    if boot.https_only_cookies:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    target = "/setup" if db.user_count() == 0 else "/login"
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if request.headers.get("HX-Request") or request.url.path.startswith("/static/"):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=exc.detail)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    s = get_settings()
    base = {"settings": PublicSettings(s), "current_user": current_user(request),
            "vcenter_ready": vcenter_configured(s), "editing_enabled": get_bootstrap().allow_image_editing}
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)


async def vcenter(fn, *args, **kwargs):
    """Call a blocking vSphere function; return (result, error message)."""
    if not vcenter_configured():
        return None, None  # pages show a "configure vCenter" banner instead
    try:
        return await asyncio.to_thread(fn, *args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - shown to the user as a banner
        log.exception("vCenter call failed")
        return None, f"vCenter error: {e}"


def is_admin(user: dict) -> bool:
    return user["role"] == "admin"


def visible_jobs(user: dict, limit: int, where: str = "1=1", args: tuple = ()) -> list[dict]:
    if not is_admin(user):
        where, args = f"({where}) AND user_id=?", (*args, user["id"])
    return db.query(f"SELECT * FROM jobs WHERE {where} ORDER BY id DESC LIMIT ?", (*args, limit))


# --- auth -------------------------------------------------------------------

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.get("/login")
async def login_form(request: Request):
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip, uname = client_ip(request), username.strip().lower()[:64]
    wait = max(users.ip_throttle.retry_after(ip), users.user_throttle.retry_after(uname))
    if wait:
        db.audit("login.throttled", f"user {uname!r}", ip=ip)
        return render(request, "login.html", status_code=429, username=uname,
                      error=f"Too many failed attempts. Try again in {wait // 60 + 1} minutes.")
    user = await asyncio.to_thread(users.authenticate, uname, password)
    if user is None:
        users.ip_throttle.fail(ip)
        users.user_throttle.fail(uname)
        db.audit("login.failed", f"user {uname!r}", ip=ip)
        await login_failed_delay()
        return render(request, "login.html", status_code=401, username=uname, error="Wrong username or password")
    users.ip_throttle.reset(ip)
    users.user_throttle.reset(uname)
    start_session(request, user)
    db.audit("login", username=user["username"], ip=ip)
    return RedirectResponse("/", status_code=303)


async def login_failed_delay() -> None:
    await asyncio.sleep(1)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- dashboard --------------------------------------------------------------

def owned_vms(user: dict) -> list[dict]:
    rows = db.active_vms()
    return rows if is_admin(user) else [v for v in rows if v["owner_id"] == user["id"]]


async def vm_rows(user: dict) -> tuple[list[dict], str | None]:
    mine = owned_vms(user)
    live, err = await vcenter(vsphere.vms_by_name) if mine else ({}, None)
    owners = {u["id"]: u["username"] for u in users.all_users()} if is_admin(user) else {}
    rows = []
    for vm in mine:
        info = (live or {}).get(vm["name"])
        rows.append({
            **vm,
            "owner": owners.get(vm["owner_id"]),
            "power_state": info.power_state if info else ("unknown" if err or live is None else "missing"),
            "live_ip": (info.ip if info else None) or vm["ip"],
            "busy": jobs.is_locked(f"vm:{vm['name']}"),
        })
    return rows, err


@app.get("/")
async def dashboard(request: Request, user: dict = Depends(require_user)):
    vms, err = await vm_rows(user)
    tmpls, err2 = await vcenter(vsphere.templates)
    from .routes_images import group_templates
    return render(request, "dashboard.html", vms=vms, groups=group_templates(tmpls or []),
                  jobs=visible_jobs(user, 8), error=err or err2)


@app.get("/partials/vms")
async def vms_partial(request: Request, user: dict = Depends(require_user)):
    vms, err = await vm_rows(user)
    return render(request, "_vm_table.html", vms=vms, error=err)


# --- VMs --------------------------------------------------------------------

def image_choices(tmpls: list) -> list[dict]:
    """Templates grouped by image for the create-VM page, with what each image supports."""
    from .routes_images import group_templates
    out = []
    for g in group_templates(tmpls):
        img = g["image"]
        cfg = images.config(img) if img else None
        if img and cfg and not cfg.enabled:
            continue
        driver = guests.get(img.manifest.guest if img else None)
        out.append({
            "key": img.id if img else "other",
            "name": img.manifest.name if img else "Other templates",
            "distro": img.manifest.os.distro if img else "",
            "templates": g["templates"],
            "login_user": images.login_user(img, cfg),
            "docker": (img is None) or "docker" in img.manifest.features,
            "console_password": driver.supports_console_password,
        })
    return out


def _new_vm_form(tmpls: list) -> dict:
    s = get_settings()
    first = image_choices(tmpls)
    return {"cpus": s.default_cpus, "memory_mb": s.default_memory_mb, "disk_gb": s.default_disk_gb,
            "template": first[0]["templates"][0].name if first else "", "ip_mode": "dhcp"}


@app.get("/vms/new", dependencies=auth)
async def vm_new(request: Request):
    tmpls, err = await vcenter(vsphere.templates)
    return render(request, "vm_new.html", choices=image_choices(tmpls or []), form=_new_vm_form(tmpls or []),
                  error=err)


@app.post("/vms")
async def vm_create(
    request: Request,
    user: dict = Depends(require_user),
    name: str = Form(...),
    template: str = Form(...),
    cpus: int = Form(..., ge=1, le=32),
    memory_mb: int = Form(..., ge=512, le=262144),
    disk_gb: int = Form(..., ge=10, le=4096),
    ip_mode: str = Form("dhcp"),
    ip: str = Form(""),
    ssh_keys: str = Form("", max_length=20000),
    console_password: str = Form("", max_length=256),
    console_password_confirm: str = Form("", max_length=256),
    compose_yaml: str = Form("", max_length=100000),
):
    form = dict(name=name, template=template, cpus=cpus, memory_mb=memory_mb, disk_gb=disk_gb,
                ip_mode=ip_mode, ip=ip, ssh_keys=ssh_keys, compose_yaml=compose_yaml)
    tmpls, _ = await vcenter(vsphere.templates)

    async def fail(msg: str):
        return render(request, "vm_new.html", status_code=400, choices=image_choices(tmpls or []), form=form,
                      error=msg)

    if not vcenter_configured():
        return await fail("vCenter is not configured yet")
    try:
        name = provision.validate_name(name.strip())
        keys = provision.parse_ssh_keys(ssh_keys)
        compose = provision.validate_compose(compose_yaml)
        static_ip = ipam.validate_static_ip(ip) if ip_mode == "static" else None
        console_hash = await asyncio.to_thread(provision.hash_console_password, console_password,
                                               console_password_confirm)
    except ValueError as e:
        return await fail(str(e))

    if tmpls is not None and template not in {t.name for t in tmpls}:
        return await fail(f"Unknown template {template!r}")
    img = images.for_template(template)
    cfg = images.config(img) if img else None
    if img and cfg and not cfg.enabled:
        return await fail("That image is disabled")
    driver = guests.get(img.manifest.guest if img else None)
    features = tuple(img.manifest.features) if img else ("docker",)
    if compose and "docker" not in features:
        return await fail("This image has no Docker, so it can't run a compose.yaml")
    if console_hash and not driver.supports_console_password:
        return await fail("This image doesn't support a console password")
    if static_ip and not driver.supports_static_ip:
        return await fail("This image doesn't support static IPs")
    if not keys and not get_settings().default_ssh_keys:
        return await fail("Add at least one SSH key")
    if db.active_vm(name) or jobs.is_locked(f"vm:{name}"):
        return await fail(f"The name {name!r} is already taken")
    exists, err = await vcenter(vsphere.name_exists, name)
    if err:
        return await fail(err)
    if exists:
        return await fail(f"The name {name!r} is already taken in vCenter")
    if static_ip:
        reason = await ipam.ipam.check(static_ip)
        if reason:
            return await fail(f"{static_ip} is in use ({reason})")

    req = provision.VMRequest(name=name, template=template, cpus=cpus, memory_mb=memory_mb,
                              disk_gb=disk_gb, ip=static_ip, ssh_keys=keys, compose_yaml=compose,
                              console_password_hash=console_hash, login_user=images.login_user(img, cfg),
                              guest=driver.name, features=features, image_id=img.id if img else None)
    try:
        job_id = provision.create(req, owner_id=user["id"])
    except ValueError as e:
        return await fail(str(e))
    db.audit("vm.create", name, username=user["username"], ip=client_ip(request))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _vm_or_404(name: str, user: dict) -> dict:
    vm = db.active_vm(name)
    # 404 rather than 403, so users can't probe for other users' VM names.
    if not vm or not (is_admin(user) or vm["owner_id"] == user["id"]):
        raise HTTPException(404, f"No VM named {name!r}")
    return vm


@app.get("/vms/{name}")
async def vm_detail(request: Request, name: str, user: dict = Depends(require_user)):
    vm = _vm_or_404(name, user)
    live, err = await vcenter(vsphere.vms_by_name)
    info = (live or {}).get(name)
    # Only jobs for this VM instance (a destroyed VM may have had the same name).
    vm_jobs = db.query("SELECT * FROM jobs WHERE target=? AND kind LIKE 'vm-%' AND created_at >= ?"
                       " ORDER BY id DESC LIMIT 20", (name, vm["created_at"]))
    owner = users.get(vm["owner_id"]) if vm["owner_id"] else None
    return render(request, "vm_detail.html", vm=vm, info=info, jobs=vm_jobs, error=err,
                  owner=owner["username"] if owner else None, busy=jobs.is_locked(f"vm:{name}"))


@app.post("/vms/{name}/power/{action}")
async def vm_power(request: Request, name: str, action: str, user: dict = Depends(require_user)):
    if action not in ("on", "off", "shutdown", "reboot"):
        raise HTTPException(400, "Unknown action")
    _vm_or_404(name, user)
    _, err = await vcenter(vsphere.power, name, action)
    if err:
        raise HTTPException(502, err)
    db.audit(f"vm.power.{action}", name, username=user["username"], ip=client_ip(request))
    return RedirectResponse(f"/vms/{name}", status_code=303)


@app.post("/vms/{name}/destroy")
async def vm_destroy(request: Request, name: str, confirm: str = Form(""), user: dict = Depends(require_user)):
    vm = _vm_or_404(name, user)
    if confirm != name:
        raise HTTPException(400, "Type the VM name to confirm")
    if jobs.is_locked(f"vm:{name}"):
        raise HTTPException(409, "A job is already running for this VM")
    job_id = provision.destroy(vm, user_id=user["id"])
    db.audit("vm.destroy", name, username=user["username"], ip=client_ip(request))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# --- IPs --------------------------------------------------------------------

@app.get("/ip/suggest", dependencies=auth)
async def ip_suggest(request: Request, fresh: bool = False):
    try:
        result = await ipam.ipam.scan(fresh=fresh)
    except ipam.PoolNotConfigured as e:
        return render(request, "_ip_suggest.html", error=str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("IP scan failed")
        return render(request, "_ip_suggest.html", error=f"IP scan failed: {e}")
    return render(request, "_ip_suggest.html", result=result)


# --- jobs -------------------------------------------------------------------

def _job_or_404(job_id: int, user: dict) -> dict:
    job = db.get_job(job_id)
    if job and (is_admin(user) or job["user_id"] == user["id"]):
        return job
    if job and job["kind"].startswith("vm-"):
        # Also jobs others (admins) ran on a VM this user owns.
        vm = db.one("SELECT owner_id FROM vms WHERE name=? AND created_at <= ? ORDER BY id DESC LIMIT 1",
                    (job["target"], job["created_at"]))
        if vm and vm["owner_id"] == user["id"]:
            return job
    raise HTTPException(404, "No such job")


@app.get("/jobs")
async def job_list(request: Request, user: dict = Depends(require_user)):
    return render(request, "jobs.html", jobs=visible_jobs(user, 100))


@app.get("/jobs/{job_id}")
async def job_detail(request: Request, job_id: int, user: dict = Depends(require_user)):
    return render(request, "job.html", job=_job_or_404(job_id, user))


@app.get("/jobs/{job_id}/stream")
async def job_stream(job_id: int, user: dict = Depends(require_user)):
    _job_or_404(job_id, user)

    async def events():
        async for line in jobs.stream(job_id):
            yield f"data: {line.rstrip(chr(13))}\n\n"
        job = db.get_job(job_id)
        yield f"event: done\ndata: {job['status'] if job else 'unknown'}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


from . import admin, console, editor, routes_images  # noqa: E402 - feature routers

for _r in (admin.router, console.router, routes_images.router, editor.router):
    app.include_router(_r)
