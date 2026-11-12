"""First-run setup, the user's own account, and the admin Settings pages."""
import asyncio
import hashlib
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from . import backends, config, db, feedback, packer, users
from .auth import client_ip, current_user, require_admin, require_user, start_session
from .config import SECRET_FIELDS, SECTIONS, Settings, env_overrides, get_settings
from .platforms import PLATFORMS

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
        return RedirectResponse("/setup/backend" if u and u["role"] == "admin" else "/login", status_code=303)
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
    return RedirectResponse("/setup/backend", status_code=303)


# The second and third steps set up the first backend: its connection and placement, then
# its IP pool (with the default SSH keys). More backends are added under Settings.

def _setup_backend() -> backends.Backend | None:
    return next(iter(backends.all_backends()), None)


@router.get("/setup/vcenter", dependencies=[Depends(require_admin)])
async def setup_vcenter_redirect():
    return RedirectResponse("/setup/backend", status_code=303)


@router.get("/setup/backend", dependencies=[Depends(require_admin)])
async def setup_backend_form(request: Request):
    return render_setup(request, "backend", _setup_backend())


@router.post("/setup/backend")
async def setup_backend(request: Request, user: dict = Depends(require_admin)):
    from .routes_backends import save_backend
    form = await request.form()
    b = _setup_backend()
    try:
        if b is None:   # first: which kind of backend
            kind = str(form.get("type", ""))
            if kind not in PLATFORMS:
                raise ValueError("Choose the type of backend")
            name = str(form.get("name", "")).strip() or backends.DEFAULT_NAMES.get(kind, kind)
            backends.create(name, kind, {}, by=user["username"])
            audit(request, user, "backend.created", f"{name} ({kind})")
            return RedirectResponse("/setup/backend", status_code=303)
        await save_backend(request, user, b, form, keys=[*b.platform.CONNECTION, *b.platform.PLACEMENT],
                           general=False)
        b = backends.get(b.id)
        missing = [FIELDS[k]["label"] for k in b.platform.REQUIRED if not getattr(b.cfg, k)]
        if missing:
            raise ValueError("Still missing: " + ", ".join(missing))
    except ValueError as e:
        return render_setup(request, "backend", b, status_code=400, error=str(e))
    return RedirectResponse("/setup/network", status_code=303)


@router.get("/setup/network", dependencies=[Depends(require_admin)])
async def setup_network_form(request: Request):
    return render_setup(request, "network", _setup_backend())


@router.post("/setup/network")
async def setup_network(request: Request, user: dict = Depends(require_admin)):
    from .routes_backends import save_backend
    form = await request.form()
    locked = env_overrides()
    b = _setup_backend()
    try:
        if b is not None:
            await save_backend(request, user, b, form, keys=config.POOL_FIELDS, general=False)
        new_keys = [k.strip() for k in str(form.get("ssh_keys", "")).splitlines() if k.strip()]
        if new_keys and "default_ssh_keys" not in locked:
            apply_changes(request, user, {"default_ssh_keys": get_settings().default_ssh_keys + new_keys})
    except ValueError as e:
        return render_setup(request, "network", b, status_code=400, error=str(e), ssh_keys=form.get("ssh_keys", ""))
    audit(request, user, "setup.completed")
    return RedirectResponse("/", status_code=303)


def render_setup(request: Request, step: str, b: backends.Backend | None, status_code: int = 200,
                 **ctx) -> HTMLResponse:
    from .routes_backends import backend_context
    data = backend_context(b) if b else {"backend": None}
    if step == "backend":
        data["types"] = [(p.NAME, p.LABEL) for p in PLATFORMS.values()]
    else:
        data["fields"] = data.get("pool_fields", [])
    return render(request, f"setup_{step}.html", status_code=status_code, locked=env_overrides(), **data, **ctx)


# --- own account ----------------------------------------------------------------

@router.get("/account")
async def account(request: Request, user: dict = Depends(require_user)):
    return render(request, "account.html")


@router.post("/account/password")
async def change_password(request: Request, user: dict = Depends(require_user), current: str = Form(""),
                          password: str = Form(""), confirm: str = Form("")):
    def fail(msg: str):
        return render(request, "account.html", status_code=400, error=msg)

    # Throttled like the step-up confirmation, so a stolen session can't guess the password.
    if users.user_throttle.retry_after(user["username"]):
        return fail("Too many wrong passwords. Try again later.")
    if not await asyncio.to_thread(users.verify_password, current, user["password_hash"]):
        users.user_throttle.fail(user["username"])
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
    "proxmox_server": {"label": "Server", "hint": "Hostname or IP of a Proxmox node, optionally with :port (8006)"},
    "proxmox_token_id": {"label": "API token ID", "mono": True,
                         "hint": "user@realm!tokenname. Use a dedicated user and token with only the "
                                 "permissions the portal needs"},
    "proxmox_token_secret": {"label": "API token secret", "type": "password"},
    "proxmox_fingerprint": {"label": "Certificate fingerprint (SHA-256)", "mono": True,
                            "hint": "Only for Proxmox's own (self-signed) certificate: copy it from the node's "
                                    "System > Certificates. Empty: the certificate must be trusted like any website's"},
    "proxmox_node": {"label": "Node"},
    "proxmox_storage": {"label": "Storage for VM disks"},
    "proxmox_iso_storage": {"label": "Storage for ISO images"},
    "proxmox_bridge": {"label": "Network bridge"},
    "proxmox_pool": {"label": "Resource pool", "hint": "Optional"},
    "proxmox_vlan": {"label": "VLAN tag", "hint": "Optional, 1 to 4094: tags the network cards of new VMs and of "
                                                  "template builds on the bridge. Empty: untagged"},
    "proxmox_nodes": {"label": "More nodes for VMs", "type": "list",
                      "hint": "Optional, comma separated: other nodes of the cluster users may put a VM on. "
                              "The storages and the bridge must exist there under the same names"},
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
    "packer_http_ip": {"label": "Build HTTP address", "mono": True,
                       "hint": "IP that build VMs fetch installer files from (Debian preseed). Empty: the "
                               "address the portal uses to reach the backend's API."},
    "allow_image_editing": {"label": "Allow creating and editing image definitions in the browser",
                            "type": "bool", "confirm": True,
                            "hint": "Definitions are code: Packer runs them inside the portal with the backends' "
                                    "credentials. Only turn this on if you trust every admin with that."},
    "backup_enabled": {"label": "Make scheduled backups", "type": "bool",
                       "hint": "Full backups of the portal, written to the backup folder"},
    "backup_interval_hours": {"label": "Every (hours)", "type": "number"},
    "backup_keep": {"label": "Keep the newest", "type": "number", "hint": "Older scheduled backups are deleted"},
    "backup_recipients": {"label": "Encrypt to these age public keys", "type": "list", "mono": True,
                          "hint": "age1… keys, comma separated. Keep the matching secret keys away from the portal: "
                                  "it can then write backups but never read them."},
    "backup_include_logs": {"label": "Include job logs", "type": "bool"},
    "https_only_cookies": {"label": "HTTPS only (Secure cookies and HSTS)", "type": "bool", "confirm": True,
                           "hint": "Turn on when the portal is served over HTTPS, normally behind a reverse proxy. "
                                   "Browsers then never send the session over plain HTTP."},
}
TABS = [("backends", "Backends"), ("vms", "VM defaults"), ("ssh-keys", "SSH keys"),
        ("templates", "Images"), ("security", "Security"), ("users", "Users"), ("backup", "Backup"),
        ("audit", "Audit log"), ("system", "System")]
# Turning these on needs the admin's password, so a hijacked session can't quietly do it.
CONFIRM_TO_ENABLE = {k for k, v in FIELDS.items() if v.get("confirm")}


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
    env_backends = [x for x in backends.all_backends() if x.from_env and x.locked]
    data = {"section": section, "tabs": TABS, "locked": locked,
            "importable": sorted(locked) + [f"{x.name}: {k}" for x in env_backends for k in sorted(x.locked)]}
    if section == "backends" and "backend" not in ctx:
        from .routes_backends import list_context
        data.update(list_context())
    if section in SECTIONS and section != "ssh-keys":
        data["fields"] = field_views(section, s, locked, submitted)
        data["confirm_fields"] = [FIELDS[f["key"]]["label"] for f in data["fields"]
                                  if f["key"] in CONFIRM_TO_ENABLE and not f["locked"] and not getattr(s, f["key"])]
        data["is_https"] = request.url.scheme == "https"
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
    if section == "system":
        data["system"] = system_info(request)
    if section == "audit":
        data["entries"] = db.query("SELECT * FROM audit ORDER BY id DESC LIMIT 200")
    if section == "backup":
        from .routes_backup import backup_context
        data.update(backup_context(request))
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
    audit(request, user, "settings.changed", ", ".join(sorted(changed)))  # names only, never values


def _error_text(err: dict) -> str:
    field = err["loc"][0] if err["loc"] else ""
    label = FIELDS.get(field, {}).get("label", "")
    msg = err["msg"].removeprefix("Value error, ")
    return f"{label}: {msg}" if label else msg


def system_info(request: Request) -> dict:
    """Read-only view of what can only come from the environment, and why."""
    import os
    import shutil
    b = config.get_bootstrap()
    path, generated = packer.key_source()
    return {
        "env_only": [
            ("Data directory", "DATA_DIR", str(b.data_dir),
             "Holds the database itself, so it can't be stored in it."),
            ("Encryption key", "ENCRYPTION_KEY",
             "from the environment" if b.encryption_key else f"generated file in {b.data_dir}",
             "Decrypts stored secrets. Set it in the environment to keep it out of the data volume and its backups."),
            ("Session signing key", "SECRET_KEY",
             "from the environment" if b.secret_key else f"generated file in {b.data_dir}",
             "Changing it logs everyone out."),
            ("Listen port", "PORT", os.environ.get("PORT", "8080"), "Read before the portal starts."),
            ("Build SSH key", "PACKER_SSH_KEY", "generated by the portal" if generated else str(path),
             "Regenerate it under Images."),
            ("Built-in image definitions", "IMAGES_DIR", str(b.images_dir), "Shipped read-only in the container."),
            ("First admin", "ADMIN_USERNAME / ADMIN_PASSWORD", "only used while there are no users",
             "Afterwards, manage users under Users."),
        ],
        "overrides": sorted(env_overrides() | (config.env_fields() & config.BACKEND_FIELDS)),
        "tools": {t: bool(shutil.which(t)) for t in ("tofu", "packer", "xorriso")},
        "https": request.url.scheme == "https",
    }


def check_confirmations(request: Request, user: dict, changes: dict, form) -> None:
    """Enabling a sensitive switch needs the admin's password; HTTPS-only also needs HTTPS."""
    current = get_settings()
    turning_on = [k for k in CONFIRM_TO_ENABLE if changes.get(k) is True and not getattr(current, k)]
    if not turning_on:
        return
    if "https_only_cookies" in turning_on and request.url.scheme != "https":
        raise ValueError("Open the portal over HTTPS first: turning this on over plain HTTP would log "
                         "everyone out, because browsers would stop sending the session cookie. If you are "
                         "using HTTPS, the proxy isn't trusted: set FORWARDED_ALLOW_IPS to its address")
    labels = ", ".join(FIELDS[k]["label"] for k in sorted(turning_on))
    confirm_password(request, user, form.get("confirm_password"), ", ".join(sorted(turning_on)),
                     f"Enter your password to turn on: {labels}")


def confirm_password(request: Request, user: dict, password, what: str, message: str) -> None:
    """Step-up check: the acting admin's own password (throttled). Raises ValueError(message)."""
    if users.user_throttle.retry_after(user["username"]):
        raise ValueError("Too many wrong passwords. Try again later.")
    if not isinstance(password, str) or not users.verify_password(password, user["password_hash"]):
        users.user_throttle.fail(user["username"])
        audit(request, user, "settings.confirm_failed", what)
        raise ValueError(message)


@router.get("/settings", dependencies=[Depends(require_admin)])
async def settings_index():
    return RedirectResponse("/settings/backends", status_code=303)


@router.get("/settings/vcenter", dependencies=[Depends(require_admin)])
async def settings_vcenter():
    """The page from before backends existed: now the first vCenter backend's."""
    b = next((x for x in backends.all_backends() if x.type == "vsphere"), None)
    return RedirectResponse(f"/settings/backends/{b.id}" if b else "/settings/backends", status_code=303)


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
    return changes


@router.post("/settings/import-env")
async def import_env(request: Request, user: dict = Depends(require_admin)):
    s = get_settings()
    values = {k: getattr(s, k) for k in env_overrides()}
    if values:
        db.save_settings(values, SECRET_FIELDS, by=user["username"])
    done = sorted(values) + backends.import_env(user["username"])
    if done:
        audit(request, user, "settings.imported_env", ", ".join(done))
    return render_settings(request, "backends", notice=f"Saved {len(done)} value(s) from the environment. "
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
    return feedback.redirect(request, "/settings/ssh-keys",
                             notice=f"Added {len(new)} key{'' if len(new) == 1 else 's'}. New VMs get "
                                    f"{'it' if len(new) == 1 else 'them'}; existing VMs don't change.")


@router.post("/settings/ssh-keys/{kid}/delete")
async def ssh_key_delete(request: Request, kid: str, user: dict = Depends(require_admin)):
    if "default_ssh_keys" in env_overrides():
        raise HTTPException(400, "SSH keys are set by the environment")
    keys = get_settings().default_ssh_keys
    left = [k for k in keys if key_id(k) != kid]
    if len(left) == len(keys):
        return feedback.redirect(request, "/settings/ssh-keys", error="That key was already removed.")
    apply_changes(request, user, {"default_ssh_keys": left})
    return feedback.redirect(request, "/settings/ssh-keys",
                             notice="Key removed. New VMs won't get it; VMs that have it keep it.")


# --- users ------------------------------------------------------------------------

def _target_user(uid: int, me: dict) -> dict:
    u = users.get(uid)
    if not u:
        raise HTTPException(404, "No such user")
    if u["id"] == me["id"]:
        raise HTTPException(400, "Use the Account page to change your own account")
    return u


# Creating credentials someone knows for an admin account (a new admin, a promotion, a
# password reset) needs the acting admin's password, like the sensitive switches do:
# otherwise a stolen session could mint an admin it controls and pass that confirmation.

@router.post("/settings/users")
async def user_create(request: Request, user: dict = Depends(require_admin), username: str = Form(""),
                      password: str = Form(""), role: str = Form("user"), confirm: str = Form("")):
    try:
        if role == "admin":
            await asyncio.to_thread(confirm_password, request, user, confirm, "user.create admin",
                                    "Enter your password to add an admin")
        await asyncio.to_thread(users.create, username, password, role)
    except ValueError as e:
        return render_settings(request, "users", status_code=400, error=str(e), new_username=username)
    audit(request, user, "user.created", f"{username.strip().lower()} ({role})")
    return feedback.redirect(request, "/settings/users", notice=f"Added {username.strip().lower()} ({role}).")


@router.post("/settings/users/{uid}/role")
async def user_role(request: Request, uid: int, user: dict = Depends(require_admin), role: str = Form(""),
                    confirm: str = Form("")):
    u = _target_user(uid, user)
    if role not in users.ROLES:
        raise HTTPException(400, "Unknown role")
    if role == "admin" and u["role"] != "admin":
        try:
            await asyncio.to_thread(confirm_password, request, user, confirm, f"user.role {u['username']}",
                                    f"Enter your password to make {u['username']} an admin")
        except ValueError as e:
            return render_settings(request, "users", status_code=400, error=str(e))
    # Bumping session_version makes the new role take effect on a fresh login.
    db.execute("UPDATE users SET role=?, session_version=session_version+1 WHERE id=?", (role, uid))
    audit(request, user, "user.role", f"{u['username']} -> {role}")
    return feedback.redirect(request, "/settings/users",
                             notice=f"{u['username']} is now {'an' if role == 'admin' else 'a'} {role}. "
                                    "It applies from their next login.")


@router.post("/settings/users/{uid}/disable")
async def user_disable(request: Request, uid: int, user: dict = Depends(require_admin)):
    u = _target_user(uid, user)
    disabled = 0 if u["disabled"] else 1
    db.execute("UPDATE users SET disabled=?, session_version=session_version+1 WHERE id=?", (disabled, uid))
    audit(request, user, "user.disabled" if disabled else "user.enabled", u["username"])
    return feedback.redirect(request, "/settings/users",
                             notice=f"{u['username']} is disabled and logged out." if disabled
                             else f"{u['username']} can log in again.")


@router.post("/settings/users/{uid}/password")
async def user_password(request: Request, uid: int, user: dict = Depends(require_admin),
                        password: str = Form(""), confirm: str = Form("")):
    u = _target_user(uid, user)
    try:
        await asyncio.to_thread(confirm_password, request, user, confirm, f"user.password {u['username']}",
                                f"Enter your own password to set a new password for {u['username']}")
        await asyncio.to_thread(users.set_password, uid, password)
    except ValueError as e:
        return render_settings(request, "users", status_code=400, error=f"{u['username']}: {e}")
    audit(request, user, "user.password_reset", u["username"])
    return render_settings(request, "users", notice=f"Password for {u['username']} changed; their sessions were logged out.")


@router.post("/settings/users/{uid}/delete")
async def user_delete(request: Request, uid: int, user: dict = Depends(require_admin)):
    u = _target_user(uid, user)
    if db.one("SELECT 1 FROM vms WHERE owner_id=? AND status NOT IN ('destroyed', 'forgotten')", (uid,)):
        return render_settings(request, "users", status_code=400,
                               error=f"{u['username']} still owns VMs. Destroy them or disable the user instead.")
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    audit(request, user, "user.deleted", u["username"])
    return feedback.redirect(request, "/settings/users", notice=f"Deleted {u['username']}.")


@router.post("/settings/{section}")
async def settings_save(request: Request, section: str, user: dict = Depends(require_admin)):
    if section not in SECTIONS or section == "ssh-keys":
        raise HTTPException(404, "Unknown settings section")
    form = await request.form()
    try:
        changes = _form_changes(section, form, env_overrides())
        await asyncio.to_thread(check_confirmations, request, user, changes, form)
        apply_changes(request, user, changes)
    except ValueError as e:
        submitted = {k: v for k, v in form.items() if isinstance(v, str) and k not in ("csrf_token", "confirm_password")}
        return render_settings(request, section, status_code=400, error=str(e), submitted=submitted)
    return render_settings(request, section, notice="Settings saved.")
