"""The backup file: an age-encrypted, gzip-compressed tar archive with a manifest.

    manifest.json                     format, version, kind, source installation, SHA-256 per file
    db/<table>.json                   database rows (validated on restore; never a raw SQLite file)
    secrets.json                      stored secrets in plain text; only ever inside the encryption
    vms/<name>/terraform.tfstate      a VM's OpenTofu state
    vms/<name>/terraform.tfvars.json  and its variables
    images/<id>/<path>                custom image definitions
    jobs/<id>.log                     job logs (optional)

age (https://age-encryption.org) gives authenticated encryption, so a modified file
fails to decrypt; the manifest's checksums then make sure nothing was added, dropped
or swapped inside it. Encryption is mandatory: there is no way to write a plain archive.
Reading never touches the filesystem: the archive is checked entirely in memory,
against fixed limits, before anything uses its contents.
"""
import gzip
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pyrage

FORMAT = "vm-portal-backup"
# Bump when the meaning of a file or a table's columns changes; older portals then
# refuse newer backups instead of misreading them.
VERSION = 1
KINDS = ("full", "vms")
SUFFIX = ".vmpbak"

PASSPHRASE_MIN = 16
MAX_FILES = 20000
MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_BYTES = 512 * 1024 * 1024

_VM = r"[a-z][a-z0-9-]{1,30}[a-z0-9]"           # provision.NAME_RE
_IMG = r"[a-z0-9][a-z0-9.-]{1,47}"              # images.ID_RE
_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"      # editor.NAME_RE
ALLOWED_PATHS = re.compile(
    r"manifest\.json|secrets\.json|db/[a-z_]{1,32}\.json"
    rf"|vms/{_VM}/terraform\.(?:tfstate|tfvars\.json)"
    rf"|images/{_IMG}(?:/{_PART}){{1,4}}"
    r"|jobs/[0-9]{1,12}\.log"
)
AGE_HEADER = b"age-encryption.org/v1\n"


class BackupError(ValueError):
    """The file isn't a usable backup (wrong key, damaged, tampered with, or not ours)."""


@dataclass
class Backup:
    manifest: dict
    files: dict[str, bytes] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.manifest["kind"]

    def json(self, path: str, default=None):
        if path not in self.files:
            return default
        try:
            return json.loads(self.files[path])
        except ValueError:
            raise BackupError(f"{path} is not valid JSON") from None


# --- encryption ------------------------------------------------------------------------

def check_passphrase(passphrase: str) -> str:
    if not isinstance(passphrase, str) or len(passphrase) < PASSPHRASE_MIN:
        raise ValueError(f"Use a passphrase of at least {PASSPHRASE_MIN} characters")
    if len(passphrase) > 1024:
        raise ValueError("The passphrase is too long")
    return passphrase


def parse_recipient(text: str):
    try:
        return pyrage.x25519.Recipient.from_str(text.strip())
    except Exception:  # noqa: BLE001 - pyrage raises its own types
        raise ValueError(f"Not an age public key (age1…): {text.strip()[:20]}…") from None


def encrypt(data: bytes, *, passphrase: str | None = None, recipients: list[str] | None = None) -> bytes:
    if bool(passphrase) == bool(recipients):
        raise ValueError("Encrypt with either a passphrase or public keys")
    if passphrase:
        return pyrage.passphrase.encrypt(data, check_passphrase(passphrase))
    return pyrage.encrypt(data, [parse_recipient(r) for r in recipients])


def is_identity(secret: str) -> bool:
    return secret.strip().startswith("AGE-SECRET-KEY-1")


def decrypt(data: bytes, secret: str) -> bytes:
    """Decrypt with a passphrase or an age secret key (AGE-SECRET-KEY-1…)."""
    if not data.startswith(AGE_HEADER):
        raise BackupError("This isn't a vm-portal backup (not an age-encrypted file)")
    if not isinstance(secret, str) or not secret:
        raise BackupError("Enter the backup's passphrase or secret key")
    try:
        if is_identity(secret):
            return pyrage.decrypt(data, [pyrage.x25519.Identity.from_str(secret.strip())])
        return pyrage.passphrase.decrypt(data, secret)
    except Exception:  # noqa: BLE001 - wrong key and tampering look the same, deliberately
        raise BackupError("Can't decrypt the backup: wrong passphrase or key, or the file was changed") from None


def encrypted_to_passphrase(data: bytes) -> bool:
    """True when the file was encrypted with a passphrase (not to public keys)."""
    return b"\n-> scrypt " in data[:256]


# --- archive -----------------------------------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pack(kind: str, files: dict[str, bytes], **meta) -> bytes:
    """A tar.gz of files plus a manifest describing and checksumming them."""
    if kind not in KINDS:
        raise ValueError(f"Unknown backup kind {kind!r}")
    for name in files:
        if name == "manifest.json" or not ALLOWED_PATHS.fullmatch(name):
            raise ValueError(f"Not allowed in a backup: {name}")
    manifest = {"format": FORMAT, "version": VERSION, "kind": kind, "created_at": now(), **meta,
                "files": {n: {"size": len(d), "sha256": sha256(d)} for n, d in sorted(files.items())}}
    buf = io.BytesIO()
    # mtime=0 and fixed owners: the archive leaks nothing about the host beyond its contents.
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tf:
        for name, data in [("manifest.json", json.dumps(manifest, indent=1).encode()), *sorted(files.items())]:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, 0
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(blob: bytes, max_bytes: int = DEFAULT_MAX_BYTES) -> Backup:
    """Read and check an archive in memory: only regular files at allowed paths, within limits,
    each listed in the manifest with a matching checksum, and nothing the manifest doesn't list."""
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for m in tf:
                if len(files) >= MAX_FILES:
                    raise BackupError("The backup has too many files")
                if not m.isreg():
                    raise BackupError(f"{m.name}: only regular files are allowed (no links or devices)")
                if not ALLOWED_PATHS.fullmatch(m.name):
                    raise BackupError(f"{m.name[:100]}: not something a backup contains")
                if m.name in files:
                    raise BackupError(f"{m.name}: listed twice")
                if m.size > MAX_FILE_BYTES:
                    raise BackupError(f"{m.name}: too large")
                total += m.size
                if total > max_bytes:
                    raise BackupError("The backup unpacks to more than the size limit (BACKUP_MAX_MB)")
                fh = tf.extractfile(m)
                data = fh.read(m.size + 1) if fh else b""
                if len(data) != m.size:
                    raise BackupError(f"{m.name}: truncated")
                files[m.name] = data
    except (tarfile.TarError, EOFError, OSError, gzip.BadGzipFile) as e:
        raise BackupError(f"The backup archive is damaged: {e}") from None
    if "manifest.json" not in files:
        raise BackupError("The backup has no manifest")
    try:
        manifest = json.loads(files.pop("manifest.json"))
    except ValueError:
        raise BackupError("The backup's manifest is not valid JSON") from None
    check_manifest(manifest, files)
    return Backup(manifest, files)


def check_manifest(manifest, files: dict[str, bytes]) -> None:
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("This isn't a vm-portal backup")
    version = manifest.get("version")
    if not isinstance(version, int) or version < 1:
        raise BackupError("The backup has no valid format version")
    if version > VERSION:
        raise BackupError(f"This backup was made by a newer portal (format {version}); update this portal first")
    if manifest.get("kind") not in KINDS:
        raise BackupError("Unknown backup kind")
    listed = manifest.get("files")
    if not isinstance(listed, dict) or set(listed) != set(files):
        raise BackupError("The backup's contents don't match its manifest")
    for name, data in files.items():
        entry = listed[name]
        if not isinstance(entry, dict) or entry.get("size") != len(data) or entry.get("sha256") != sha256(data):
            raise BackupError(f"{name}: checksum mismatch")


def read(data: bytes, secret: str, max_bytes: int = DEFAULT_MAX_BYTES) -> Backup:
    if len(data) > max_bytes:
        raise BackupError("The file is larger than the size limit (BACKUP_MAX_MB)")
    return unpack(decrypt(data, secret), max_bytes)


def write(kind: str, files: dict[str, bytes], *, passphrase: str | None = None,
          recipients: list[str] | None = None, **meta) -> bytes:
    return encrypt(pack(kind, files, **meta), passphrase=passphrase, recipients=recipients)
