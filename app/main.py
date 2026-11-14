import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import backends, crypto, datalock, db, feedback, firstboot, guests, images, ipam, jobs, power, provision, users
from .auth import (SESSION_MAX_AGE, LoginRequired, client_ip, csrf_field, csrf_protect, current_user,
                   end_session, require_admin, require_user, start_session)
from .config import APP_DIR, PublicSettings, get_bootstrap, get_settings
from .backup import schedule
from .links import Links
from .platforms import PLATFORMS
from .platforms.base import usable_ipv4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vm-portal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings()
    datalock.acquire()
    try:
        db.fail_interrupted_jobs()
        users.ensure_bootstrap()
        images.migrate_legacy()
        schedule.start()
        firstboot.ensure_watching()
        yield
        await schedule.stop()
        await power.stop()
        await firstboot.stop()
        for p in PLATFORMS.values():
            await asyncio.to_thread(p.close)
    finally:
        datalock.release()


class SessionCookies(SessionMiddleware):
    """Starlette's session middleware, with the Secure flag following the live setting."""

    @property
    def security_flags(self) -> str:
        return self._flags + ("; secure" if get_settings().https_only_cookies else "")

    @security_flags.setter
    def security_flags(self, value: str) -> None:
        self._flags = value


boot = get_bootstrap()
app = FastAPI(title="VM Portal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(csrf_protect)])
app.add_middleware(
    SessionCookies,
    secret_key=boot.secret_key or crypto.session_key(),
    session_cookie="vmportal_session",
    same_site="strict",
    max_age=SESSION_MAX_AGE,
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
    if jobs.paused and request.url.path not in ("/healthz",) and not request.url.path.startswith("/static/"):
        # A restore is swapping the data in place: nothing may read or write it meanwhile.
        response = PlainTextResponse(jobs.paused, status_code=503, headers={"Retry-After": "30"})
    else:
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
    if get_settings().https_only_cookies:
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
    user = current_user(request)
    try:
        bs = backends.all_backends() if user else []
    except Exception:  # noqa: BLE001 - e.g. a page shown during a restore; the page itself still renders
        log.exception("could not read the backends")
        bs = []
    base = {"settings": PublicSettings(s), "current_user": user, "links": Links(user),
            "backends_ready": any(b.configured for b in bs), "editing_enabled": s.allow_image_editing,
            "can_create": any(b.usable_by(user) and b.platform.CAN_PROVISION for b in bs),
            "backend_count": len(bs)}
    flashed = feedback.take(request)
    base.update(flash_notice=flashed.get("notice"), flash_error=flashed.get("error"))
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)


async def on_backend(b, fn, *args, **kwargs):
    """Call a blocking platform function on a backend; return (result, error message).
    (None, None) when the backend isn't set up: pages say so instead."""
    if b is None or not b.configured:
        return None, None
    try:
        return await asyncio.to_thread(fn, b, *args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - shown to the user as a banner
        log.exception("%s call on backend %s failed", getattr(fn, "__name__", "platform"), b.name)
        return None, f"{b.name}: {e}"


async def all_templates() -> tuple[list, list[str]]:
    """The templates on every configured backend (each tagged with .backend, its backend's
    name), and the errors of the backends that couldn't be read."""
    bs = [b for b in backends.all_backends() if b.configured]
    results = await asyncio.gather(*(on_backend(b, b.platform.templates) for b in bs))
    out, errors = [], []
    for b, (ts, err) in zip(bs, results):
        if err:
            errors.append(err)
        for t in ts or []:
            t.backend = b.name
            out.append(t)
    return out, errors


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
    notice = "The backup was restored. Log in with an admin account from the backup." \
        if request.query_params.get("restored") == "1" else None
    return render(request, "login.html", notice=notice)


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip, uname = client_ip(request), username.strip().lower()[:64]
    wait = users.login_retry_after(uname, ip)
    if wait:
        db.audit("login.throttled", f"user {uname!r}", ip=ip)
        return render(request, "login.html", status_code=429, username=uname,
                      error=f"Too many failed attempts. Try again in {wait // 60 + 1} minutes.")
    user = await asyncio.to_thread(users.authenticate, uname, password)
    if user is None:
        users.login_failed(uname, ip)
        db.audit("login.failed", f"user {uname!r}", ip=ip)
        await login_failed_delay()
        return render(request, "login.html", status_code=401, username=uname, error="Wrong username or password")
    users.login_succeeded(uname, ip)
    start_session(request, user)
    db.audit("login", username=user["username"], ip=ip)
    return RedirectResponse("/", status_code=303)


async def login_failed_delay() -> None:
    await asyncio.sleep(1)


@app.post("/logout")
async def logout(request: Request):
    end_session(request)
    return RedirectResponse("/login", status_code=303)


# --- dashboard --------------------------------------------------------------

def owned_vms(user: dict) -> list[dict]:
    rows = db.active_vms()
    return rows if is_admin(user) else [v for v in rows if v["owner_id"] == user["id"]]


async def vm_rows(user: dict) -> tuple[list[dict], str | None]:
    """The user's VMs with what their backends report; each backend is asked once, all at
    the same time, and one that's down only affects its own VMs."""
    mine = owned_vms(user)
    by_backend: dict[int | None, object] = {}
    for vm in mine:
        b = backends.of_vm(vm)
        by_backend.setdefault(b.id if b else None, b)
    found = {bid: b for bid, b in by_backend.items() if b is not None}
    results = dict(zip(found, await asyncio.gather(*(on_backend(b, b.platform.vms_by_name)
                                                     for b in found.values()))))
    owners = {u["id"]: u["username"] for u in users.all_users()} if is_admin(user) else {}
    errors = sorted({err for _, err in results.values() if err})
    rows = []
    for vm in mine:
        b = backends.of_vm(vm)
        live, err = results.get(b.id, (None, None)) if b else (None, None)
        info = (live or {}).get(vm["name"])
        rows.append({
            **vm,
            "owner": owners.get(vm["owner_id"]),
            "backend": b.name if b else "(deleted backend)",
            "power_state": info.power_state if info else ("unknown" if err or live is None else "missing"),
            "live_ip": (info.ip if info else None) or usable_ipv4(vm["ip"]),
            "busy": jobs.is_locked(f"vm:{vm['name']}"),
            "power_pending": (pp.label if (pp := power.running(vm["id"])) else None),
        })
    return rows, "; ".join(errors) or None


@app.get("/")
async def dashboard(request: Request, user: dict = Depends(require_user)):
    vms, err = await vm_rows(user)
    tmpls, errs = await all_templates()
    err2 = "; ".join(e for e in errs if e not in (err or ""))
    from .routes_images import group_templates
    forgotten = request.query_params.get("forgotten", "")
    try:
        forgotten = provision.validate_name(forgotten) if forgotten and is_admin(user) else None
    except ValueError:
        forgotten = None
    return render(request, "dashboard.html", vms=vms, groups=group_templates(tmpls or []),
                  jobs=visible_jobs(user, 8), error=err or err2, forgotten=forgotten,
                  backup_health=schedule.health() if is_admin(user) else None)


@app.get("/partials/vms")
async def vms_partial(request: Request, user: dict = Depends(require_user)):
    vms, err = await vm_rows(user)
    return render(request, "_vm_table.html", vms=vms, error=err)


# --- VMs --------------------------------------------------------------------

DISK_MIN_GB = 10


def min_disk_gb(t, cfg) -> int:
    """The smallest disk a VM cloned from template t can have: the template's own disk (a
    clone is never smaller). When the platform didn't say, the size the image builds."""
    known = t.disk_gb or (-(-cfg.disk_mb // 1024) if cfg else 0)
    return max(DISK_MIN_GB, known)


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
            "min_disk": {t.name: min_disk_gb(t, cfg) for t in g["templates"]},
            "login_user": images.login_user(img, cfg),
            "docker": (img is None) or "docker" in img.manifest.features,
            "console_password": driver.supports_console_password,
        })
    return out


def _new_vm_form(tmpls: list, b) -> dict:
    s = get_settings()
    first = image_choices(tmpls)
    template = first[0]["templates"][0].name if first else ""
    disk = max(s.default_disk_gb, first[0]["min_disk"][template]) if first else s.default_disk_gb
    return {"cpus": s.default_cpus, "memory_mb": s.default_memory_mb, "disk_gb": disk,
            "template": template, "ip_mode": "dhcp", "autostart": True,
            "backend_id": b.id if b else None}


def creatable(user: dict) -> list:
    """Backends this user may create VMs on, in order."""
    return [b for b in backends.usable(user) if b.platform.CAN_PROVISION]


def chosen_backend(user: dict, backend_id) -> backends.Backend | None:
    """The backend asked for, if this user may create VMs on it; else the first one they may."""
    choices = creatable(user)
    return next((b for b in choices if str(b.id) == str(backend_id)), None) if backend_id not in (None, "") \
        else (choices[0] if choices else None)


@app.get("/vms/new")
async def vm_new(request: Request, user: dict = Depends(require_user), backend: str = ""):
    b = chosen_backend(user, backend)
    if b is None and backend:
        raise HTTPException(404, "No such backend")
    tmpls, err = await on_backend(b, b.platform.templates) if b else (None, None)
    return render(request, "vm_new.html", choices=image_choices(tmpls or []), form=_new_vm_form(tmpls or [], b),
                  error=err, backend=b, backend_choices=creatable(user), locations=await locations_of(b))


async def locations_of(b) -> list[str]:
    """Where on the backend a new VM can go (Proxmox nodes), when there's a choice."""
    if b is None or not b.platform.LOCATION_LABEL:
        return []
    found, _ = await on_backend(b, b.platform.locations)
    return found if found and len(found) > 1 else []


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
    autostart: str = Form(""),
    backend_id: str = Form(""),
    location: str = Form("", max_length=64),
):
    b = chosen_backend(user, backend_id)
    form = dict(name=name, template=template, cpus=cpus, memory_mb=memory_mb, disk_gb=disk_gb,
                ip_mode=ip_mode, ip=ip, ssh_keys=ssh_keys, compose_yaml=compose_yaml, autostart=autostart == "on",
                backend_id=b.id if b else None, location=location)
    tmpls, tmpls_err = await on_backend(b, b.platform.templates) if b else (None, None)
    places = await locations_of(b)

    async def fail(msg: str):
        return render(request, "vm_new.html", status_code=400, choices=image_choices(tmpls or []), form=form,
                      error=msg, backend=b, backend_choices=creatable(user), locations=places)

    if b is None:
        return await fail("There's no backend you can create VMs on" if not backend_id else
                          "You can't create VMs on that backend")
    try:
        name = provision.validate_name(name.strip())
        keys = provision.parse_ssh_keys(ssh_keys)
        compose = provision.validate_compose(compose_yaml)
        static_ip = ipam.validate_static_ip(b, ip) if ip_mode == "static" else None
        console_hash = await asyncio.to_thread(provision.hash_console_password, console_password,
                                               console_password_confirm)
    except ValueError as e:
        return await fail(str(e))

    # Fail closed: the template list is the only thing that keeps `template` to portal
    # templates (Terraform would clone any VM by that name), so no list means no VM.
    if tmpls is None:
        return await fail(tmpls_err or f"Could not list the templates on {b.name}. Try again.")
    if location and location not in places:
        return await fail(f"{b.name} can't put a VM on {b.platform.LOCATION_LABEL.lower() or 'location'} "
                          f"{location!r} right now")
    tmpl = next((t for t in tmpls if t.name == template), None)
    if tmpl is None:
        return await fail(f"Unknown template {template!r}")
    img = images.for_template(template)
    cfg = images.config(img) if img else None
    if img and cfg and not cfg.enabled:
        return await fail("That image is disabled")
    if disk_gb < (smallest := min_disk_gb(tmpl, cfg)):
        return await fail(f"The disk can't be smaller than {smallest} GB: that's the size of the template's"
                          " disk, and a clone can't shrink it")
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
    exists, err = await on_backend(b, b.platform.name_exists, name)
    if err:
        return await fail(err)
    if exists:
        return await fail(f"The name {name!r} is already taken on {b.name}")
    if static_ip:
        reason = await ipam.ipam.check(b, static_ip)
        if reason:
            return await fail(f"{static_ip} is in use ({reason})")

    req = provision.VMRequest(name=name, template=template, cpus=cpus, memory_mb=memory_mb,
                              disk_gb=disk_gb, ip=static_ip, ssh_keys=keys, compose_yaml=compose,
                              console_password_hash=console_hash, login_user=images.login_user(img, cfg),
                              guest=driver.name, features=features, image_id=img.id if img else None,
                              network_interface=img.manifest.network_interface if img and img.usable else "en*",
                              autostart=autostart == "on", backend_id=b.id, location=location or None)
    try:
        job_id = provision.create(req, owner_id=user["id"])
    except ValueError as e:
        return await fail(str(e))
    db.audit("vm.create", f"{name} on {b.name}" + (f" ({location})" if location else ""),
             username=user["username"], ip=client_ip(request))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _vm_or_404(name: str, user: dict) -> dict:
    vm = db.active_vm(name)
    # 404 rather than 403, so users can't probe for other users' VM names.
    if not vm or not (is_admin(user) or vm["owner_id"] == user["id"]):
        raise HTTPException(404, f"No VM named {name!r}")
    return vm


def _backend_of(vm: dict) -> backends.Backend:
    b = backends.of_vm(vm)
    if b is None:
        raise HTTPException(409, f"{vm['name']}'s backend no longer exists in this portal")
    return b


async def _live(vm: dict, fresh: bool = False):
    """(backend, the VM as its backend reports it, error)."""
    b = backends.of_vm(vm)
    live, err = await on_backend(b, b.platform.vms_by_name, fresh) if b else (None, "Its backend no longer exists")
    return b, (live or {}).get(vm["name"]), err


def _power_view(vm: dict, info) -> dict:
    """What the VM page shows of a power action in progress (or just over), and the
    signature the page's status poll compares to see when to reload."""
    p = power.get(vm["id"])
    sig = "|".join(str(x) for x in (info.power_state if info else "none", bool(info and info.tools_running),
                                    info.ip if info else "", p.action if p and p.running else "",
                                    vm.get("firstboot_state") or ""))
    return {"pending": p, "status_sig": sig}


async def vm_page(request: Request, vm: dict, user: dict, status_code: int = 200, error: str | None = None,
                  resize_form: dict | None = None):
    """The VM's page; also after a refused action, with why (and what was typed)."""
    name = vm["name"]
    b, info, err = await _live(vm)
    # Only jobs for this VM instance (a destroyed VM may have had the same name).
    vm_jobs = db.query("SELECT * FROM jobs WHERE target=? AND kind LIKE 'vm-%' AND created_at >= ?"
                       " ORDER BY id DESC LIMIT 20", (name, vm["created_at"]))
    owner = users.get(vm["owner_id"]) if vm["owner_id"] else None
    autostart, autostart_err = (await on_backend(b, b.platform.autostart, name, provision.vm_moid(name))
                                if info and vm["status"] == "ready" else (None, None))
    return render(request, "vm_detail.html", status_code=status_code, vm=vm, info=info, jobs=vm_jobs,
                  error=error or err, backend=b, resize_form=resize_form, **_power_view(vm, info),
                  firstboot=firstboot.detail(vm),
                  autostart=autostart, autostart_error=autostart_err,
                  owner=owner["username"] if owner else None, busy=jobs.is_locked(f"vm:{name}"),
                  all_users=[{"id": u["id"], "username": u["username"]} for u in users.all_users()]
                  if is_admin(user) else [],
                  can_resize=(get_settings().vms_dir / name / "terraform.tfstate").exists(),
                  state_history=[p.name for p in reversed(provision.state_history_files(provision.vm_dir(name)))]
                  if is_admin(user) else [])


@app.get("/vms/{name}")
async def vm_detail(request: Request, name: str, user: dict = Depends(require_user)):
    return await vm_page(request, _vm_or_404(name, user), user)


@app.get("/vms/{name}/status")
async def vm_status(request: Request, name: str, user: dict = Depends(require_user), sig: str = ""):
    """Polled by the VM page: the power action's progress; a reload (HX-Refresh) once
    anything the page shows has changed."""
    vm = _vm_or_404(name, user)
    _, info, _ = await _live(vm)
    view = _power_view(vm, info)
    p = view["pending"]
    if sig and sig != view["status_sig"]:
        if p and not p.running and not p.announced:
            p.announced = True
            feedback.flash(request, **{"notice" if p.outcome == "done" else "error": p.message})
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return render(request, "_vm_status.html", vm=vm, info=info, **view)


POWER_ACTIONS = {"on": "Power on", "off": "Power off", "shutdown": "Shut down", "reboot": "Reboot"}


@app.post("/vms/{name}/power/{action}")
async def vm_power(request: Request, name: str, action: str, user: dict = Depends(require_user)):
    if action not in POWER_ACTIONS:
        raise HTTPException(400, "Unknown action")
    vm = _vm_or_404(name, user)

    async def refuse(status: int, msg: str):
        return await vm_page(request, vm, user, status_code=status, error=msg)

    b = backends.of_vm(vm)
    if b is None:
        return await refuse(409, f"{name}'s backend no longer exists in this portal")
    if jobs.is_locked(f"vm:{name}"):
        return await refuse(409, "A job is running for this VM. Try again when it's done")
    busy = power.running(vm["id"])
    if busy and action != "off":           # a hard power off is always allowed: it's the way out
        return await refuse(409, f"{busy.label} is already in progress ({busy.elapsed}s so far)")
    live, err = await on_backend(b, b.platform.vms_by_name, True)
    info = (live or {}).get(name)
    if err:
        return await refuse(502, err)
    if action in ("shutdown", "reboot") and info and b.platform.GUEST_POWER_NEEDS_TOOLS and not info.tools_running:
        return await refuse(409, f"The guest tools in {name} aren't answering, so it can't be asked to"
                                 f" {POWER_ACTIONS[action].lower()}. Use Power off (hard) instead")
    _, err = await on_backend(b, b.platform.power, name, action, provision.vm_moid(name))
    if err:
        return await refuse(502, err)
    db.audit(f"vm.power.{action}", name, username=user["username"], ip=client_ip(request))
    if power.start(vm, action, user["username"], info) is None:
        return feedback.redirect(request, f"/vms/{name}", notice=power.DONE.get(action))
    return RedirectResponse(f"/vms/{name}", status_code=303)


@app.post("/vms/{name}/resize")
async def vm_resize(
    request: Request,
    name: str,
    user: dict = Depends(require_user),
    cpus: int = Form(..., ge=1, le=32),
    memory_mb: int = Form(..., ge=512, le=262144),
    disk_gb: int = Form(..., ge=10, le=4096),
    restart: str = Form(""),
):
    vm = _vm_or_404(name, user)

    async def refuse(status: int, msg: str):
        return await vm_page(request, vm, user, status_code=status, error=msg,
                             resize_form={"cpus": cpus, "memory_mb": memory_mb, "disk_gb": disk_gb,
                                          "restart": restart == "on"})

    if vm["status"] != "ready":
        return await refuse(409, "Only a ready VM can be resized")
    if jobs.is_locked(f"vm:{name}"):
        return await refuse(409, "A job is already running for this VM")
    if busy := power.running(vm["id"]):
        return await refuse(409, f"{busy.label} is in progress. Resize when it's done")
    if memory_mb % 4:
        return await refuse(400, "Memory must be a multiple of 4 MB")
    if (cpus, memory_mb, disk_gb) == (vm["cpus"], vm["memory_mb"], vm["disk_gb"]):
        return feedback.redirect(request, f"/vms/{name}", notice="Nothing to change: those are the VM's sizes now.")
    needs_off = (cpus, memory_mb) != (vm["cpus"], vm["memory_mb"])
    if needs_off and restart != "on":
        b = _backend_of(vm)
        live, err = await on_backend(b, b.platform.vms_by_name, True)
        info = (live or {}).get(name)
        if err or not info or info.power_state != "poweredOff":
            return await refuse(400, "Changing vCPUs or memory restarts the VM: tick 'Restart the VM'")
    try:
        job_id = provision.resize(vm, cpus, memory_mb, disk_gb, restart=restart == "on", user_id=user["id"])
    except ValueError as e:
        return await refuse(400, str(e))
    db.audit("vm.resize", f"{name}: {vm['cpus']}->{cpus} vCPU, {vm['memory_mb']}->{memory_mb} MB,"
             f" {vm['disk_gb']}->{disk_gb} GB", username=user["username"], ip=client_ip(request))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/vms/{name}/details")
async def vm_details(request: Request, name: str, user: dict = Depends(require_user),
                     notes: str = Form("", max_length=2000), owner_id: str = Form("")):
    """Portal-only details: notes (owner or admin), owner (admin). Nothing changes on the backend."""
    vm = _vm_or_404(name, user)
    notes = notes.replace("\r\n", "\n").strip()
    if any(ord(c) < 32 and c not in "\n\t" for c in notes):
        return await vm_page(request, vm, user, 400, "Notes can't contain control characters")
    new_owner = vm["owner_id"]
    if owner_id and is_admin(user):
        target = users.get(int(owner_id)) if owner_id.isdigit() else None
        if target is None:
            return await vm_page(request, vm, user, 400, "Unknown user")
        new_owner = target["id"]
    db.execute("UPDATE vms SET notes=?, owner_id=? WHERE id=?", (notes, new_owner, vm["id"]))
    if new_owner != vm["owner_id"]:
        old = users.get(vm["owner_id"]) if vm["owner_id"] else None
        db.audit("vm.owner_changed", f"{name}: {old['username'] if old else '-'} -> {target['username']}",
                 username=user["username"], ip=client_ip(request))
    if notes != (vm["notes"] or ""):
        db.audit("vm.notes_changed", name, username=user["username"], ip=client_ip(request))
    changed = [w for w, c in (("notes", notes != (vm["notes"] or "")), ("owner", new_owner != vm["owner_id"])) if c]
    msg = f"Saved the {' and '.join(changed)}." if changed else "Nothing changed."
    # The previous owner may no longer see the VM.
    return feedback.redirect(request, f"/vms/{name}" if new_owner == user["id"] or is_admin(user) else "/",
                             notice=msg)


@app.post("/vms/{name}/autostart")
async def vm_autostart(request: Request, name: str, user: dict = Depends(require_user), enabled: str = Form("")):
    """Start the VM with its host, or not: on vSphere its entry in the host's startup list, on
    Proxmox the VM's own start-at-boot option."""
    vm = _vm_or_404(name, user)
    if vm["status"] != "ready":
        return await vm_page(request, vm, user, 409, "Only a ready VM's autostart can be changed")
    if jobs.is_locked(f"vm:{name}"):
        return await vm_page(request, vm, user, 409, "A job is already running for this VM")
    on = enabled == "on"
    b = _backend_of(vm)
    done, err = await on_backend(b, b.platform.set_autostart, name, provision.vm_moid(name), on)
    if done is None:
        return await vm_page(request, vm, user, 502, err or f"{b.name} is not configured")
    db.execute("UPDATE vms SET autostart=? WHERE id=?", (int(on), vm["id"]))
    db.audit(f"vm.autostart.{'on' if on else 'off'}", name, username=user["username"], ip=client_ip(request))
    return feedback.redirect(request, f"/vms/{name}", notice=f"Autostart is {'on' if on else 'off'}: {name} "
                             f"{'starts' if on else 'no longer starts'} with host {done.host}.")


@app.post("/vms/{name}/seed-cleanup")
async def vm_seed_cleanup(request: Request, name: str, user: dict = Depends(require_user)):
    """Proxmox: replace the VM's first-boot seed ISO (keys, console password hash, compose
    file) by a copy without them, once the VM is up. The create job does this itself; this
    is for when the VM's guest agent didn't answer in time."""
    vm = _vm_or_404(name, user)
    b = _backend_of(vm)
    clean = getattr(b.platform, "clean_seed", None)
    if clean is None or not vm["seed_pending"]:
        return await vm_page(request, vm, user, 400, "Nothing to clean up")
    if jobs.is_locked(f"vm:{name}"):
        return await vm_page(request, vm, user, 409, "A job is already running for this VM")
    done, err = await on_backend(b, clean, name, provision.vm_moid(name), 20)
    if err:
        return await vm_page(request, vm, user, 502, err)
    if not done:
        return await vm_page(request, vm, user, 409, "The VM's guest agent doesn't answer yet. Start the VM, "
                                                     "wait for it to boot, then try again")
    db.audit("vm.seed_cleaned", name, username=user["username"], ip=client_ip(request))
    return feedback.redirect(request, f"/vms/{name}", notice="The seed ISO was replaced by a copy without the "
                                                             "keys, password hash and compose file.")


@app.post("/vms/{name}/firstboot-check")
async def vm_firstboot_check(request: Request, name: str, user: dict = Depends(require_user)):
    """Ask again whether the VM's first-boot setup worked (after the portal gave up waiting,
    or once a missing permission is given)."""
    vm = _vm_or_404(name, user)
    b = _backend_of(vm)
    if vm["status"] != "ready" or not vm["firstboot_state"] or not firstboot.tracked(b.platform):
        return await vm_page(request, vm, user, 400, "Nothing to check")
    if not b.configured:
        return await vm_page(request, vm, user, 409, f"{b.name} is not configured")
    try:
        # From now, so a VM the portal already gave up on isn't given up on again at once.
        state = await asyncio.to_thread(firstboot.check, vm | {"firstboot_since": db.now()})
    except Exception as e:  # noqa: BLE001 - shown as a banner
        log.exception("first-boot check of %s failed", name)
        return await vm_page(request, vm, user, 502, f"{b.name}: {e}")
    if state == "waiting":
        firstboot.restart_watch(vm["id"])
        return feedback.redirect(request, f"/vms/{name}", notice="No result from cloud-init yet. The portal "
                                                                 "keeps checking and updates this page.")
    return feedback.redirect(request, f"/vms/{name}")


@app.post("/vms/{name}/destroy")
async def vm_destroy(request: Request, name: str, confirm: str = Form(""), user: dict = Depends(require_user)):
    vm = _vm_or_404(name, user)
    if confirm != name:
        return await vm_page(request, vm, user, 400, f"Type the VM name ({name}) to confirm destroying it")
    if jobs.is_locked(f"vm:{name}"):
        return await vm_page(request, vm, user, 409, "A job is already running for this VM")
    if busy := power.running(vm["id"]):
        return await vm_page(request, vm, user, 409, f"{busy.label} is in progress. Destroy it when that's done,"
                                                     " or power it off (hard) first")
    _backend_of(vm)
    job_id = provision.destroy(vm, user_id=user["id"])
    db.audit("vm.destroy", name, username=user["username"], ip=client_ip(request))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# --- IPs --------------------------------------------------------------------

@app.get("/ip/suggest")
async def ip_suggest(request: Request, user: dict = Depends(require_user), fresh: bool = False,
                     backend: str = ""):
    b = chosen_backend(user, backend)
    if b is None:
        return render(request, "_ip_suggest.html", error="Choose a backend first")
    try:
        result = await ipam.ipam.scan(b, fresh=fresh)
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
    job = _job_or_404(job_id, user)
    return render(request, "job.html", job=job, cancelling=jobs.cancelling(job_id),
                  can_cancel=is_admin(user) and job["kind"] in CANCELLABLE and job["status"] in ("queued", "running"))


# Only template builds: Packer cleans up after itself when stopped. Stopping tofu halfway
# through a VM would leave one that's half made.
CANCELLABLE = ("packer-build",)


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: int, user: dict = Depends(require_admin)):
    job = _job_or_404(job_id, user)
    if job["kind"] not in CANCELLABLE:
        raise HTTPException(400, "This kind of job can't be cancelled")
    if jobs.cancel(job_id, by=user["username"]):
        db.audit("job.cancelled", f"#{job_id} {job['kind']} {job['target']}", username=user["username"],
                 ip=client_ip(request))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


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


from . import admin, console, editor, routes_backends, routes_backup, routes_images  # noqa: E402 - feature routers

# routes_backends first: admin's /settings/{section} would otherwise take /settings/backends.
for _r in (routes_backends.router, admin.router, console.router, routes_images.router, editor.router,
           routes_backup.router):
    app.include_router(_r)
