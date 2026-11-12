"""Backends: the hypervisor platforms this portal creates VMs on.

A backend is one named connection, such as "Lab vCenter" or "Proxmox Oslo": its type
(a module in app/platforms), how to connect, where new VMs go and its static IP pool.
A portal has any number of them, of any type, and every VM records the backend it's on.

Stored in the `backends` table: the settings as JSON, the secrets (a vCenter password,
a Proxmox API token secret) each encrypted on their own. Admins manage them under
Settings → Backends.

The environment still works the way it always did: VSPHERE_SERVER and the other
VSPHERE_* variables (and PROXMOX_*) describe one backend of that type, created when
first needed and marked `from_env`. What the environment sets wins over what's stored
and is read-only in the UI. The IP_POOL_* variables apply to those backends too.
"""
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass

from pydantic import ValidationError

from . import crypto, db
from .config import (BACKEND_SECRETS, POOL_FIELDS, BackendFields, env_fields, get_settings)

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,38}[A-Za-z0-9.)]$|^[A-Za-z0-9]$")
DEFAULT_NAMES = {"vsphere": "vCenter", "proxmox": "Proxmox VE"}
_lock = threading.Lock()


def platforms():
    from .platforms import PLATFORMS
    return PLATFORMS


@dataclass(frozen=True)
class Backend:
    id: int
    name: str
    type: str
    enabled: bool            # offered for new VMs and builds (its existing VMs are managed either way)
    admin_only: bool         # only admins may create VMs on it
    from_env: bool           # the backend the environment describes
    cfg: BackendFields       # the stored settings with the environment's on top
    locked: frozenset        # fields set by the environment

    @property
    def platform(self):
        return platforms()[self.type]

    @property
    def fields(self) -> list[str]:
        return fields_for(self.type)

    @property
    def configured(self) -> bool:
        """Everything it needs to connect and place VMs is set."""
        return all(getattr(self.cfg, f) for f in self.platform.REQUIRED)

    @property
    def has_pool(self) -> bool:
        return bool(self.cfg.ip_pool_start and self.cfg.ip_pool_end)

    @property
    def key(self) -> str:
        """Identifies this backend *and* its connection, for connection caches: a changed
        server or secret is a different key, so nothing made with the old one is reused."""
        conn = [str(getattr(self.cfg, f)) for f in self.platform.CONNECTION]
        return f"{self.id}:" + hashlib.sha256("\0".join(conn).encode()).hexdigest()[:16]

    @property
    def label(self) -> str:
        return f"{self.name} ({self.platform.LABEL})"

    def usable_by(self, user: dict | None) -> bool:
        return self.enabled and self.configured and (not self.admin_only or bool(user and user["role"] == "admin"))


def fields_for(kind: str) -> list[str]:
    p = platforms()[kind]
    return [*p.CONNECTION, *p.PLACEMENT, *POOL_FIELDS]


def secrets_for(kind: str) -> list[str]:
    return [f for f in fields_for(kind) if f in BACKEND_SECRETS]


def validate_name(name: str) -> str:
    name = " ".join(name.split())
    if not NAME_RE.match(name):
        raise ValueError("Name the backend with 1-40 letters, digits, spaces, '.', '_', '-' or parentheses")
    return name


def validate(kind: str, values: dict) -> BackendFields:
    """The values of one backend, checked by the same rules as everywhere else. Raises
    ValueError with a message for people."""
    if kind not in platforms():
        raise ValueError(f"Unknown backend type {kind!r}")
    allowed = set(fields_for(kind))
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Not settings of a {platforms()[kind].LABEL} backend: {', '.join(sorted(unknown))}")
    try:
        return BackendFields(**values)
    except ValidationError as e:
        raise ValueError("; ".join(_error_text(err) for err in e.errors())) from None


def _error_text(err: dict) -> str:
    from .admin import FIELDS
    field = err["loc"][0] if err["loc"] else ""
    label = FIELDS.get(field, {}).get("label", field)
    msg = err["msg"].removeprefix("Value error, ")
    return f"{label}: {msg}" if label else msg


# --- reading ------------------------------------------------------------------------------

def _env_for(kind: str) -> dict:
    """The fields of this backend type that the environment sets, with their values."""
    s, env = get_settings(), env_fields()
    return {f: getattr(s, f) for f in fields_for(kind) if f in env}


def _env_kinds() -> list[str]:
    """Backend types the environment describes: it sets their server."""
    env = env_fields()
    return [k for k, p in platforms().items() if p.SERVER_FIELD in env]


def _ensure_env_backends() -> None:
    """Create the backend the environment describes, once per type. VMs from before
    backends existed (no backend recorded) were all on vSphere: they get this one."""
    for kind in _env_kinds():
        if db.one("SELECT 1 FROM backends WHERE from_env=1 AND type=?", (kind,)):
            continue
        with _lock:
            if db.one("SELECT 1 FROM backends WHERE from_env=1 AND type=?", (kind,)):
                continue
            name = _free_name(DEFAULT_NAMES[kind])
            bid = db.execute("INSERT INTO backends (name, type, enabled, admin_only, from_env, config, secrets,"
                             " created_at, updated_at, updated_by) VALUES (?, ?, 1, 0, 1, '{}', '{}', ?, ?, 'system')",
                             (name, kind, db.now(), db.now()))
            if kind == "vsphere":
                db.execute("UPDATE vms SET backend_id=? WHERE backend_id IS NULL", (bid,))
            log.info("Created backend %s from the environment", name)


def _free_name(base: str) -> str:
    taken = {r["name"].lower() for r in db.query("SELECT name FROM backends")}
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base} {n}", n + 1
    return name


def _load(row: dict) -> Backend:
    kind = row["type"]
    stored = json.loads(row["config"] or "{}")
    for k, token in (json.loads(row["secrets"] or "{}")).items():
        raw = crypto.decrypt(token)
        if raw is None:
            log.error("Can't decrypt the %s of backend %s: the encryption key changed. Re-enter it.", k, row["name"])
            continue
        stored[k] = json.loads(raw)
    allowed = set(fields_for(kind)) if kind in platforms() else set()
    stored = {k: v for k, v in stored.items() if k in allowed}
    env = _env_for(kind) if row["from_env"] and kind in platforms() else {}
    values = {**stored, **env}
    for _ in range(len(values) + 1):
        try:
            cfg = BackendFields(**values)
            break
        except ValidationError as e:
            # A bad stored value must not take the backend down: drop it and fix it in the UI.
            bad = {err["loc"][0] for err in e.errors() if err["loc"] and err["loc"][0] in values}
            if not bad:
                bad = set(POOL_FIELDS) & set(values)    # the pool as a whole doesn't add up
            if not bad:
                raise
            log.error("Backend %s: ignoring invalid settings %s", row["name"], sorted(bad))
            for k in bad:
                values.pop(k, None)
    else:
        cfg = BackendFields()
    return Backend(id=row["id"], name=row["name"], type=kind, enabled=bool(row["enabled"]),
                   admin_only=bool(row["admin_only"]), from_env=bool(row["from_env"]), cfg=cfg,
                   locked=frozenset(env))


def all_backends() -> list[Backend]:
    """Every backend, in the order they were added."""
    _ensure_env_backends()
    return [_load(r) for r in db.query("SELECT * FROM backends ORDER BY id")
            if r["type"] in platforms()]


def get(backend_id) -> Backend | None:
    try:
        bid = int(backend_id)
    except (TypeError, ValueError):
        return None
    _ensure_env_backends()
    row = db.one("SELECT * FROM backends WHERE id=?", (bid,))
    return _load(row) if row and row["type"] in platforms() else None


def usable(user: dict | None) -> list[Backend]:
    """Backends this user may create VMs on."""
    return [b for b in all_backends() if b.usable_by(user)]


def of_vm(vm: dict) -> Backend | None:
    """The backend a VM is on. VMs from before backends existed are on the first vCenter."""
    if vm.get("backend_id") is not None:
        return get(vm["backend_id"])
    return next((b for b in all_backends() if b.type == "vsphere"), None)


def vm_count(backend_id: int) -> int:
    return db.one("SELECT COUNT(*) AS n FROM vms WHERE backend_id=? AND status NOT IN ('destroyed', 'forgotten')",
                  (backend_id,))["n"]


# --- changing -------------------------------------------------------------------------------

def _split(kind: str, values: dict) -> tuple[dict, dict]:
    secret = set(secrets_for(kind))
    return ({k: v for k, v in values.items() if k not in secret},
            {k: crypto.encrypt(json.dumps(v)) for k, v in values.items() if k in secret and v})


def _stored(row: dict) -> dict:
    out = json.loads(row["config"] or "{}")
    for k, token in json.loads(row["secrets"] or "{}").items():
        raw = crypto.decrypt(token)
        if raw is not None:
            out[k] = json.loads(raw)
    return out


def create(name: str, kind: str, values: dict, by: str, *, enabled: bool = True, admin_only: bool = False) -> int:
    _ensure_env_backends()   # the environment's backend keeps the lowest id, as if it had been there first
    name = validate_name(name)
    cfg = validate(kind, values)
    if db.one("SELECT 1 FROM backends WHERE name=? COLLATE NOCASE", (name,)):
        raise ValueError(f"There's already a backend named {name!r}")
    plain, secret = _split(kind, {k: getattr(cfg, k) for k in values})
    return db.execute("INSERT INTO backends (name, type, enabled, admin_only, from_env, config, secrets, created_at,"
                      " updated_at, updated_by) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                      (name, kind, int(enabled), int(admin_only), json.dumps(plain), json.dumps(secret),
                       db.now(), db.now(), by))


def update(b: Backend, changes: dict, by: str, *, name: str | None = None, enabled: bool | None = None,
           admin_only: bool | None = None) -> list[str]:
    """Store changed settings (only those the environment doesn't set). Returns the names
    of what changed, for the audit log (never values)."""
    row = db.one("SELECT * FROM backends WHERE id=?", (b.id,))
    if row is None:
        raise ValueError("No such backend")
    changes = {k: v for k, v in changes.items() if k not in b.locked}
    stored = _stored(row)
    server = b.platform.SERVER_FIELD
    secret_field = b.platform.SECRET_FIELD
    # Never send the stored secret to a server it wasn't entered for.
    has_secret = bool(stored.get(secret_field)) or secret_field in b.locked
    if (server in changes and changes[server] != stored.get(server, "") and has_secret
            and secret_field not in changes):
        if secret_field in b.locked:
            raise ValueError(f"The {secret_field.upper()} is set in the environment, so the server can't be changed "
                             f"here: set {server.upper()} in the environment as well")
        raise ValueError("Re-enter the " + ("password" if b.type == "vsphere" else "API token secret")
                         + " when you change the server")
    merged = {**stored, **changes}
    cfg = validate(b.type, {**merged, **{k: getattr(b.cfg, k) for k in b.locked}})
    new = {k: getattr(cfg, k) for k in merged}
    changed = sorted(k for k in new if new[k] != stored.get(k, getattr(BackendFields(), k)))
    plain, secret = _split(b.type, new)
    sets, args = ["config=?", "secrets=?", "updated_at=?", "updated_by=?"], [json.dumps(plain), json.dumps(secret),
                                                                           db.now(), by]
    if name is not None and name != b.name:
        name = validate_name(name)
        if db.one("SELECT 1 FROM backends WHERE name=? COLLATE NOCASE AND id<>?", (name, b.id)):
            raise ValueError(f"There's already a backend named {name!r}")
        sets.append("name=?"), args.append(name), changed.append("name")
    for col, value in (("enabled", enabled), ("admin_only", admin_only)):
        if value is not None and value != getattr(b, col):
            sets.append(f"{col}=?"), args.append(int(value)), changed.append(col)
    db.execute(f"UPDATE backends SET {', '.join(sets)} WHERE id=?", (*args, b.id))
    if changed:
        b.platform.close(b)   # reconnect with the new settings
    return changed


def delete(b: Backend) -> None:
    n = vm_count(b.id)
    if n:
        raise ValueError(f"{b.name} still has {n} VM(s) in this portal. Destroy or forget them first")
    if b.from_env and b.type in _env_kinds():
        raise ValueError(f"{b.name} is described by the environment ({b.platform.SERVER_FIELD.upper()}): "
                         "remove it there first")
    db.execute("DELETE FROM backends WHERE id=?", (b.id,))
    b.platform.close(b)


def import_env(by: str) -> list[str]:
    """Copy what the environment sets for its backends into their stored settings (the
    "Import from environment" button). Returns '<backend>: <field>' for the audit log."""
    done = []
    for b in all_backends():
        if not b.from_env or not b.locked:
            continue
        row = db.one("SELECT * FROM backends WHERE id=?", (b.id,))
        values = {**_stored(row), **{k: getattr(b.cfg, k) for k in b.locked}}
        plain, secret = _split(b.type, values)
        db.execute("UPDATE backends SET config=?, secrets=?, updated_at=?, updated_by=? WHERE id=?",
                   (json.dumps(plain), json.dumps(secret), db.now(), by, b.id))
        done += [f"{b.name}: {k}" for k in sorted(b.locked)]
    return done


def stored_rows() -> list[dict]:
    """The backends as stored (secrets decrypted, nothing from the environment), for a full backup."""
    _ensure_env_backends()
    out = []
    for r in db.query("SELECT * FROM backends ORDER BY id"):
        stored = _stored(r)
        secret = set(BACKEND_SECRETS)
        out.append({"id": r["id"], "name": r["name"], "type": r["type"], "enabled": r["enabled"],
                    "admin_only": r["admin_only"], "from_env": r["from_env"],
                    "config": {k: v for k, v in stored.items() if k not in secret},
                    "secrets": {k: v for k, v in stored.items() if k in secret}})
    return out


def encrypt_secrets(values: dict) -> str:
    return json.dumps({k: crypto.encrypt(json.dumps(v)) for k, v in values.items() if v})
