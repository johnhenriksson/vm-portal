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
    status      TEXT NOT NULL,            -- queued, running, succeeded, failed
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    exit_code   INTEGER
);
CREATE TABLE IF NOT EXISTS vms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,            -- provisioning, ready, failed, destroying, destroyed
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
]

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
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
        for table, col, definition in MIGRATIONS:
            cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        _conn.commit()
        path.chmod(0o600)  # holds password hashes and encrypted secrets
    return _conn


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

def active_vm(name: str) -> dict | None:
    return one("SELECT * FROM vms WHERE name=? AND status != 'destroyed'", (name,))


def active_vms() -> list[dict]:
    return query("SELECT * FROM vms WHERE status != 'destroyed' ORDER BY name")


def reserved_ips() -> set[str]:
    rows = query("SELECT ip FROM vms WHERE status != 'destroyed' AND ip IS NOT NULL")
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
