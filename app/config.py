"""Settings.

Bootstrap values (data dir, keys, cookie flags, mount paths) come from the
environment only. Everything else is edited on the Settings page and stored in
SQLite, with secrets encrypted. Environment variables (and .env) still take
precedence over stored values, so config-as-code deployments keep working; the
UI shows such fields as read-only.
"""
import ipaddress
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import (BaseSettings, DotEnvSettingsSource, EnvSettingsSource, NoDecode,
                               SettingsConfigDict)

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = ".env"
log = logging.getLogger(__name__)

SSH_KEY_RE = re.compile(r"^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-\S+) [A-Za-z0-9+/=]+( [^\n\r]*)?$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?$")
PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
LINUX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Proxmox names end up in API paths, so they are held to what Proxmox itself allows.
PVE_TOKEN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,32}![A-Za-z0-9._-]{1,64}$")
PVE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


class Bootstrap(BaseSettings):
    """Needed before the database is opened; environment only."""
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore", env_ignore_empty=True)

    data_dir: Path = Path("/data")
    # Session signing key and secret-encryption key. Generated into data_dir when
    # unset. Setting ENCRYPTION_KEY keeps the key out of the data volume (and its backups).
    secret_key: str = ""
    encryption_key: str = ""
    # Only used to create the first admin when there are no users yet.
    admin_username: str = "admin"
    admin_password: str = ""

    # Built-in image definitions (read-only, shipped with the app). Custom ones live in data_dir/images.
    images_dir: Path = APP_DIR.parent / "images"
    # Private key Packer logs in with. Unset: the portal generates one in data_dir.
    packer_ssh_key: Path | None = None
    terraform_module_dir: Path = APP_DIR.parent / "terraform" / "vm"
    # Scheduled backups are written here (mount another disk or a network share).
    backup_dir: Path = Path("/backups")
    # Largest backup the portal reads or writes (uploads, restores), in MB.
    backup_max_mb: int = Field(512, ge=16, le=8192)

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def vms_dir(self) -> Path:
        return self.data_dir / "vms"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "portal.db"


def _no_control(v: str) -> str:
    if CONTROL_RE.search(v):
        raise ValueError("must not contain control characters or newlines")
    return v.strip()


def _ipv4(v: str) -> str:
    try:
        return str(ipaddress.IPv4Address(v))
    except ValueError:
        raise ValueError(f"{v!r} is not a valid IPv4 address") from None


class BackendFields(BaseModel):
    """What one backend (a vCenter or a Proxmox VE cluster) is configured with: its
    connection, where new VMs go, and its static IP pool. A backend only uses the fields of
    its own type (see platforms/*.CONNECTION, PLACEMENT) and the pool. Stored per backend
    (backends.py); the same names in the environment (VSPHERE_SERVER, IP_POOL_START, ...)
    configure the backend the environment describes."""

    # vCenter
    vsphere_server: str = ""
    vsphere_user: str = Field("", max_length=255)
    vsphere_password: str = Field("", max_length=1024)
    vsphere_insecure: bool = False
    vsphere_datacenter: str = Field("", max_length=255)
    vsphere_cluster: str = Field("", max_length=255)
    vsphere_datastore: str = Field("", max_length=255)
    vsphere_network: str = Field("", max_length=255)
    vsphere_folder: str = Field("", max_length=255)

    # Proxmox VE. An API token ("user@realm!name" and its secret), never a password. The
    # certificate is checked against the system's CAs, or against this SHA-256 fingerprint
    # (Proxmox's own certificates are signed by the cluster's CA, which nothing else trusts).
    proxmox_server: str = ""
    proxmox_token_id: str = ""
    proxmox_token_secret: str = Field("", max_length=1024)
    proxmox_fingerprint: str = ""
    proxmox_node: str = ""
    proxmox_storage: str = ""        # VM disks
    proxmox_iso_storage: str = ""    # installer ISOs
    proxmox_bridge: str = ""
    proxmox_pool: str = ""           # optional: the resource pool the portal's VMs go in
    proxmox_vlan: str = ""           # optional: VLAN tag (1-4094) of the VMs' network cards on the bridge
    # More nodes of the cluster users may put a VM on, beside proxmox_node (the default,
    # and where templates are built). The storages and the bridge must exist on each.
    proxmox_nodes: Annotated[list[str], NoDecode] = []

    # Static IP pool for the free-IP finder
    ip_pool_start: str = ""
    ip_pool_end: str = ""
    ip_prefix: int = Field(24, ge=8, le=30)
    ip_gateway: str = ""
    ip_dns: Annotated[list[str], NoDecode] = []

    @field_validator("ip_dns", mode="before")
    @classmethod
    def _split_dns(cls, v):
        return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else v

    @field_validator("proxmox_nodes", mode="before")
    @classmethod
    def _split_nodes(cls, v):
        return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else v

    @field_validator("proxmox_nodes")
    @classmethod
    def _node_names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not PVE_NAME_RE.match(name):
                raise ValueError(f"{name!r} is not a node name: use letters, digits, '.', '_' and '-' only")
        return list(dict.fromkeys(v))

    @field_validator("proxmox_vlan", mode="before")
    @classmethod
    def _vlan(cls, v) -> str:
        v = str(v if v is not None else "").strip()
        if v and not (v.isdecimal() and 1 <= int(v) <= 4094):
            raise ValueError("a VLAN tag is a number from 1 to 4094 (empty: untagged)")
        return str(int(v)) if v else ""

    @field_validator("proxmox_token_id")
    @classmethod
    def _token_id(cls, v: str) -> str:
        v = v.strip()
        if v and not PVE_TOKEN_ID_RE.match(v):
            raise ValueError("must look like user@realm!tokenname (an API token, not a user)")
        return v

    @field_validator("proxmox_token_secret")
    @classmethod
    def _token_secret(cls, v: str) -> str:
        v = v.strip()
        if CONTROL_RE.search(v) or " " in v:
            raise ValueError("must not contain spaces or control characters")
        return v

    @field_validator("proxmox_fingerprint")
    @classmethod
    def _fingerprint(cls, v: str) -> str:
        """SHA-256 of the certificate, as Proxmox shows it (AA:BB:…); stored that way."""
        digits = re.sub(r"[\s:]", "", v).upper()
        if not digits:
            return ""
        if not re.fullmatch(r"[0-9A-F]{64}", digits):
            raise ValueError("must be a SHA-256 fingerprint: 64 hex digits, with or without colons")
        return ":".join(digits[i:i + 2] for i in range(0, 64, 2))

    @field_validator("proxmox_node", "proxmox_storage", "proxmox_iso_storage", "proxmox_bridge", "proxmox_pool")
    @classmethod
    def _pve_name(cls, v: str) -> str:
        v = v.strip()
        if v and not PVE_NAME_RE.match(v):
            raise ValueError("use letters, digits, '.', '_' and '-' only")
        return v

    @field_validator("vsphere_server", "proxmox_server")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if v and not HOST_RE.match(v):
            raise ValueError("must be a hostname or IP address (optionally :port)")
        return v

    @field_validator("vsphere_user", "vsphere_datacenter", "vsphere_cluster", "vsphere_datastore",
                     "vsphere_network", "vsphere_folder")
    @classmethod
    def _text(cls, v: str) -> str:
        return _no_control(v)

    @field_validator("ip_pool_start", "ip_pool_end", "ip_gateway")
    @classmethod
    def _ip(cls, v: str) -> str:
        v = v.strip()
        return _ipv4(v) if v else v

    @field_validator("ip_dns")
    @classmethod
    def _dns(cls, v: list[str]) -> list[str]:
        return [_ipv4(x) for x in v]

    @model_validator(mode="after")
    def _pool(self):
        if bool(self.ip_pool_start) != bool(self.ip_pool_end):
            raise ValueError("Set both the start and the end of the IP pool, or neither")
        if self.ip_pool_start:
            start, end = ipaddress.IPv4Address(self.ip_pool_start), ipaddress.IPv4Address(self.ip_pool_end)
            if end < start:
                raise ValueError("The IP pool ends before it starts")
            if int(end) - int(start) > 4096:
                raise ValueError("The IP pool is limited to 4096 addresses")
            if self.ip_gateway:
                net = ipaddress.IPv4Network(f"{self.ip_gateway}/{self.ip_prefix}", strict=False)
                if start not in net or end not in net:
                    raise ValueError(f"The IP pool must be inside {net}")
        return self


class Settings(Bootstrap, BackendFields):
    # Templates. Each image definition has its own prefix; these list templates
    # built some other way (e.g. by hand) as well.
    template_prefixes: Annotated[list[str], NoDecode] = []
    template_max_age_days: int = Field(30, ge=1, le=3650)

    # VMs. Login user for templates that don't come from an image definition.
    vm_user: str = "ubuntu"
    default_ssh_keys: Annotated[list[str], NoDecode] = []  # env: separated by "|"
    default_cpus: int = Field(2, ge=1, le=32)
    default_memory_mb: int = Field(4096, ge=512, le=262144)
    default_disk_gb: int = Field(40, ge=10, le=4096)
    # The free-IP finder also checks the ARP table (needs host networking).
    ip_scan_use_arp: bool = True

    # Images. Editing definitions in the web UI lets admins run arbitrary Packer code
    # inside the portal (shell-local, file(), env()...), so it is off by default and
    # turning it on needs the admin's password (see admin.py).
    allow_image_editing: bool = False
    # Address build VMs use to fetch files from Packer's HTTP server (e.g. a Debian
    # preseed). Empty: the address the portal uses to reach vCenter.
    packer_http_ip: str = ""

    # Security. Turning https_only_cookies on is only allowed over HTTPS (admin.py),
    # so it can't lock everyone out; HTTPS_ONLY_COOKIES=false in the environment
    # overrides it if it ever does.
    https_only_cookies: bool = False

    # Scheduled backups: full backups written to BACKUP_DIR, encrypted to these age public
    # keys. The portal can't read them back: the matching secret keys stay offline.
    backup_enabled: bool = False
    backup_interval_hours: int = Field(24, ge=1, le=168)
    backup_keep: int = Field(14, ge=1, le=1000)
    backup_recipients: Annotated[list[str], NoDecode] = []   # env: separated by "|" or ","
    backup_include_logs: bool = False

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # Environment beats stored values (passed as init kwargs).
        return env_settings, dotenv_settings, file_secret_settings, init_settings

    @field_validator("template_prefixes", mode="before")
    @classmethod
    def _split_commas(cls, v):
        return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else v

    @field_validator("default_ssh_keys", mode="before")
    @classmethod
    def _split_keys(cls, v):
        return [x.strip() for x in re.split(r"[|\n]", v) if x.strip()] if isinstance(v, str) else v

    @field_validator("backup_recipients", mode="before")
    @classmethod
    def _split_recipients(cls, v):
        return [x.strip() for x in re.split(r"[|,\n]", v) if x.strip()] if isinstance(v, str) else v

    @field_validator("backup_recipients")
    @classmethod
    def _recipients(cls, v: list[str]) -> list[str]:
        from .backup.archive import parse_recipient
        for r in v:
            if not r.startswith("age1") or len(r) > 100:
                raise ValueError(f"not an age public key (age1…): {r[:20]}…")
            parse_recipient(r)
        if len(v) > 20:
            raise ValueError("at most 20 public keys")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _backup(self):
        if self.backup_enabled and not self.backup_recipients:
            raise ValueError("Scheduled backups need at least one age public key to encrypt to")
        return self

    @field_validator("template_prefixes")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        for p in v:
            if not PREFIX_RE.match(p):
                raise ValueError(f"{p!r}: use 3-64 lowercase letters, digits, '.', '_' or '-'")
        return v

    @field_validator("vm_user")
    @classmethod
    def _vm_user(cls, v: str) -> str:
        if not LINUX_USER_RE.match(v) or v == "root":
            raise ValueError("must be a valid Linux user name (not root)")
        return v

    @field_validator("default_ssh_keys")
    @classmethod
    def _keys(cls, v: list[str]) -> list[str]:
        for k in v:
            if not SSH_KEY_RE.match(k):
                raise ValueError(f"not a valid SSH public key: {k[:40]}…")
        return list(dict.fromkeys(v))

    @field_validator("packer_http_ip")
    @classmethod
    def _http_ip(cls, v: str) -> str:
        v = v.strip()
        return _ipv4(v) if v else v

# Fields editable in the UI, by settings section.
SECTIONS: dict[str, list[str]] = {
    "vms": ["vm_user", "default_cpus", "default_memory_mb", "default_disk_gb", "ip_scan_use_arp"],
    "ssh-keys": ["default_ssh_keys"],
    "templates": ["template_prefixes", "template_max_age_days", "packer_http_ip", "allow_image_editing"],
    "security": ["https_only_cookies"],
    "backup": ["backup_enabled", "backup_interval_hours", "backup_keep", "backup_recipients", "backup_include_logs"],
}
EDITABLE = {f for fields in SECTIONS.values() for f in fields}
SECRET_FIELDS: set[str] = set()     # the global settings have none; backends keep theirs (backends.py)
# Per-backend fields (BackendFields). Before backends existed they were global settings, so
# older databases and backups still have them there; backends.py moves them into a backend.
BACKEND_FIELDS = set(BackendFields.model_fields)
BACKEND_SECRETS = {"vsphere_password", "proxmox_token_secret"}
POOL_FIELDS = ["ip_pool_start", "ip_pool_end", "ip_prefix", "ip_gateway", "ip_dns"]
# Never exposed to templates.
HIDDEN_FIELDS = SECRET_FIELDS | BACKEND_SECRETS | {"secret_key", "encryption_key", "admin_password"}


@lru_cache
def get_bootstrap() -> Bootstrap:
    b = Bootstrap()
    for d in (b.data_dir, b.jobs_dir, b.vms_dir):
        d.mkdir(parents=True, exist_ok=True)
    return b


def env_fields() -> set[str]:
    """Every settings field the environment or .env sets."""
    found: set[str] = set()
    for src in (EnvSettingsSource(Settings), DotEnvSettingsSource(Settings, env_file=ENV_FILE)):
        found |= {k for k, v in src().items() if v not in ("", None)}
    return found


def env_overrides() -> set[str]:
    """Editable fields currently set by the environment or .env (read-only in the UI)."""
    return env_fields() & EDITABLE


def build(stored: dict) -> Settings:
    from . import crypto
    extra = {} if get_bootstrap().secret_key else {"secret_key": crypto.session_key()}
    return Settings(**stored, **extra)


@lru_cache
def get_settings() -> Settings:
    from . import db
    get_bootstrap()
    stored = {k: v for k, v in db.load_settings().items() if k in EDITABLE}
    for _ in range(len(stored) + 1):
        try:
            return build(stored)
        except ValidationError as e:
            # A bad stored value must not brick the app: drop it and fix it in the UI.
            bad = {err["loc"][0] for err in e.errors() if err["loc"] and err["loc"][0] in stored}
            if not bad:
                raise
            log.error("Ignoring invalid stored settings %s: %s", sorted(bad), e)
            for k in bad:
                stored.pop(k)
    return build({})


def reset_caches() -> None:
    from . import crypto
    get_settings.cache_clear()
    get_bootstrap.cache_clear()
    crypto.reset()


class PublicSettings:
    """Read-only view for templates that refuses to expose secrets."""

    def __init__(self, s: Settings):
        self._s = s

    def __getattr__(self, name: str):
        if name in HIDDEN_FIELDS:
            raise AttributeError(f"{name} is not available in templates")
        return getattr(self._s, name)
