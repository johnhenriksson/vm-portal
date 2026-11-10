"""Virtualization platforms the portal can run VMs on.

    from .platforms import current as platform
    platform().templates()

Each platform is a module implementing base.Platform; everything specific to one
platform's API, OpenTofu provider and Packer builder lives in its module. Add one by
writing the module and registering it in PLATFORMS.
"""
from . import vsphere
from .base import Autostart, ConsoleTicket, Platform, VMInfo  # noqa: F401 - the package's public names

PLATFORMS: dict[str, Platform] = {p.NAME: p for p in (vsphere,)}


def current() -> Platform:
    """The platform this installation uses. There's one today; with a second, a setting
    chosen at setup picks (an installation has one platform)."""
    return PLATFORMS["vsphere"]
