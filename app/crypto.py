"""Key files and encryption of stored secrets (Fernet: AES-128-CBC + HMAC-SHA256)."""
import logging
import os
import secrets
import stat
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


def _key_file(name: str, generate) -> bytes:
    """Read a key from data_dir, creating it (mode 0600) on first use."""
    from .config import get_bootstrap
    path: Path = get_bootstrap().data_dir / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            log.warning("%s is readable by group/others (mode %o); tightening to 0600", path, mode)
            path.chmod(0o600)
        key = path.read_bytes().strip()
        if not key:
            raise RuntimeError(f"{path} is empty; restore it from backup or delete it to start over")
        return key
    key = generate()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key + b"\n")
    log.info("Generated %s", path)
    return key


@lru_cache
def fernet() -> Fernet:
    from .config import get_bootstrap
    key = get_bootstrap().encryption_key
    return Fernet(key.encode() if key else _key_file("encryption.key", Fernet.generate_key))


@lru_cache
def session_key() -> str:
    return _key_file("session.key", lambda: secrets.token_urlsafe(48).encode()).decode()


def encrypt(plaintext: str) -> str:
    return fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    """None if the token can't be decrypted (wrong or rotated key)."""
    try:
        return fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def reset() -> None:
    fernet.cache_clear()
    session_key.cache_clear()
