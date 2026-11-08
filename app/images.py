"""Image definitions: one directory per buildable image (Packer template + image.yaml).

Two sources:
  built-in  <images_dir>/<id>/        shipped with the app, read-only
  custom    <data_dir>/images/<id>/   created in the UI (when editing is enabled)

The manifest (image.yaml) tells the portal how to build the image and how to
configure VMs cloned from it. See images/README.md for the full contract.
"""
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import db
from .config import CONTROL_RE, LINUX_USER_RE, PREFIX_RE, get_bootstrap, get_settings

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,47}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MANIFEST = "image.yaml"
# Variables the portal always sets itself; definitions can't declare parameters with these names.
RESERVED_VARS = {
    "vsphere_server", "vsphere_user", "vsphere_password", "vsphere_insecure", "vsphere_datacenter",
    "vsphere_cluster", "vsphere_datastore", "vsphere_network", "vsphere_folder", "vm_name",
    "template_prefix", "iso_url", "vm_cpu_num", "vm_mem_size", "vm_disk_size",
    "ssh_private_key_file", "ssh_authorized_keys", "http_ip",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OS(_Strict):
    family: Literal["linux", "windows"]
    distro: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    version: str = Field("", max_length=32)


class BuildDefaults(_Strict):
    cpus: int = Field(2, ge=1, le=32)
    memory_mb: int = Field(2048, ge=1024, le=65536)
    disk_mb: int = Field(25600, ge=10240, le=1048576)


class IsoSpec(_Strict):
    hint: str = Field("", max_length=200)
    match: str = Field("", max_length=200)  # glob used to preselect an ISO in the picker


class Parameter(_Strict):
    """An admin-settable Packer variable. Values are always validated against `pattern`."""
    name: str
    label: str = Field(max_length=80)
    help: str = Field("", max_length=300)
    default: str = Field("", max_length=200)
    pattern: str = Field(max_length=300)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not PARAM_RE.match(v) or v in RESERVED_VARS:
            raise ValueError(f"{v!r} is not allowed as a parameter name")
        return v

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        if not (v.startswith("^") and v.endswith("$")):
            raise ValueError("patterns must be anchored with ^ and $")
        re.compile(v)
        return v

    @model_validator(mode="after")
    def _default_matches(self):
        if self.default and not re.fullmatch(self.pattern, self.default):
            raise ValueError(f"default {self.default!r} doesn't match the pattern of {self.name}")
        return self


class Manifest(_Strict):
    name: str = Field(max_length=80)
    description: str = Field("", max_length=1000)
    os: OS
    guest: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    features: list[Literal["docker"]] = []
    template_prefix: str
    login_user: str | None = None             # fixed login user, or …
    login_user_parameter: str | None = None   # … the parameter that sets it
    # Name of the template's network interface, for static IPs. A glob ("en*") is
    # matched by netplan (Ubuntu); other renderers need the exact name (e.g. "eth0").
    network_interface: str = Field("en*", pattern=r"^[a-z][a-z0-9]{0,14}\*?$")
    build: BuildDefaults = BuildDefaults()
    iso: IsoSpec = IsoSpec()
    parameters: list[Parameter] = Field([], max_length=20)

    @field_validator("template_prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        if not PREFIX_RE.match(v):
            raise ValueError("use 3-64 lowercase letters, digits, '.', '_' or '-'")
        return v

    @model_validator(mode="after")
    def _check(self):
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        if self.login_user_parameter and self.login_user_parameter not in names:
            raise ValueError(f"login_user_parameter {self.login_user_parameter!r} is not a parameter")
        if self.login_user and not LINUX_USER_RE.match(self.login_user):
            raise ValueError("login_user is not a valid user name")
        from .guests import DRIVERS
        if self.guest not in DRIVERS:
            raise ValueError(f"unknown guest driver {self.guest!r} (known: {', '.join(sorted(DRIVERS))})")
        return self


@dataclass
class ImageDef:
    id: str
    path: Path
    builtin: bool
    manifest: Manifest | None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.manifest is not None and self.error is None


def custom_dir() -> Path:
    return get_bootstrap().data_dir / "images"


def _load(path: Path, builtin: bool) -> ImageDef:
    try:
        raw = yaml.safe_load((path / MANIFEST).read_text(encoding="utf-8"))
        return ImageDef(path.name, path, builtin, Manifest.model_validate(raw))
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as e:
        return ImageDef(path.name, path, builtin, None, f"Invalid {MANIFEST}: {e}")


def catalog() -> list[ImageDef]:
    """All image definitions, built-in first. Cheap enough to call per request."""
    found: dict[str, ImageDef] = {}
    for base, builtin in ((get_bootstrap().images_dir, True), (custom_dir(), False)):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or not (d / MANIFEST).is_file() or not ID_RE.match(d.name):
                continue
            img = _load(d, builtin)
            if d.name in found:
                img.error = f"id {d.name!r} is already used by a built-in image"
                found[f"{d.name}~custom"] = img
                continue
            found[d.name] = img
    prefixes: dict[str, str] = {}
    for img in found.values():
        if img.usable:
            other = prefixes.setdefault(img.manifest.template_prefix, img.id)
            if other != img.id:
                img.error = f"template prefix {img.manifest.template_prefix!r} is already used by {other}"
    return list(found.values())


def get(image_id: str) -> ImageDef | None:
    return next((i for i in catalog() if i.id == image_id), None)


def for_template(name: str, images: list[ImageDef] | None = None) -> ImageDef | None:
    """The image a template was built from, by its name prefix (longest match wins)."""
    best = None
    for img in images if images is not None else catalog():
        if img.usable and name.startswith(img.manifest.template_prefix + "-"):
            if best is None or len(img.manifest.template_prefix) > len(best.manifest.template_prefix):
                best = img
    return best


def template_prefixes() -> list[str]:
    own = [i.manifest.template_prefix for i in catalog() if i.usable]
    return list(dict.fromkeys(own + get_settings().template_prefixes))


# --- per-image configuration ------------------------------------------------------

class ImageConfig(_Strict):
    enabled: bool = True
    iso_url: str = Field("", max_length=512)
    cpus: int = Field(2, ge=1, le=32)
    memory_mb: int = Field(2048, ge=1024, le=65536)
    disk_mb: int = Field(25600, ge=10240, le=1048576)
    keep: int = Field(3, ge=1, le=20)
    params: dict[str, str] = {}

    @field_validator("iso_url")
    @classmethod
    def _iso(cls, v: str) -> str:
        if CONTROL_RE.search(v):
            raise ValueError("must not contain control characters")
        return v.strip()


def _defaults(img: ImageDef) -> dict:
    m = img.manifest
    return {"cpus": m.build.cpus, "memory_mb": m.build.memory_mb, "disk_mb": m.build.disk_mb,
            "params": {p.name: p.default for p in m.parameters}}


def validate_config(img: ImageDef, data: dict) -> ImageConfig:
    """Merge with the manifest defaults and validate, including every parameter's pattern."""
    base = _defaults(img)
    params = {**base["params"], **{k: v for k, v in (data.get("params") or {}).items() if k in base["params"]}}
    cfg = ImageConfig.model_validate({**base, **data, "params": params})
    for p in img.manifest.parameters:
        value = cfg.params.get(p.name, "")
        if not re.fullmatch(p.pattern, value):
            raise ValueError(f"{p.label}: {value!r} is not valid")
    return cfg


def config(img: ImageDef) -> ImageConfig:
    row = db.one("SELECT data FROM image_settings WHERE image_id=?", (img.id,))
    stored = json.loads(row["data"]) if row else {}
    try:
        return validate_config(img, stored)
    except (ValidationError, ValueError) as e:
        log.error("Ignoring invalid settings for image %s: %s", img.id, e)
        return validate_config(img, {})


def save_config(img: ImageDef, cfg: ImageConfig, by: str) -> None:
    db.execute(
        "INSERT INTO image_settings (image_id, data, updated_at, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(image_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at,"
        " updated_by=excluded.updated_by",
        (img.id, cfg.model_dump_json(), db.now(), by))


def login_user(img: ImageDef | None, cfg: ImageConfig | None = None) -> str:
    """The user VMs from this image log in as (gets the SSH keys / console password)."""
    if img and img.usable:
        m = img.manifest
        if m.login_user_parameter:
            return (cfg or config(img)).params.get(m.login_user_parameter) or get_settings().vm_user
        if m.login_user:
            return m.login_user
    return get_settings().vm_user


def migrate_legacy() -> None:
    """One-time: move the pre-catalog global Packer settings onto the bundled Ubuntu image."""
    img = get("ubuntu-26.04")
    if not img or not img.usable or db.one("SELECT 1 FROM image_settings WHERE image_id=?", (img.id,)):
        return
    stored = db.load_settings()
    # Only keys that no current version writes prove a pre-catalog database. VM rows or a
    # stored vm_user don't: a current install that never saved Ubuntu image settings has
    # those too, and treating it as legacy would turn TLS verification off below.
    legacy = {k: stored[k] for k in ("packer_iso_url", "packer_cpus", "packer_memory_mb", "packer_disk_mb",
                                    "template_keep") if k in stored}
    if not legacy:
        return  # a current or fresh install: nothing to carry over
    data = {"iso_url": legacy.get("packer_iso_url", ""), "keep": legacy.get("template_keep", 3),
            # The old template hardcoded user "ubnt" and a Swedish keyboard.
            "params": {"ssh_username": stored.get("vm_user", "ubnt"), "keyboard_layout": "se"}}
    for old, new in (("packer_cpus", "cpus"), ("packer_memory_mb", "memory_mb"), ("packer_disk_mb", "disk_mb")):
        if old in legacy:
            data[new] = legacy[old]
    try:
        save_config(img, validate_config(img, data), by="migration")
    except (ValidationError, ValueError) as e:
        log.error("Could not migrate the old Packer settings: %s", e)
        return
    if "vsphere_insecure" not in stored and stored.get("vsphere_server"):
        # Pre-catalog versions skipped verification by default; keep that for their existing
        # vCenter config so the upgrade doesn't break it, and say so in the audit log.
        db.save_settings({"vsphere_insecure": True}, set(), by="migration")
        db.audit("settings.vsphere_insecure_kept",
                 "upgrade kept the old default: TLS certificate verification is off; turn it on in Settings")
    db.audit("images.migrated", f"old Packer settings moved to image {img.id}")
    log.info("Moved the old Packer settings to image %s", img.id)
