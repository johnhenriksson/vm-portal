"""What the portal needs from a virtualization platform.

A platform is a module in this package that provides everything in `Platform`:
looking at and acting on VMs through the platform's API, what the portal's OpenTofu
module for it needs and writes, the variables its Packer builds get, and how a VM in a
backup is checked before it's imported. `vsphere` and `proxmox` are the two so far.

A portal has any number of *backends* (backends.py): named connections, each of one
platform type. Every call that talks to a platform takes the backend first (`b`), with
its settings in `b.cfg`; connections and caches are kept per backend.

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


def usable_ipv4(ip) -> str | None:
    """The address if it's one people can SSH to: IPv4, not loopback or link-local. A guest
    agent's "primary" address can be IPv6 link-local (fe80::…), which needs an interface
    name and only works on the same network segment."""
    import ipaddress
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    return str(a) if a.version == 4 and not (a.is_loopback or a.is_link_local or a.is_unspecified) else None


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
    vm_id: str | None = None         # the platform's id for it, where cheap to know
    host: str | None = None          # the host (node) it's on now, where the list says
    booted: datetime | None = None   # when the guest last (re)booted, where the platform says
    disk_gb: int | None = None       # a template's boot disk (a clone's can't be smaller); None: unknown

    @property
    def age_days(self) -> int | None:
        if not self.created:
            return None
        return (datetime.now(timezone.utc) - self.created).days


@dataclass
class Autostart:
    """Whether a VM starts when its host does. On vSphere that's an entry in the host's
    startup list, which only acts when the host itself has autostart turned on; on Proxmox
    it's the VM's own "start at boot" option (the host always honours it)."""
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
    # Proxmox: headers for the WebSocket request, and the VNC password the portal answers
    # the server's challenge with (the browser gets a console without one).
    headers: dict = field(default_factory=dict)
    vnc_password: str | None = None
    # Without a thumbprint: verify the host's certificate against the system's CAs (when
    # the platform allows that) instead of refusing.
    ca_verified: bool = False


class Platform(Protocol):
    NAME: str          # the backend type, in settings and backups, e.g. "vsphere"
    LABEL: str         # for people, e.g. "VMware vSphere"
    SUMMARY: str       # one line about what a backend of this kind is, for the "Add a backend" choice
    API: str           # what people call the place its VMs are seen in, e.g. "vCenter"
    CAN_PROVISION: bool
    GUEST_POWER_NEEDS_TOOLS: bool           # guest reboot/shutdown only work while the guest tools answer
    GUEST_TOOLS: str                        # what reports a guest's addresses, e.g. "VMware Tools"
    AUTOSTART_HINT: str                     # where a VM's autostart lives there, as a sentence
    HOST_AUTOSTART_HELP: str                # how an admin turns on a host's autostart, as a sentence
    SERVER_FIELD: str                       # the setting that names the server
    SECRET_FIELD: str                       # the secret that must never go to another server
    CONNECTION: tuple[str, ...]             # settings (config.BackendFields) to connect with
    PLACEMENT: tuple[str, ...]              # settings saying where new VMs go
    REQUIRED: tuple[str, ...]               # settings that must be set to use the backend
    PACKER_SOURCE: str                      # Packer source type, e.g. "vsphere-iso"
    LOCATION_LABEL: str                     # what locations() are called, e.g. "Node"; "" if it has none

    # --- the platform's API ------------------------------------------------------------
    def close(self, b=None) -> None:
        """Drop one backend's connection and caches (every backend's without b)."""

    def all_vms(self, b, fresh: bool = False) -> list[VMInfo]:
        """Every VM and template; briefly cached unless fresh."""

    def vms_by_name(self, b, fresh: bool = False) -> dict[str, VMInfo]:
        """VMs (not templates) by name."""

    def templates(self, b) -> list[VMInfo]:
        """The portal's templates (names starting with an image's template prefix), newest first."""

    def used_ips(self, b) -> set[str]:
        """Every IPv4 address the platform knows a VM is using."""

    def name_exists(self, b, name: str) -> bool:
        """Whether any VM or template has this name (not cached)."""

    def power(self, b, name: str, action: str, vm_id: str | None = None) -> None:
        """action: on, off (hard), shutdown (guest), reboot (guest)."""

    def delete_template(self, b, name: str) -> None:
        """Delete a template; refuses anything that isn't one."""

    def list_isos(self, b) -> list[str]:
        """Installer ISOs a build can use, in the form the platform's Packer builder takes."""

    def iso_name(self, iso: str) -> str:
        """The file name in an ISO reference from list_isos."""

    def inventory(self, *args, **kwargs) -> dict:
        """What the backend's settings page offers to choose from (its own connection args)."""

    def console_ticket(self, b, name: str, vm_id: str | None = None) -> ConsoleTicket:
        """A short-lived ticket for the VM's console."""

    def autostart(self, b, name: str, vm_id: str | None = None) -> Autostart: ...

    def set_autostart(self, b, name: str, vm_id: str | None, enabled: bool) -> Autostart: ...

    # --- OpenTofu ----------------------------------------------------------------------
    # Every platform's module takes the same variables for the VM itself: name, template,
    # cpus, memory_mb, disk_gb, metadata, userdata, annotation. The platform adds where the
    # VM goes (placement) and how to connect, and reads its own state.
    TF_RESOURCE: tuple[str, str]            # (type, name) of the VM in the module
    TF_PROVIDER: str                        # provider address in OpenTofu state
    TF_LEGACY_PROVIDER: str | None          # the same provider in state written by Terraform
    TF_CONNECTION_VARS: tuple[str, ...]     # never kept in a VM's tfvars: they come from the backend
    TF_SECRET_VARS: tuple[str, ...]         # connection secrets; never in tfvars or backups
    TF_PLACEMENT_VARS: tuple[str, ...]      # the keys of tf_placement()
    TF_FACTS: tuple[str, ...]               # the keys of the template_facts variable
    TF_EXTRA_VARS: dict[str, type]          # more variables the platform's create writes (type checked)
    TF_RESIZABLE: set[tuple]                # attribute paths a resize may change
    TF_RECOMPUTED: set[str]                 # attributes the provider recomputes on any update
    TF_DEFAULTED: dict[str, str]            # attributes that may go from unset to this default

    def tf_module_dir(self) -> Path:
        """The portal's OpenTofu module for this platform."""

    def tf_env(self, b) -> dict[str, str]:
        """TF_VAR_* with the backend's connection settings (never in a tfvars file)."""

    def locations(self, b) -> list[str]:
        """Where within the backend a user may put a new VM (Proxmox: the backend's nodes
        that are online), the default first. Empty or one entry: there's nothing to choose."""

    def tf_placement(self, b, location: str | None = None) -> dict[str, str]:
        """Variables saying where a new VM goes, from the backend's settings (and the
        location chosen for it, one of locations())."""

    async def before_change(self, b, d: Path, log) -> None:
        """Run before planning against an existing VM (resize, destroy): bring its state
        and variables in line with what moved on the platform since."""

    def tf_template_facts(self, values: dict | None) -> dict | None:
        """From the VM's state: what later plans need to know about its template, so the
        template itself can be deleted. None if the state doesn't have it."""

    def tf_size(self, values: dict | None) -> dict | None:
        """From the VM's state: {"cpus", "memory_mb", "disk_gb"} as the VM really is."""

    def state_vm_id(self, state: dict) -> str | None:
        """The VM's platform id in a parsed OpenTofu state file."""

    async def before_create(self, b, req, d: Path, log) -> dict:
        """Run before the first apply; returns more variables for the VM's tfvars."""

    async def after_create(self, b, req, vm_id: str | None, log) -> None:
        """Run after the apply succeeded (e.g. clean up first-boot data)."""

    async def after_destroy(self, b, name: str, vm_id: str | None, log) -> None:
        """Run after the VM was destroyed (e.g. remove what before_create left)."""

    # --- Packer ------------------------------------------------------------------------
    def packer_vars(self, b) -> dict[str, str]:
        """PKR_VAR_* (and other environment) with connection and placement, for a build."""

    def api_host(self, b) -> str:
        """The API server's host name, to find the address build VMs reach the portal at."""

    # --- backups -----------------------------------------------------------------------
    VM_ID_RE: object                        # compiled pattern a VM id must match
    STATE_DATA: set[tuple[str, str]]        # data sources the module leaves in state

    def import_problems(self, b, vm_id: str, name: str, tfvars: dict) -> list[str]:
        """Reasons not to import a VM from a backup: the id must belong to that very VM,
        inside this backend's part of the platform, created by vm-portal. Empty = fine."""

    # Optional, found with getattr (a platform without it isn't asked):
    #   clean_seed(b, name, vm_id, wait) -> bool      replace a first-boot seed without secrets
    #   first_boot(b, name, vm_id) -> firstboot.Result | None
    #       cloud-init's outcome on the VM, None while there is none yet; raises
    #       firstboot.Unreadable if the backend can't read it at all (a permission)
