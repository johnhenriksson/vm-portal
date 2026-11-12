"""Virtualization platforms the portal can run VMs on.

    from .platforms import PLATFORMS
    PLATFORMS["vsphere"].templates(backend)

Each platform is a module implementing base.Platform; everything specific to one
platform's API, OpenTofu provider and Packer builder lives in its module. A portal uses
them through its backends (backends.py): named connections, each of one platform type.
Add a platform by writing the module and registering it in PLATFORMS.
"""
from . import proxmox, vsphere
from .base import Autostart, ConsoleTicket, Platform, VMInfo  # noqa: F401 - the package's public names

PLATFORMS: dict[str, Platform] = {p.NAME: p for p in (vsphere, proxmox)}


def for_state(state: dict) -> Platform | None:
    """The platform whose OpenTofu provider wrote this state, or failing that whose VM
    resource it has (None: none of ours). Backups check the providers strictly themselves."""
    resources = [r for r in state.get("resources") or [] if isinstance(r, dict)]
    used = {r.get("provider") for r in resources}
    for p in PLATFORMS.values():
        if used & {f'provider["{a}"]' for a in (p.TF_PROVIDER, p.TF_LEGACY_PROVIDER) if a}:
            return p
    types = {r.get("type") for r in resources}
    return next((p for p in PLATFORMS.values() if p.TF_RESOURCE[0] in types), None)
