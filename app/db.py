"""Tiny SQLite layer. The app is single-process, so one connection + a lock is enough."""
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone

from .config import get_bootstrap

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- vm-create, vm-destroy, packer-build
    target      TEXT NOT NULL,            -- VM or template name
    status      TEXT NOT NULL,            -- queued, running, succeeded, failed, cancelled
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    exit_code   INTEGER
);
CREATE TABLE IF NOT EXISTS vms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,            -- provisioning, ready, failed, destroying, destroyed, forgotten
    template    TEXT NOT NULL,
    cpus        INTEGER NOT NULL,
    memory_mb   INTEGER NOT NULL,
    disk_gb     INTEGER NOT NULL,
    ip_mode     TEXT NOT NULL,            -- dhcp, static
    ip          TEXT,
    has_compose INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    last_job_id INTEGER
);
CREATE INDEX IF NOT EXISTS vms_name ON vms(name);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,            -- JSON, or a Fernet token when encrypted
    encrypted   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    disabled        INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 0,   -- bump to log out every session
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);
CREATE TABLE IF NOT EXISTS image_settings (
    image_id    TEXT PRIMARY KEY,
    data        TEXT NOT NULL,            -- JSON: enabled, iso_url, sizes, keep, parameters
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS revoked_sessions (
    sid         TEXT PRIMARY KEY,         -- session id from a logged-out cookie
    expires_at  TEXT NOT NULL             -- after this the cookie itself has expired
);
CREATE TABLE IF NOT EXISTS backends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type        TEXT NOT NULL,            -- a module in app/platforms: vsphere, proxmox
    enabled     INTEGER NOT NULL DEFAULT 1,
    admin_only  INTEGER NOT NULL DEFAULT 0,
    from_env    INTEGER NOT NULL DEFAULT 0, -- the backend VSPHERE_* / PROXMOX_* describe
    config      TEXT NOT NULL,            -- JSON
    secrets     TEXT NOT NULL DEFAULT '{}', -- JSON: field -> Fernet token
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    username TEXT,
    ip       TEXT,
    action   TEXT NOT NULL,
    detail   TEXT
);
"""

# Columns added after the first release: (table, column, definition).
MIGRATIONS = [
    ("vms", "owner_id", "INTEGER"),
    ("jobs", "user_id", "INTEGER"),
    ("vms", "console_login", "INTEGER NOT NULL DEFAULT 0"),
    ("vms", "vm_user", "TEXT"),
    ("vms", "image_id", "TEXT"),
    ("vms", "notes", "TEXT NOT NULL DEFAULT ''"),
    # Wanted autostart: 1 on, 0 off, NULL not managed (VMs from before the setting existed).
    ("vms", "autostart", "INTEGER"),
    # The backend the VM is on. NULL: from before backends existed (the first vCenter).
    ("vms", "backend_id", "INTEGER"),
    # Proxmox: the first-boot seed ISO still holds the VM's secrets (it's replaced by a
    # cleaned copy once the VM has booted).
    ("vms", "seed_pending", "INTEGER NOT NULL DEFAULT 0"),
]

# Settings that were global before backends existed, and become the first backend.
LEGACY_VSPHERE = ("vsphere_server", "vsphere_user", "vsphere_password", "vsphere_insecure", "vsphere_datacenter",
                  "vsphere_cluster", "vsphere_datastore", "vsphere_network", "vsphere_folder")
LEGACY_PROXMOX = ("proxmox_server", "proxmox_token_id", "proxmox_token_secret", "proxmox_fingerprint",
                  "proxmox_node", "proxmox_storage", "proxmox_iso_storage", "proxmox_bridge", "proxmox_pool")
LEGACY_POOL = ("ip_pool_start", "ip_pool_end", "ip_prefix", "ip_gateway", "ip_dns")
# Settings only versions from before image definitions wrote (see images.migrate_legacy).
LEGACY_PACKER = {"packer_iso_url", "packer_cpus", "packer_memory_mb", "packer_disk_mb", "template_keep"}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = get_bootstrap().db_path
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        init_schema(_conn)
        path.chmod(0o600)  # holds password hashes and encrypted secrets
    return _conn


def init_schema(c: sqlite3.Connection) -> None:
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA)
    for table, col, definition in MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    _migrate_backends(c)
    c.commit()


def _migrate_backends(c: sqlite3.Connection) -> None:
    """Before backends existed, the vCenter connection and the IP pool were global settings.
    They become a backend (one per type that was set up), marked as the one the environment
    describes, as the environment always beat stored settings. Secrets move still encrypted.
    Runs when the database has no backends yet: an upgrade, or a restored older backup."""
    if c.execute("SELECT 1 FROM backends LIMIT 1").fetchone():
        return
    rows = {r[0]: (r[1], r[2]) for r in c.execute("SELECT key, value, encrypted FROM settings")}
    ts = now()
    first = None
    for kind, name, keys in (("vsphere", "vCenter", LEGACY_VSPHERE), ("proxmox", "Proxmox VE", LEGACY_PROXMOX)):
        if keys[0] not in rows:
            continue
        config, secrets = {}, {}
        for k in (*keys, *(LEGACY_POOL if first is None else ())):
            if k in rows:
                value, encrypted = rows[k]
                if encrypted:
                    secrets[k] = value
                else:
                    config[k] = json.loads(value)
        if kind == "vsphere" and "vsphere_insecure" not in rows and set(rows) & LEGACY_PACKER:
            # Versions from before image definitions skipped TLS verification by default; keep
            # that for their vCenter so the upgrade doesn't break it (images.migrate_legacy
            # did this before backends existed, and says so in the audit log).
            config["vsphere_insecure"] = True
            c.execute("INSERT INTO audit (ts, username, action, detail) VALUES (?, 'migration',"
                      " 'settings.vsphere_insecure_kept', ?)", (ts, "upgrade kept the old default: TLS certificate "
                                                                    "verification is off; turn it on in Settings"))
        cur = c.execute("INSERT INTO backends (name, type, enabled, admin_only, from_env, config, secrets, created_at,"
                        " updated_at, updated_by) VALUES (?, ?, 1, 0, 1, ?, ?, ?, ?, 'migration')",
                        (name, kind, json.dumps(config), json.dumps(secrets), ts, ts))
        if kind == "vsphere":
            c.execute("UPDATE vms SET backend_id=? WHERE backend_id IS NULL", (cur.lastrowid,))
        first = first or cur.lastrowid
    if first is not None:
        c.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(LEGACY_VSPHERE + LEGACY_PROXMOX + LEGACY_POOL))})",
                  LEGACY_VSPHERE + LEGACY_PROXMOX + LEGACY_POOL)
        c.execute("INSERT INTO audit (ts, username, action, detail) VALUES (?, 'migration', 'backends.migrated', ?)",
                  (ts, "the vCenter connection and IP pool settings became a backend"))
    c.execute("DELETE FROM settings WHERE key='vm_platform'")


def close() -> None:
    """Close the connection (a restore swaps the database file); the next query reopens it."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def dump(tables: tuple[str, ...]) -> dict[str, list[dict]]:
    """Every row of these tables, read in one transaction so they match each other."""
    with _lock:
        c = conn()
        c.execute("BEGIN")
        try:
            return {t: [dict(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        finally:
            c.execute("COMMIT")


def get_meta(key: str, default=None):
    """A portal-internal value in the settings table (never one of the editable settings)."""
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_meta(key: str, value) -> None:
    execute("INSERT INTO settings (key, value, encrypted, updated_at, updated_by) VALUES (?, ?, 0, ?, 'system')"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now()))


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


# --- jobs -------------------------------------------------------------------

def create_job(kind: str, target: str, user_id: int | None = None) -> int:
    return execute(
        "INSERT INTO jobs (kind, target, status, created_at, user_id) VALUES (?, ?, 'queued', ?, ?)",
        (kind, target, now(), user_id),
    )


def set_job_status(job_id: int, status: str, exit_code: int | None = None) -> None:
    if status == "running":
        execute("UPDATE jobs SET status=?, started_at=? WHERE id=?", (status, now(), job_id))
    else:
        execute(
            "UPDATE jobs SET status=?, finished_at=?, exit_code=? WHERE id=?",
            (status, now(), exit_code, job_id),
        )


def get_job(job_id: int) -> dict | None:
    return one("SELECT * FROM jobs WHERE id=?", (job_id,))


def fail_interrupted_jobs() -> None:
    """Jobs can't survive a restart; mark them failed."""
    execute(
        "UPDATE jobs SET status='failed', finished_at=? WHERE status IN ('queued','running')",
        (now(),),
    )
    execute("UPDATE vms SET status='failed' WHERE status IN ('provisioning','destroying')")


# --- vms --------------------------------------------------------------------

# A destroyed VM is gone from vCenter; a forgotten one still exists there but this portal
# no longer manages it (it was moved to another portal). Neither counts as a VM here.
GONE = ("destroyed", "forgotten")

def active_vm(name: str) -> dict | None:
    return one("SELECT * FROM vms WHERE name=? AND status NOT IN ('destroyed', 'forgotten')", (name,))


def active_vms() -> list[dict]:
    return query("SELECT * FROM vms WHERE status NOT IN ('destroyed', 'forgotten') ORDER BY name")


def reserved_ips() -> set[str]:
    rows = query("SELECT ip FROM vms WHERE status NOT IN ('destroyed', 'forgotten') AND ip IS NOT NULL")
    return {r["ip"] for r in rows}


# --- settings ---------------------------------------------------------------

def load_settings() -> dict:
    """Stored settings, secrets decrypted. Undecryptable values are skipped (and logged)."""
    from . import crypto
    out = {}
    for r in query("SELECT key, value, encrypted FROM settings"):
        raw = r["value"]
        if r["encrypted"]:
            raw = crypto.decrypt(raw)
            if raw is None:
                log.error("Can't decrypt stored setting %s: the encryption key changed. Re-enter it.", r["key"])
                continue
        out[r["key"]] = json.loads(raw)
    return out


def save_settings(values: dict, secret_keys: set[str], by: str) -> None:
    from . import crypto
    ts = now()
    with _lock:
        c = conn()
        for k, v in values.items():
            raw = json.dumps(v)
            enc = k in secret_keys
            c.execute(
                "INSERT INTO settings (key, value, encrypted, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=excluded.encrypted,"
                " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (k, crypto.encrypt(raw) if enc else raw, int(enc), ts, by),
            )
        c.commit()


# --- users & audit ----------------------------------------------------------

def user_count() -> int:
    return one("SELECT COUNT(*) AS n FROM users")["n"]


def create_user(username: str, password_hash: str, role: str, *, only_if_first: bool = False) -> int | None:
    """Insert a user. With only_if_first, do it atomically only when there are no users yet."""
    with _lock:
        c = conn()
        if only_if_first and c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return None
        cur = c.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, now()),
        )
        c.commit()
        return cur.lastrowid


def revoke_session(sid: str, ttl_seconds: int) -> None:
    """Remember a logged-out session id until every copy of its cookie has expired."""
    from datetime import timedelta
    ts = datetime.now(timezone.utc)
    expires = (ts + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    with _lock:
        c = conn()
        c.execute("DELETE FROM revoked_sessions WHERE expires_at < ?", (ts.isoformat(timespec="seconds"),))
        c.execute("INSERT OR REPLACE INTO revoked_sessions (sid, expires_at) VALUES (?, ?)", (sid, expires))
        c.commit()


def session_revoked(sid: str) -> bool:
    return one("SELECT 1 FROM revoked_sessions WHERE sid=?", (sid,)) is not None


def audit(action: str, detail: str = "", *, username: str | None = None, ip: str | None = None) -> None:
    execute("INSERT INTO audit (ts, username, ip, action, detail) VALUES (?, ?, ?, ?, ?)",
            (now(), username, ip, action, detail[:1000]))
