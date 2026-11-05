"""First-run setup, the user's own account, and the admin Settings pages."""
import asyncio
import hashlib
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from . import config, db, packer, users, vsphere
from .auth import client_ip, current_user, require_admin, require_user, start_session
from .config import SECRET_FIELDS, SECTIONS, Settings, env_overrides, get_settings

log = logging.getLogger(__name__)
router = APIRouter()


def render(*args, **kwargs):
    from .main import render as _render
    return _render(*args, **kwargs)


def audit(request: Request, user: dict | None, action: str, detail: str = "") -> None:
    db.audit(action, detail, username=user["username"] if user else None, ip=client_ip(request))


# --- first run ----------------------------------------------------------------

@router.get("/setup")
async def setup_form(request: Request):
    if db.user_count():
        u = current_user(request)
        return RedirectResponse("/setup/vcenter" if u and u["role"] == "admin" else "/login", status_code=303)
    return render(request, "setup.html", form={})


@router.post("/setup")
async def setup(request: Request, token: str = Form(""), username: str = Form(""),
                password: str = Form(""), confirm: str = Form("")):
    if db.user_count():
        return RedirectResponse("/login", status_code=303)
    ip = client_ip(request)
    form = {"username": username}

    def fail(msg: str, code: int = 400):
        return render(request, "setup.html", status_code=code, form=form, error=msg)

    if users.ip_throttle.retry_after(ip):
        return fail("Too many attempts. Try again later.", 429)
    if not users.check_setup_token(token):
        users.ip_throttle.fail(ip)
        db.audit("setup.bad_token", ip=ip)
        return fail("Wrong setup token. It is printed in the container log.", 403)
    try:
        username = users.validate_username(username)
        users.validate_password(password, username)
        if password != confirm:
            raise ValueError("The passwords don't match")
    except ValueError as e:
        return fail(str(e))
    uid = db.create_user(username, await asyncio.to_thread(users.hash_password, password), "admin",
                         only_if_first=True)
    if uid is None:  # someone else finished setup first
        return RedirectResponse("/login", status_code=303)
    users.setup_token = None
    user = users.get(uid)
    start_session(request, user)
    audit(request, user, "setup.admin_created", username)
    return RedirectResponse("/setup/vcenter", status_code=303)


@router.get("/setup/vcenter", dependencies=[Depends(require_admin)])
async def setup_vcenter_form(request: Request):
    return render_setup(request, "vcenter")


@router.post("/setup/vcenter")
async def setup_vcenter(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    try:
        apply_changes(request, user, _form_changes("vcenter", form, env_overrides()))
        missing = [FIELDS[k]["label"] for k in config.VCENTER_REQUIRED if not getattr(get_settings(), k)]
        if missing:
            raise ValueError("Still missing: " + ", ".join(missing))
    except ValueError as e:
        return render_setup(request, "vcenter", status_code=400, error=str(e))
    return RedirectResponse("/setup/network", status_code=303)


@router.get("/setup/network", dependencies=[Depends(require_admin)])
async def setup_network_form(request: Request):
    return render_setup(request, "network")


@router.post("/setup/network")
async def setup_network(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    locked = env_overrides()
    try:
        changes = _form_changes("network", form, locked)
        new_keys = [k.strip() for k in str(form.get("ssh_keys", "")).splitlines() if k.strip()]
        if new_keys and "default_ssh_keys" not in locked:
            changes["default_ssh_keys"] = get_settings().default_ssh_keys + new_keys
        apply_changes(request, user, changes)
    except ValueError as e:
        return render_setup(request, "network", status_code=400, error=str(e), ssh_keys=form.get("ssh_keys", ""))
    audit(request, user, "setup.completed")
    return RedirectResponse("/", status_code=303)


def render_setup(request: Request, step: str, status_code: int = 200, **ctx) -> HTMLResponse:
    s, locked = get_settings(), env_overrides()
    if step == "vcenter":
        fields = [f for f in field_views("vcenter", s, locked) if f["key"] in CONNECTION]
    else:
        fields = field_views("network", s, locked)
    return render(request, f"setup_{step}.html", status_code=status_code, fields=fields, locked=locked,
                  has_creds=bool(s.vsphere_server and s.vsphere_password), **ctx)


# --- own account ----------------------------------------------------------------

@router.get("/account")
async def account(request: Request, user: dict = Depends(require_user)):
    return render(request, "account.html")


@router.post("/account/password")
async def change_password(request: Request, user: dict = Depends(require_user), current: str = Form(""),
                          password: str = Form(""), confirm: str = Form("")):
    def fail(msg: str):
        return render(request, "account.html", status_code=400, error=msg)

    if not await asyncio.to_thread(users.verify_password, current, user["password_hash"]):
        audit(request, user, "account.password_failed")
        return fail("Your current password is wrong")
    if password != confirm:
        return fail("The new passwords don't match")
    try:
        await asyncio.to_thread(users.set_password, user["id"], password)
    except ValueError as e:
        return fail(str(e))
    start_session(request, users.get(user["id"]))  # keep this session, end all others
    audit(request, user, "account.password_changed")
    return render(request, "account.html", notice="Password changed. Your other sessions were logged out.")


# --- settings -------------------------------------------------------------------

FIELDS: dict[str, dict] = {
    "vsphere_server": {"label": "Server", "hint": "Hostname or IP of vCenter, optionally with :port"},
    "vsphere_user": {"label": "User", "hint": "Use a dedicated service account with only the permissions the portal needs"},
    "vsphere_password": {"label": "Password", "type": "password"},
    "vsphere_insecure": {"label": "Skip TLS certificate verification", "type": "bool",
                         "hint": "Only for self-signed certificates on a trusted network"},
    "vsphere_datacenter": {"label": "Datacenter"},
    "vsphere_cluster": {"label": "Cluster"},
    "vsphere_datastore": {"label": "Datastore"},
    "vsphere_network": {"label": "Network (port group)"},
    "vsphere_folder": {"label": "VM folder", "hint": "Optional"},
    "ip_pool_start": {"label": "Pool start", "mono": True},
    "ip_pool_end": {"label": "Pool end", "mono": True},
    "ip_prefix": {"label": "Prefix length", "type": "number", "hint": "e.g. 24 for 255.255.255.0"},
    "ip_gateway": {"label": "Gateway", "mono": True},
    "ip_dns": {"label": "DNS servers", "type": "list", "mono": True, "hint": "Comma separated. Defaults to the gateway"},
    "ip_scan_use_arp": {"label": "Check the ARP table when looking for free IPs", "type": "bool",
                        "hint": "Needs host networking"},
    "vm_user": {"label": "Login user for other templates",
                "hint": "Templates from image definitions use the image's login user"},
    "default_cpus": {"label": "Default vCPUs", "type": "number"},
    "default_memory_mb": {"label": "Default memory (MB)", "type": "number"},
    "default_disk_gb": {"label": "Default disk (GB)", "type": "number"},
    "template_prefixes": {"label": "Other template prefixes", "type": "list",
                          "hint": "Comma separated. Lists templates built outside the portal (each image "
                                  "definition has its own prefix)."},
    "template_max_age_days": {"label": "Flag templates older than (days)", "type": "number"},
}
TABS = [("vcenter", "vCenter"), ("network", "Network"), ("vms", "VM defaults"), ("ssh-keys", "SSH keys"),
        ("templates", "Images"), ("users", "Users"), ("audit", "Audit log")]


def field_views(section: str, s: Settings, locked: set[str], submitted: dict | None = None) -> list[dict]:
    out = []
    for key in SECTIONS[section]:
        meta = {"type": "text", **FIELDS[key], "key": key, "locked": key in locked}
        value = getattr(s, key)
        if key in SECRET_FIELDS:
            meta["is_set"] = bool(value)
            value = ""  # never send a stored secret back to the browser
        elif meta["type"] == "list":
            value = ", ".join(value)
        if submitted is not None and key not in SECRET_FIELDS and not meta["locked"]:
            value = (submitted.get(key) == "on") if meta["type"] == "bool" else submitted.get(key, "")
        meta["value"] = value
        out.append(meta)
    return out


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def render_settings(request: Request, section: str, status_code: int = 200, submitted: dict | None = None,
                    **ctx) -> HTMLResponse:
    s, locked = get_settings(), env_overrides()
    data = {"section": section, "tabs": TABS, "locked": locked,
            "importable": sorted(locked)}
    if section in SECTIONS and section != "ssh-keys":
        data["fields"] = field_views(section, s, locked, submitted)
    if section == "vcenter":
        data["fields"] = [f for f in data["fields"] if f["key"] in CONNECTION]
        data["has_creds"] = bool(s.vsphere_server and s.vsphere_password)
    if section == "ssh-keys":
        data["keys"] = [{"id": key_id(k), "type": k.split()[0], "comment": " ".join(k.split()[2:]),
                         "short": k.split()[1][-16:]} for k in s.default_ssh_keys]
    if section == "templates":
        path, generated = packer.key_source()
        try:
            data["build_key"] = {"public": packer.public_key(), "fingerprint": packer.fingerprint(),
                                 "generated": generated, "path": str(path)}
        except (OSError, ValueError) as e:
            data["build_key"] = {"error": str(e), "generated": generated, "path": str(path)}
    if section == "users":
        data["users"] = users.all_users()
    if section == "audit":
        data["entries"] = db.query("SELECT * FROM audit ORDER BY id DESC LIMIT 200")
    return render(request, "settings.html", status_code=status_code, **data, **ctx)


def apply_changes(request: Request, user: dict, changes: dict) -> None:
    """Validate the full settings with these changes, then store only what changed."""
    current = get_settings()
    stored = {k: v for k, v in db.load_settings().items() if k in config.EDITABLE}
    try:
        new = config.build({**stored, **changes})
    except ValidationError as e:
        raise ValueError("; ".join(_error_text(err) for err in e.errors())) from None
    changed = {k: getattr(new, k) for k in changes if getattr(new, k) != getattr(current, k)}
    if not changed:
        return
    db.save_settings(changed, SECRET_FIELDS, by=user["username"])
    config.get_settings.cache_clear()
    if any(k.startswith("vsphere_") for k in changed):
        vsphere.close()  # reconnect with the new settings
    audit(request, user, "settings.changed", ", ".join(sorted(changed)))  # names only, never values


def _error_text(err: dict) -> str:
    field = err["loc"][0] if err["loc"] else ""
    label = FIELDS.get(field, {}).get("label", "")
    msg = err["msg"].removeprefix("Value error, ")
    return f"{label}: {msg}" if label else msg


@router.get("/settings", dependencies=[Depends(require_admin)])
async def settings_index():
    return RedirectResponse("/settings/vcenter", status_code=303)


@router.get("/settings/{section}", dependencies=[Depends(require_admin)])
async def settings_page(request: Request, section: str):
    if section not in dict(TABS):
        raise HTTPException(404, "Unknown settings section")
    return render_settings(request, section)


def _form_changes(section: str, form, locked: set[str]) -> dict:
    current = get_settings()
    changes = {}
    for key in SECTIONS[section]:
        if key in locked:
            continue  # set by the environment; the UI can't change it
        kind = FIELDS[key].get("type", "text")
        if kind == "bool":
            changes[key] = form.get(key) == "on"
            continue
        raw = form.get(key)
        if not isinstance(raw, str):
            continue
        if key in SECRET_FIELDS:
            if not raw:
                continue  # empty = keep the stored secret
            changes[key] = raw
        else:
            changes[key] = raw.strip()
    # Don't let a changed server receive the stored password: require it to be re-entered.
    server = changes.get("vsphere_server", current.vsphere_server)
    if (section == "vcenter" and server != current.vsphere_server and current.vsphere_password
            and "vsphere_password" not in changes and "vsphere_password" not in locked):
        raise ValueError("Re-enter the password when you change the server")
    return changes


PLACEMENT = ["vsphere_datacenter", "vsphere_cluster", "vsphere_datastore", "vsphere_network", "vsphere_folder"]
CONNECTION = ["vsphere_server", "vsphere_user", "vsphere_password", "vsphere_insecure"]
# Changing a field resets the fields that depend on it.
DEPENDENTS = {"vsphere_datacenter": PLACEMENT[1:], "vsphere_cluster": ["vsphere_datastore", "vsphere_network"]}


def _credentials(form) -> dict:
    """Connection values from the form, falling back to the stored ones (password: only for the same server)."""
    s = get_settings()
    changes = _form_changes("vcenter", form, env_overrides())  # raises if the server changed without a password
    creds = {"server": changes.get("vsphere_server", s.vsphere_server),
             "user": changes.get("vsphere_user", s.vsphere_user),
             "password": changes.get("vsphere_password", s.vsphere_password),
             "insecure": changes.get("vsphere_insecure", s.vsphere_insecure)}
    Settings.model_validate({"vsphere_server": creds["server"]})
    if not all((creds["server"], creds["user"], creds["password"])):
        raise ValueError("Enter the server, user and password first")
    return creds


def _select(key: str, label: str, value: str, options: list[tuple[str, str]], locked: set[str], *,
            optional: bool = False, cascade: bool = False, empty: str = "Choose…") -> dict:
    names = [v for v, _ in options]
    missing = bool(value) and value not in names
    if missing:
        options = [(value, f"{value} (not found in vCenter)")] + options
    return {"key": key, "label": label, "value": value, "options": options, "locked": key in locked,
            "missing": missing, "optional": optional, "cascade": cascade, "empty": empty}


def placement_view(inv: dict, chosen: dict, locked: set[str]) -> list[dict]:
    def auto(value: str, names: list[str]) -> str:
        return names[0] if not value and len(names) == 1 else value

    dcs = inv["datacenters"]
    dc_val = auto(chosen["vsphere_datacenter"], [d["name"] for d in dcs])
    dc = next((d for d in dcs if d["name"] == dc_val), None)
    clusters = dc["clusters"] if dc else []
    cl_val = auto(chosen["vsphere_cluster"], [c["name"] for c in clusters])
    cl = next((c for c in clusters if c["name"] == cl_val), None)
    stores = cl["datastores"] if cl else []
    nets = cl["networks"] if cl else []
    ds_val = auto(chosen["vsphere_datastore"], [d["name"] for d in stores])
    net_val = auto(chosen["vsphere_network"], nets)
    first = "Choose a datacenter first" if not dc else "Choose a cluster first"
    return [
        _select("vsphere_datacenter", "Datacenter", dc_val, [(d["name"], d["name"]) for d in dcs], locked,
                cascade=True),
        _select("vsphere_cluster", "Cluster", cl_val, [(c["name"], c["name"]) for c in clusters], locked,
                cascade=True, empty="Choose…" if dc else first),
        _select("vsphere_datastore", "Datastore", ds_val,
                [(d["name"], f"{d['name']} · {d['free_gb']:,} GB free of {d['capacity_gb']:,} GB ({d['type']})")
                 for d in stores], locked, empty="Choose…" if cl else first),
        _select("vsphere_network", "Network (port group)", net_val, [(n, n) for n in nets], locked,
                empty="Choose…" if cl else first),
        _select("vsphere_folder", "VM folder", chosen["vsphere_folder"],
                [(f, f) for f in (dc["folders"] if dc else [])], locked, optional=True,
                empty="(datacenter root)"),
    ]


@router.post("/settings/vcenter/placement")
async def vcenter_placement(request: Request, user: dict = Depends(require_admin)):
    """htmx: connect with the form's credentials and render the placement dropdowns."""
    form = await request.form()
    s, locked = get_settings(), env_overrides()
    chosen = {k: (form.get(k) if k not in locked and isinstance(form.get(k), str) else getattr(s, k))
              for k in PLACEMENT}
    trigger = request.headers.get("HX-Trigger-Name", "")
    for k in DEPENDENTS.get(trigger, []):
        if k not in locked:
            chosen[k] = ""
    ctx = {"chosen": chosen, "locked": locked}
    try:
        creds = _credentials(form)
        inv = await asyncio.wait_for(asyncio.to_thread(
            vsphere.inventory, **creds, fresh=form.get("fresh") == "1"), timeout=45)
    except Exception as e:  # noqa: BLE001 - shown to the admin; falls back to text fields
        if not isinstance(e, ValueError):
            audit(request, user, "settings.vcenter_connect_failed", str(form.get("vsphere_server", ""))[:100])
        text = getattr(e, "msg", None) or str(e) or type(e).__name__
        fields = [f for f in field_views("vcenter", s, locked) if f["key"] in PLACEMENT]
        for f in fields:
            f["value"] = chosen[f["key"]]
        return render(request, "_placement.html", error_text=text, fields=fields, **ctx)
    return render(request, "_placement.html", about=inv["about"], selects=placement_view(inv, chosen, locked),
                  **ctx)


@router.post("/settings/import-env")
async def import_env(request: Request, user: dict = Depends(require_admin)):
    s = get_settings()
    values = {k: getattr(s, k) for k in env_overrides()}
    if values:
        db.save_settings(values, SECRET_FIELDS, by=user["username"])
        audit(request, user, "settings.imported_env", ", ".join(sorted(values)))
    return render_settings(request, "vcenter", notice=f"Saved {len(values)} value(s) from the environment. "
                           "You can now remove them from the environment and restart the portal.")


@router.post("/settings/templates/regenerate-key")
async def regenerate_build_key(request: Request, user: dict = Depends(require_admin)):
    if packer.building():
        return render_settings(request, "templates", status_code=409, error="Wait for the running build to finish")
    try:
        await asyncio.to_thread(packer.regenerate_key)
    except ValueError as e:
        return render_settings(request, "templates", status_code=400, error=str(e))
    audit(request, user, "settings.build_key_regenerated", packer.fingerprint())
    return render_settings(request, "templates", notice="New build key generated. It's used from the next build.")


@router.post("/settings/ssh-keys/add")
async def ssh_key_add(request: Request, user: dict = Depends(require_admin), key: str = Form("", max_length=16000)):
    if "default_ssh_keys" in env_overrides():
        raise HTTPException(400, "SSH keys are set by the environment")
    new = [k.strip() for k in key.splitlines() if k.strip()]
    try:
        apply_changes(request, user, {"default_ssh_keys": get_settings().default_ssh_keys + new})
    except ValueError as e:
        return render_settings(request, "ssh-keys", status_code=400, error=str(e))
    return RedirectResponse("/settings/ssh-keys", status_code=303)


@router.post("/settings/ssh-keys/{kid}/delete")
async def ssh_key_delete(request: Request, kid: str, user: dict = Depends(require_admin)):
    if "default_ssh_keys" in env_overrides():
        raise HTTPException(400, "SSH keys are set by the environment")
    keys = get_settings().default_ssh_keys
    apply_changes(request, user, {"default_ssh_keys": [k for k in keys if key_id(k) != kid]})
    return RedirectResponse("/settings/ssh-keys", status_code=303)


# --- users ------------------------------------------------------------------------

def _target_user(uid: int, me: dict) -> dict:
    u = users.get(uid)
    if not u:
        raise HTTPException(404, "No such user")
    if u["id"] == me["id"]:
        raise HTTPException(400, "Use the Account page to change your own account")
    return u


@router.post("/settings/users")
async def user_create(request: Request, user: dict = Depends(require_admin), username: str = Form(""),
                      password: str = Form(""), role: str = Form("user")):
    try:
        await asyncio.to_thread(users.create, username, password, role)
    except ValueError as e:
        return render_settings(request, "users", status_code=400, error=str(e), new_username=username)
    audit(request, user, "user.created", f"{username.strip().lower()} ({role})")
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/users/{uid}/role")
async def user_role(request: Request, uid: int, user: dict = Depends(require_admin), role: str = Form("")):
    u = _target_user(uid, user)
    if role not in users.ROLES:
        raise HTTPException(400, "Unknown role")
    # Bumping session_version makes the new role take effect on a fresh login.
    db.execute("UPDATE users SET role=?, session_version=session_version+1 WHERE id=?", (role, uid))
    audit(request, user, "user.role", f"{u['username']} -> {role}")
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/users/{uid}/disable")
async def user_disable(request: Request, uid: int, user: dict = Depends(require_admin)):
    u = _target_user(uid, user)
    disabled = 0 if u["disabled"] else 1
    db.execute("UPDATE users SET disabled=?, session_version=session_version+1 WHERE id=?", (disabled, uid))
    audit(request, user, "user.disabled" if disabled else "user.enabled", u["username"])
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/users/{uid}/password")
async def user_password(request: Request, uid: int, user: dict = Depends(require_admin),
                        password: str = Form("")):
    u = _target_user(uid, user)
    try:
        await asyncio.to_thread(users.set_password, uid, password)
    except ValueError as e:
        return render_settings(request, "users", status_code=400, error=f"{u['username']}: {e}")
    audit(request, user, "user.password_reset", u["username"])
    return render_settings(request, "users", notice=f"Password for {u['username']} changed; their sessions were logged out.")


@router.post("/settings/users/{uid}/delete")
async def user_delete(request: Request, uid: int, user: dict = Depends(require_admin)):
    u = _target_user(uid, user)
    if db.one("SELECT 1 FROM vms WHERE owner_id=? AND status != 'destroyed'", (uid,)):
        return render_settings(request, "users", status_code=400,
                               error=f"{u['username']} still owns VMs. Destroy them or disable the user instead.")
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    audit(request, user, "user.deleted", u["username"])
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/{section}")
async def settings_save(request: Request, section: str, user: dict = Depends(require_admin)):
    if section not in SECTIONS or section == "ssh-keys":
        raise HTTPException(404, "Unknown settings section")
    form = await request.form()
    try:
        apply_changes(request, user, _form_changes(section, form, env_overrides()))
    except ValueError as e:
        submitted = {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}
        return render_settings(request, section, status_code=400, error=str(e), submitted=submitted)
    return render_settings(request, section, notice="Settings saved.")
