"""User accounts: scrypt password hashes, login throttling, first-admin bootstrap."""
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time

from . import db
from .config import get_bootstrap

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{2,31}$")
ROLES = ("admin", "user")
MIN_PASSWORD = 12
MAX_PASSWORD = 256
COMMON = {"password1234", "123456789012", "qwertyuiop12", "administrator", "changeme1234", "letmein12345"}

# scrypt cost: N=2^15, r=8 → 32 MiB and roughly 50-100 ms per hash.
_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            maxmem=_MAXMEM, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(expected))


_dummy_hash: str | None = None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_hex(16))
    return _dummy_hash


def validate_username(username: str) -> str:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("Username must be 3-32 characters: lowercase letters, digits, '.', '_' or '-', "
                         "starting with a letter")
    return username


def validate_password(password: str, username: str = "") -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"Password must be at most {MAX_PASSWORD} characters")
    lowered = password.lower()
    if lowered in COMMON or (username and username.lower() in lowered) or len(set(password)) < 5:
        raise ValueError("Password is too easy to guess")
    return password


# --- queries ----------------------------------------------------------------

def get(user_id: int) -> dict | None:
    return db.one("SELECT * FROM users WHERE id=?", (user_id,))


def by_name(username: str) -> dict | None:
    return db.one("SELECT * FROM users WHERE username=?", (username.strip().lower(),))


def all_users() -> list[dict]:
    return db.query(
        "SELECT u.*, (SELECT COUNT(*) FROM vms v WHERE v.owner_id=u.id AND v.status != 'destroyed') AS vm_count"
        " FROM users u ORDER BY u.username")


def create(username: str, password: str, role: str) -> int:
    username = validate_username(username)
    validate_password(password, username)
    if role not in ROLES:
        raise ValueError("Unknown role")
    if by_name(username):
        raise ValueError(f"User {username!r} already exists")
    return db.create_user(username, hash_password(password), role)


def set_password(user_id: int, password: str) -> None:
    """Change a password and log out every existing session of that user."""
    u = get(user_id)
    validate_password(password, u["username"] if u else "")
    db.execute("UPDATE users SET password_hash=?, session_version=session_version+1 WHERE id=?",
               (hash_password(password), user_id))


def authenticate(username: str, password: str) -> dict | None:
    """The user if the credentials are valid and the account is enabled. Constant work either way."""
    u = by_name(username) if len(password) <= MAX_PASSWORD else None
    if u is None:
        verify_password(password[:MAX_PASSWORD], _dummy())  # same cost as a real check
        return None
    if not verify_password(password, u["password_hash"]) or u["disabled"]:
        return None
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), u["id"]))
    return u


# --- login throttling --------------------------------------------------------

class Throttle:
    """Sliding-window failure counter per key (client IP, username)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        ts = [t for t in self._fails.get(key, []) if now - t < self.window]
        if ts:
            self._fails[key] = ts
        else:
            self._fails.pop(key, None)
        return ts

    def retry_after(self, key: str) -> int:
        with self._lock:
            now = time.monotonic()
            ts = self._recent(key, now)
            return int(self.window - (now - ts[0])) + 1 if len(ts) >= self.limit else 0

    def fail(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            if len(self._fails) > 10_000:  # bound memory under a spray attack
                for k in list(self._fails):
                    self._recent(k, now)
            self._fails.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()


ip_throttle = Throttle(limit=10, window=15 * 60)
user_throttle = Throttle(limit=5, window=15 * 60)


# --- first run ---------------------------------------------------------------

setup_token: str | None = None


def ensure_bootstrap() -> None:
    """With no users yet: create the admin from ADMIN_PASSWORD, or require a setup token."""
    global setup_token
    b = get_bootstrap()
    if db.user_count():
        setup_token = None
        if b.admin_password:
            log.warning("ADMIN_PASSWORD is only used to create the first admin. You can remove it from the environment.")
        return
    if b.admin_password:
        username = validate_username(b.admin_username)
        if len(b.admin_password) < MIN_PASSWORD:
            log.warning("ADMIN_PASSWORD is shorter than %d characters. Change it under Account.", MIN_PASSWORD)
        db.create_user(username, hash_password(b.admin_password), "admin", only_if_first=True)
        db.audit("user.bootstrap", f"created admin {username!r} from ADMIN_PASSWORD")
        log.info("Created admin user %r from ADMIN_PASSWORD", username)
        return
    setup_token = secrets.token_urlsafe(18)
    log.warning("No users yet. Open /setup and enter this setup token: %s", setup_token)


def check_setup_token(token: str) -> bool:
    return setup_token is not None and hmac.compare_digest(token.strip().encode(), setup_token.encode())
