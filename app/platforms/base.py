"""What the portal needs from a virtualization platform.

A platform is a module in this package that provides everything in `Platform`:
looking at and acting on VMs through the platform's API, what the portal's OpenTofu
module for it needs and writes, the variables its Packer builds get, and how a VM in a
backup is checked before it's imported. `vsphere` is the only one so far.

Rules a platform keeps, whatever its API looks like:
  * Calls are blocking; callers run them in a thread (asyncio.to_thread).
  * A VM the portal manages is acted on by its platform id (from its OpenTofu state),
    never by name alone: another VM may have, or later get, the same name.
  * Nothing here trusts a backup: see `import_problems` and the STATE_* values.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

# Starts the notes of every VM the portal creates (the module's `annotation` variable).
# Importing a VM from a backup requires it: the portal only takes over VMs it made.
CREATED_BY = "Created by vm-portal"


@dataclass
class VMInfo:
    """A VM or template as the platform reports it."""
    name: str
    is_template: bool
    power_state: str = ""            # "poweredOn", "poweredOff" or "suspended", on every platform
    ip: str | None = None
    ips: list[str] = field(default_factory=list)
    created: datetime | None = None
    notes: str = ""
    tools_running: bool = False      # the guest agent answers (so guest shutdown and IPs work)

    @property
    def age_days(self) -> int | None:
        if not self.created:
            return None
        return (datetime.now(timezone.utc) - self.created).days


@dataclass
class Autostart:
    """Whether a VM starts when its host does. On vSphere that's an entry in the host's
    startup list, which only acts when the host itself has autostart turned on."""
    enabled: bool        # the VM is set to start
    host: str            # the host the VM is on now
    host_enabled: bool   # the host starts VMs at all


@dataclass
class ConsoleTicket:
    """Where the portal connects to relay a VM's console to the browser."""
    host: str
    port: int
    path: str          # contains the one-time ticket; never sent to the browser
    thumbprint: str    # the host's certificate thumbprint, as the platform's API reports it


class Platform(Protocol):
    NAME: str          # in settings and backups, e.g. "vsphere"
    LABEL: str         # for people, e.g. "VMware vSphere"

    # --- the platform's API ------------------------------------------------------------
    def close(self) -> None:
        """Drop the connection and caches (settings changed, or the portal is stopping)."""

    def all_vms(self, fresh: bool = False) -> list[VMInfo]:
        """Every VM and template; briefly cached unless fresh."""

    def vms_by_name(self, fresh: bool = False) -> dict[str, VMInfo]:
        """VMs (not templates) by name."""

    def templates(self) -> list[VMInfo]:
        """The portal's templates (names starting with an image's template prefix), newest first."""

    def used_ips(self) -> set[str]:
        """Every IPv4 address the platform knows a VM is using."""

    def name_exists(self, name: str) -> bool:
        """Whether any VM or template has this name (not cached)."""

    def power(self, name: str, action: str, vm_id: str | None = None) -> None:
        """action: on, off (hard), shutdown (guest), reboot (guest)."""

    def delete_template(self, name: str) -> None:
        """Delete a template; refuses anything that isn't one."""

    def list_isos(self) -> list[str]:
        """Installer ISOs a build can use, in the form the platform's Packer builder takes."""

    def console_ticket(self, name: str, vm_id: str | None = None) -> ConsoleTicket:
        """A short-lived ticket for the VM's console."""

    def autostart(self, name: str, vm_id: str | None = None) -> Autostart: ...

    def set_autostart(self, name: str, vm_id: str | None, enabled: bool) -> Autostart: ...

    # --- OpenTofu ----------------------------------------------------------------------
    # Every platform's module (see terraform/vm) takes the same variables for the VM itself:
    # name, template, cpus, memory_mb, disk_gb, metadata, userdata, annotation. The platform
    # adds where the VM goes (placement) and how to connect, and reads its own state.
    TF_RESOURCE: tuple[str, str]            # (type, name) of the VM in the module
    TF_PROVIDER: str                        # provider address in OpenTofu state
    TF_LEGACY_PROVIDER: str | None          # the same provider in state written by Terraform
    TF_CONNECTION_VARS: tuple[str, ...]     # never kept in a VM's tfvars: they come from settings
    TF_SECRET_VARS: tuple[str, ...]         # connection secrets; never in tfvars or backups
    TF_PLACEMENT_VARS: tuple[str, ...]      # the keys of tf_placement()
    TF_FACTS: tuple[str, ...]               # the keys of the template_facts variable
    TF_RESIZABLE: set[tuple]                # attribute paths a resize may change
    TF_RECOMPUTED: set[str]                 # attributes the provider recomputes on any update
    TF_DEFAULTED: dict[str, str]            # attributes that may go from unset to this default

    def tf_module_dir(self) -> Path:
        """The portal's OpenTofu module for this platform."""

    def tf_env(self) -> dict[str, str]:
        """TF_VAR_* with the connection settings (they never go into a tfvars file)."""

    def tf_placement(self) -> dict[str, str]:
        """Variables saying where a new VM goes, from the settings."""

    def tf_template_facts(self, values: dict | None) -> dict | None:
        """From the VM's state: what later plans need to know about its template, so the
        template itself can be deleted. None if the state doesn't have it."""

    def tf_size(self, values: dict | None) -> dict | None:
        """From the VM's state: {"cpus", "memory_mb", "disk_gb"} as the VM really is."""

    def state_vm_id(self, state: dict) -> str | None:
        """The VM's platform id in a parsed OpenTofu state file."""

    # --- Packer ------------------------------------------------------------------------
    def packer_vars(self) -> dict[str, str]:
        """PKR_VAR_* with connection and placement, for a template build."""

    def api_host(self) -> str:
        """The API server's host name, to find the address build VMs reach the portal at."""

    # --- backups -----------------------------------------------------------------------
    VM_ID_RE: object                        # compiled pattern a VM id must match
    STATE_DATA: set[tuple[str, str]]        # data sources the module leaves in state

    def import_problems(self, vm_id: str, name: str, tfvars: dict) -> list[str]:
        """Reasons not to import a VM from a backup: the id must belong to that very VM,
        inside this portal's part of the platform, created by vm-portal. Empty = fine."""
