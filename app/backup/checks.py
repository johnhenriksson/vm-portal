"""Validation of what comes out of a backup, before any of it is used.

A backup is only as trustworthy as whoever could write it: anyone with the passphrase,
or anyone at all for a file encrypted to a public key. So nothing is taken on trust.
Terraform state could otherwise point the portal at any VM in vCenter (and a later
"destroy" would delete it), and database rows could carry anything the UI would never
accept. Everything here is checked against the same rules the UI applies.
"""
import ipaddress
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import CONTROL_RE, LINUX_USER_RE
from ..platforms import PLATFORMS, for_state
from .archive import BackupError

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")   # provision.NAME_RE
IMAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,47}$")    # images.ID_RE
MAX_STATE = 16 * 1024 * 1024

# Variables every platform's module takes for the VM itself (terraform/vm/variables.tf). The
# platform adds its placement variables; its connection settings are dropped, because they
# always come from the current settings. What the module may leave in state, the providers
# and the form of a VM id come from the platform too (platforms/base.py).
TFVARS_STRINGS = {"template", "name", "metadata", "userdata", "annotation"}
TFVARS_INTS = {"cpus": (1, 32), "memory_mb": (512, 262144), "disk_gb": (1, 65536)}


def check_state(name: str, data: bytes) -> str:
    """Validate a VM's OpenTofu state; returns the VM's id on its platform."""
    return check_state_platform(name, data)[1]


def check_state_platform(name: str, data: bytes):
    """Validate a VM's OpenTofu state; returns (the platform that wrote it, the VM's id there)."""
    if len(data) > MAX_STATE:
        raise BackupError(f"{name}: the state file is too large")
    try:
        state = json.loads(data)
    except ValueError:
        raise BackupError(f"{name}: the state file is not valid JSON") from None
    if not isinstance(state, dict) or state.get("version") != 4 or not isinstance(state.get("resources"), list):
        raise BackupError(f"{name}: not an OpenTofu/Terraform state file (version 4)")
    pf = for_state(state)
    if pf is None:
        raise BackupError(f"{name}: the state uses an unexpected provider")
    providers = {f'provider["{a}"]' for a in (pf.TF_PROVIDER, pf.TF_LEGACY_PROVIDER) if a}
    moid = None
    for r in state["resources"]:
        if not isinstance(r, dict) or "module" in r:
            raise BackupError(f"{name}: the state has resources the portal didn't create")
        key, mode = (r.get("type"), r.get("name")), r.get("mode")
        if r.get("provider") not in providers:
            raise BackupError(f"{name}: the state uses an unexpected provider")
        instances = r.get("instances")
        if not isinstance(instances, list) or len(instances) > 1:
            raise BackupError(f"{name}: unexpected instances in the state")
        if mode == "data" and key in pf.STATE_DATA:
            continue
        if mode != "managed" or key != pf.TF_RESOURCE or moid is not None:
            raise BackupError(f"{name}: the state has resources the portal didn't create ({r.get('type')}.{r.get('name')})")
        attrs = (instances[0] if instances else {}).get("attributes") if instances else None
        if not isinstance(attrs, dict):
            raise BackupError(f"{name}: the state has no VM in it")
        if attrs.get("name") != name:
            raise BackupError(f"{name}: the state describes a VM named {str(attrs.get('name'))[:40]!r}")
        moid = pf.state_vm_id({"resources": [r]})
        if not isinstance(moid, str) or not pf.VM_ID_RE.match(moid):
            raise BackupError(f"{name}: the state has no valid {pf.API} id for the VM")
    if moid is None:
        raise BackupError(f"{name}: the state has no VM in it")
    return pf, moid


def check_tfvars(name: str, data: bytes, pf=PLATFORMS["vsphere"]) -> dict:
    """Validate a VM's variables (for the platform its state belongs to); returns them
    without connection settings."""
    try:
        tfvars = json.loads(data)
    except ValueError:
        raise BackupError(f"{name}: the variables file is not valid JSON") from None
    if not isinstance(tfvars, dict):
        raise BackupError(f"{name}: the variables file is not an object")
    out = {}
    for k, v in tfvars.items():
        if k in pf.TF_CONNECTION_VARS or k in pf.TF_SECRET_VARS:
            continue
        if k in TFVARS_STRINGS or k in pf.TF_PLACEMENT_VARS:
            ok = isinstance(v, str) and len(v) <= 256 * 1024
        elif k in TFVARS_INTS:
            lo, hi = TFVARS_INTS[k]
            ok = isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi
        elif k in pf.TF_EXTRA_VARS:
            want = pf.TF_EXTRA_VARS[k]
            ok = isinstance(v, want) and not isinstance(v, bool) and (
                len(v) <= 256 if want is str else 0 <= v <= 999_999_999)
        elif k == "template_facts":
            ok = (isinstance(v, dict) and set(v) <= set(pf.TF_FACTS)
                  and all(isinstance(x, str) and len(x) <= 256 and not CONTROL_RE.search(x) for x in v.values()))
        else:
            ok = False
        if not ok:
            raise BackupError(f"{name}: unexpected variable {k[:40]!r} in the variables file")
        if k not in ("metadata", "userdata") and isinstance(v, str) and CONTROL_RE.search(v):
            raise BackupError(f"{name}: variable {k} contains control characters")
        out[k] = v
    if out.get("name") != name:
        raise BackupError(f"{name}: the variables file is for another VM")
    return out


# --- database rows ---------------------------------------------------------------------

def _text(v: str | None, limit: int = 255) -> str | None:
    if v is not None and (CONTROL_RE.search(v) or len(v) > limit):
        raise ValueError("invalid text")
    return v


def _timestamp(v: str | None) -> str | None:
    if v is not None:
        datetime.fromisoformat(v)
    return v


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class VmRow(_Row):
    id: int | None = None
    name: str
    status: Literal["provisioning", "ready", "failed", "destroying", "destroyed", "forgotten"]
    template: str = Field(max_length=255)
    cpus: int = Field(ge=1, le=32)
    memory_mb: int = Field(ge=512, le=262144)
    disk_gb: int = Field(ge=1, le=65536)
    ip_mode: Literal["dhcp", "static"]
    ip: str | None = None
    has_compose: int = Field(ge=0, le=1)
    created_at: str
    last_job_id: int | None = None
    owner_id: int | None = None
    console_login: int = Field(0, ge=0, le=1)
    vm_user: str | None = None
    image_id: str | None = None
    notes: str = Field("", max_length=2000)
    autostart: int | None = Field(None, ge=0, le=1)   # None: not managed (and backups from before it)
    backend_id: int | None = Field(None, ge=1)        # full backups; None: from before backends existed
    seed_pending: int = Field(0, ge=0, le=1)
    # Whether first-boot setup worked (app/firstboot.py); None in backups from before it.
    firstboot_state: Literal["waiting", "done", "failed", "unknown"] | None = None
    firstboot_detail: str | None = Field(None, max_length=32000)
    firstboot_since: str | None = None
    owner_username: str | None = None   # only in VM backups: ids mean nothing on another portal
    backend_name: str | None = Field(None, max_length=64)    # only in VM backups, like the owner
    backend_type: str | None = Field(None, max_length=32)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not NAME_RE.match(v):
            raise ValueError("invalid VM name")
        return v

    @field_validator("template")
    @classmethod
    def _template(cls, v):
        return _text(v)

    @field_validator("ip")
    @classmethod
    def _ip(cls, v):
        return str(ipaddress.IPv4Address(v)) if v is not None else v

    @field_validator("created_at", "firstboot_since")
    @classmethod
    def _ts(cls, v):
        return _timestamp(v)

    @field_validator("firstboot_detail")
    @classmethod
    def _firstboot_detail(cls, v):
        if v is not None and not isinstance(json.loads(v), dict):
            raise ValueError("invalid first-boot details")
        return v

    @field_validator("vm_user")
    @classmethod
    def _user(cls, v):
        if v is not None and (not LINUX_USER_RE.match(v) or v == "root"):
            raise ValueError("invalid login user")
        return v

    @field_validator("image_id")
    @classmethod
    def _image(cls, v):
        if v is not None and not IMAGE_ID_RE.match(v):
            raise ValueError("invalid image id")
        return v

    @field_validator("notes")
    @classmethod
    def _notes(cls, v):
        if any(ord(c) < 32 and c not in "\n\t" for c in v):
            raise ValueError("control characters in notes")
        return v

    @field_validator("owner_username", "backend_name")
    @classmethod
    def _owner(cls, v):
        return _text(v, 64)

    @field_validator("backend_type")
    @classmethod
    def _backend_type(cls, v):
        if v is not None and v not in PLATFORMS:
            raise ValueError("unknown backend type")
        return v


def rows(model: type[BaseModel], data, what: str) -> list:
    if not isinstance(data, list):
        raise BackupError(f"{what}: expected a list of rows")
    out = []
    for i, r in enumerate(data):
        try:
            out.append(model.model_validate(r))
        except ValidationError as e:
            err = e.errors()[0]
            raise BackupError(f"{what}, row {i + 1}: {'.'.join(map(str, err['loc']))}: {err['msg']}") from None
    return out


SCRYPT_RE = re.compile(r"^scrypt\$[0-9]{1,7}\$[0-9]{1,2}\$[0-9]{1,2}\$[A-Za-z0-9+/=]{4,100}\$[A-Za-z0-9+/=]{4,100}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class UserRow(_Row):
    id: int = Field(ge=1)
    username: str
    password_hash: str
    role: Literal["admin", "user"]
    disabled: int = Field(ge=0, le=1)
    session_version: int = Field(ge=0)
    created_at: str
    last_login_at: str | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, v):
        from .. import users
        if users.validate_username(v) != v:
            raise ValueError("invalid username")
        return v

    @field_validator("password_hash")
    @classmethod
    def _hash(cls, v):
        if not SCRYPT_RE.match(v):
            raise ValueError("not a password hash this portal makes")
        return v

    @field_validator("created_at", "last_login_at")
    @classmethod
    def _ts(cls, v):
        return _timestamp(v)


class JobRow(_Row):
    id: int = Field(ge=1)
    kind: str
    target: str = Field(max_length=255)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    user_id: int | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if not KIND_RE.match(v):
            raise ValueError("invalid job kind")
        return v

    @field_validator("target")
    @classmethod
    def _target(cls, v):
        return _text(v)

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def _ts(cls, v):
        return _timestamp(v)


class AuditRow(_Row):
    id: int = Field(ge=1)
    ts: str
    username: str | None = Field(None, max_length=64)
    ip: str | None = Field(None, max_length=64)
    action: str
    detail: str | None = Field(None, max_length=1000)

    @field_validator("action")
    @classmethod
    def _action(cls, v):
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", v):
            raise ValueError("invalid action")
        return v

    @field_validator("ts")
    @classmethod
    def _ts(cls, v):
        return _timestamp(v)


class ImageSettingRow(_Row):
    image_id: str
    data: str = Field(max_length=64 * 1024)
    updated_at: str
    updated_by: str | None = Field(None, max_length=64)

    @field_validator("image_id")
    @classmethod
    def _image(cls, v):
        if not IMAGE_ID_RE.match(v):
            raise ValueError("invalid image id")
        return v

    @field_validator("data")
    @classmethod
    def _data(cls, v):
        if not isinstance(json.loads(v), dict):
            raise ValueError("image settings must be an object")
        return v


class BackendRow(_Row):
    """A backend in a full backup. Its settings are checked like the Settings page checks
    them (backends.validate); its secrets come from secrets.json."""
    id: int = Field(ge=1)
    name: str
    type: str
    enabled: int = Field(ge=0, le=1)
    admin_only: int = Field(ge=0, le=1)
    from_env: int = Field(ge=0, le=1)
    config: dict

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        from .. import backends
        if backends.validate_name(v) != v:
            raise ValueError("invalid backend name")
        return v

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        if v not in PLATFORMS:
            raise ValueError("unknown backend type")
        return v
