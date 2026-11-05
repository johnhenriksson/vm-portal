"""Guest drivers: how a VM cloned from a template gets configured.

The image manifest's `guest:` names the driver. A driver turns a VMRequest into
the Terraform variables for the clone and says which create-VM options it
supports. Add a driver (e.g. Windows via sysprep/cloudbase-init) by
implementing GuestDriver and registering it in DRIVERS.
"""
from typing import Protocol

from .cloudinit import CloudInit


class GuestDriver(Protocol):
    name: str
    label: str
    supports_static_ip: bool
    supports_console_password: bool

    def tfvars(self, req) -> dict[str, str]:
        """Terraform variables (currently `metadata` and `userdata`) for the clone."""
        ...


DRIVERS: dict[str, GuestDriver] = {d.name: d for d in (CloudInit(),)}
DEFAULT = "cloud-init"  # for templates that don't come from an image definition


def get(name: str | None) -> GuestDriver:
    return DRIVERS[name or DEFAULT]
