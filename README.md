# vm-portal

A self-service portal for VMware vSphere and Proxmox VE. Users create VMs from
templates in a few clicks; admins build and maintain those templates with Packer,
from the same web interface. One portal manages any number of *backends*
(vCenters and Proxmox VE clusters, side by side). Runs as a single container.

- **Create VMs**: pick an image and version, size with sliders, DHCP or a static
  IP from a pool (with a free-IP finder), SSH keys, an optional console password
  and an optional `compose.yaml` that starts on first boot
- **Manage VMs**: power on/off/reboot, **resize** (vCPUs, memory, grow the
  disk), **autostart** with the host, notes, change owner, destroy, SSH command,
  and a **browser console** (noVNC) that works without access to the hosts
- **Images**: a catalog of image definitions (Packer templates): Ubuntu 26.04
  + Docker, Debian 13 + Docker and Rocky Linux 10 are built in. Configure each
  one's installer ISO, sizes and parameters; build with a live log (and cancel a
  build that hangs); old templates are pruned automatically. With editing turned
  on, admins create new
  definitions from any existing one and edit them in a browser editor (syntax
  colouring, `packer validate` on save, history with restore).
- **Users**: admins and regular users; users only see their own VMs and jobs
- **Backends**: add vCenters and Proxmox VE clusters in the UI, each with its
  own connection, resource pickers and IP pool; users pick one when they create
  a VM (or only admins, per backend). Secrets encrypted at rest
- **Backups**: encrypted full backups (download, schedule, or command line) to
  rebuild the portal or move it to another host, and per-VM state exports;
  restore in the browser, during first-run setup or from the command line
- **Audit log** and every OpenTofu/Packer run kept with its full log

## How it works

```
browser ──► FastAPI + htmx ──► jobs (in-process; logs in /data/jobs)
   │             │                 ├─ OpenTofu: one workspace + state per VM (/data/vms/<name>)
   │             │                 └─ Packer:   builds a copy of an image definition (images/<id>)
   │             └─► each VM's backend: vCenter (pyVmomi) or Proxmox VE (its HTTP API):
   │                   inventory, templates, power, IPs, console tickets
   └── console ──wss──► portal ──wss (pinned TLS)──► ESXi host / Proxmox node
```

- **Provisioning** clones a template with OpenTofu: the module in
  `terraform/vm` on vSphere, `terraform/proxmox` on Proxmox VE. Each VM gets its
  own directory with a copy of its module and its own state. Before a resize or
  destroy the copy is refreshed and the template's details are taken from the
  state, so pruned templates don't matter. A resize applies only if OpenTofu's
  plan changes nothing but vCPUs, memory and disk size (a change made by hand on
  the platform stops it instead of being undone).
- **Guest setup** is done by a *guest driver* chosen by the image: `cloud-init`
  for Linux, delivered in VMware guestinfo on vSphere and on a small NoCloud
  seed ISO on Proxmox (see [Proxmox VE](#proxmox-ve)). The interface in
  `app/guests/` is ready for others, such as Windows.
- **Backends and platforms**: a backend is one named connection (`app/backends.py`);
  its type is a *platform* module in `app/platforms/` (`vsphere.py`,
  `proxmox.py`) behind the interface in `app/platforms/base.py`: the API calls,
  the OpenTofu provider's details, Packer's variables and how an imported VM is
  checked. Every VM records its backend, and everything the portal does to a VM
  goes through that backend. Another platform is one more module.
- **Image builds** create a new template `<prefix>-YYYYMMDD-HHMM`, so the
  template in use is never touched and a failed build costs nothing. See
  [`images/README.md`](images/README.md) for how definitions work.
- **Free IPs**: each backend has its own pool. An address in it is offered only
  if the portal hasn't assigned it, no backend reports it on a VM, it doesn't
  answer ping and isn't in the ARP table; it's checked again just before
  provisioning (`app/ipam.py`).

## Quick start

```sh
git clone <this repository> vm-portal && cd vm-portal
docker compose up -d --build
docker compose logs portal | grep "setup token"
```

Open `http://<docker-host>:8080`. On first start the portal asks for the
one-time setup token from the log (so nobody else on the network can claim the
admin account), then walks you through the first backend: a vCenter (with
dropdowns for datacenter, cluster, datastore, network and folder) or a Proxmox
VE cluster (node, storages, bridge, pool), its IP pool and the default SSH keys.
Add more backends later under **Settings → Backends**. Then open **Images →
Image definitions**, pick the installer ISO for an image and build your first
template.

Requirements:
- Linux Docker host. The container uses `network_mode: host` so the IP scanner
  sees the LAN's ARP table and Packer can reach build VMs. Debian builds also
  need the build VM to reach the Docker host on a TCP port between 8000 and 9000
  (Packer's HTTP server for the preseed file); open that range in the host's
  firewall if it has one.
- vCenter 7 or 8 and a service account, and/or Proxmox VE 8 or 9 and an API
  token (see below). Installer ISOs on a datastore or the ISO storage.
- Behind HTTPS (a reverse proxy) for anything but a lab; then turn on
  **Settings → Security → HTTPS only** from an HTTPS connection.

Everything is configured on the **Settings** page: backends, VM
defaults, SSH keys, images (including whether admins may edit image definitions
in the browser), security (HTTPS only), users and the audit log. The **System**
tab shows the few values that have to come from the environment, because they're
needed before the database can be opened. Optional environment variables (`.env`):

| Variable | Purpose |
|---|---|
| `PORT` | Listen port (default 8080) |
| `ENCRYPTION_KEY` | Key for stored secrets; keeps it out of the data volume |
| `SECRET_KEY` | Session signing key (generated if unset) |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Create the first admin without the setup token |
| `PACKER_SSH_KEY` | Use your own build key instead of the generated one |
| `FORWARDED_ALLOW_IPS` | Reverse proxies trusted for `X-Forwarded-*` (default `127.0.0.1`) |
| `BACKUP_DIR` | Where scheduled backups go (default `/backups`; mount another disk there) |
| `BACKUP_MAX_MB` | Largest backup the portal writes, uploads or restores (default 512) |
| `VSPHERE_*`, `PROXMOX_*`, `IP_POOL_*` … | Describe one vCenter and/or one Proxmox VE backend (e.g. `VSPHERE_SERVER`, `PROXMOX_TOKEN_SECRET`); the pool variables apply to them. Shown read-only on the backend's page. More backends are added in the UI |
| Any setting, e.g. `DEFAULT_CPUS` | Overrides the UI; shown read-only there. Use it to pin security switches such as `ALLOW_IMAGE_EDITING=false` or `HTTPS_ONLY_COOKIES=true` |

The data volume (`portal-data`) holds the database, keys, OpenTofu state and
logs. The OpenTofu state is how the portal knows which VM on a backend is which,
so **set up backups** before you rely on the portal.

Upgrading from a version before backends: the vCenter connection and IP pool
that were global settings become the first backend, named "vCenter", and every
existing VM is on it. Nothing else changes.

## Backup and restore

**Settings → Backup** makes encrypted backups, with a passphrase or to
[age](https://age-encryption.org) public keys:

- **Full backup**: users, settings, the backends (with their stored secrets),
  every VM's record and OpenTofu state, custom
  image definitions, jobs and the audit log. Restore it to recover a lost data
  volume or to move the portal to another host. It also works from the first-run
  page of a fresh portal, or from the command line.
- **VM backup**: chosen VMs' OpenTofu state, to recover them or move them to
  another portal. Each VM also keeps its last five states for rollback. After a
  move, **Forget** on the VM's page makes the old portal let go of it without
  touching it on its backend.
- **Scheduled backups** to `/backups`, encrypted to public keys whose secret keys
  stay offline, so the portal can write backups but never read them.

Nothing in a backup is trusted on restore. Everything is checked with the UI's
own rules, and an imported VM's id must match that very VM on its backend. The
restore builds the new data next to the old, swaps it in, and keeps the
previous data so it can be undone. See [`docs/backup.md`](docs/backup.md) for
the setup, recovery and migration steps, and what is and isn't included.

## Users and security

See [`SECURITY.md`](SECURITY.md) for the threat model and how to report issues.

- **Roles**: *admins* manage settings, users, images and all VMs. *Users* create
  VMs and only see and manage their own (other VMs return 404).
- **Passwords** are scrypt-hashed (12+ characters); failed logins are throttled
  per IP and username; changing a password, role or disabling a user ends that
  user's other sessions.
- **Secrets** (a vCenter password, a Proxmox API token secret) are encrypted with
  Fernet and never sent back to the browser. Changing a backend's server requires
  re-entering its secret; one set in the environment never goes to another server.
- **Forms** are CSRF-protected; responses carry a strict Content Security Policy
  (no inline scripts), `X-Frame-Options: DENY` and `no-store`.
- **Console**: the portal fetches a one-time ticket and relays the connection:
  on vSphere a WebMKS ticket, pinning the ESXi certificate to the thumbprint
  vCenter reports; on Proxmox a VNC ticket, with the backend's pinned (or
  trusted) certificate, and the portal answers the VNC password itself.
  Browsers never reach the hosts or see a ticket. At most 3 consoles per user,
  8 hours each; sessions end when access is revoked.
- **VMs** are SSH key-only. The optional console password is stored only as a
  bcrypt hash in cloud-init (SSH stays key-only); cloud-init data is marked
  sensitive and, after the first boot, redacted from vCenter or replaced on
  Proxmox by a seed without keys, hash or compose file.
- **Templates** contain no SSH keys: the build key the portal generates is
  removed at the end of every build.
- **Processes**: OpenTofu and Packer run with a minimal environment, never the
  portal's own secrets, and only the secret of the backend they work on. The container runs as uid 1000 with only `NET_RAW`.
- **Image editing** is off by default. Packer definitions are code (they can
  run commands in the container), so only turn it on (Settings → Images, which
  asks for your password) if you trust every admin with that. Every change is
  audited and snapshotted. `ALLOW_IMAGE_EDITING=false` in the environment keeps
  it off whatever the UI says.

## vCenter service account

Don't use `administrator@vsphere.local`. Create a user such as
`svc-vmportal@vsphere.local` with a role that has at least:

- **Datastore**: Allocate space, Browse datastore, Low level file operations, Remove file, Update virtual machine files
- **Network**: Assign network
- **Resource**: Assign virtual machine to resource pool
- **Virtual machine**: all of Configuration (Change Settings); Interaction (Power on/off, Reset,
  Configure CD media, Device connection, Console interaction, Guest operating system management by VIX API);
  Inventory (Create new, Create from existing, Remove);
  Provisioning (Clone template, Clone virtual machine, Deploy template, Mark as template, Mark as virtual machine)
- **Host**: Configuration > Virtual machine autostart configuration
- **Sessions**: Validate session
- **Profile-driven storage**: View

Assign it on the datacenter (propagate to children). Packer builds need the same
permissions plus upload access to the datastore with the ISOs.

### Autostart

New VMs start with their ESXi host (the box on the create form is ticked by
default), and the owner can turn that on or off on the VM's page. vSphere keeps
this per host, in the host's VM startup list, so:

- The host itself must have autostart turned on (the host > Configure > VM
  Startup/Shutdown). The portal never changes that, because it affects every
  VM on the host; it warns on the VM page and in the create log instead.
- vSphere HA turns host autostart off. On an HA cluster, HA restarts VMs
  instead, and this setting does nothing.
- If a VM moves to another host (vMotion, DRS), its entry may stay behind. The
  VM page shows when the host disagrees with the setting, with a button to
  apply it again.

VMs from before this setting existed, and VMs imported from older backups,
aren't managed: the page shows what the host says until someone saves a choice.

## Proxmox VE

Tested on a single node running Proxmox VE 9.2: template builds of all three
images, creating VMs with DHCP and static addresses, the console, power, resize,
autostart, destroy and backups. Clusters with several nodes and VLAN tags have
only run against a simulated API so far.

Add a Proxmox VE backend under **Settings → Backends** (or describe one with
`PROXMOX_*` in the environment): server, API token, then node, storage for VM
disks, ISO storage, bridge and optionally a pool, a VLAN tag (`PROXMOX_VLAN`)
and more nodes (`PROXMOX_NODES`, comma separated).

- **API token**, never a password: create a user (e.g. `vmportal@pve`) and a
  token under Datacenter > Permissions > API Tokens. With a privilege-separated
  token, give the role to the token itself. A role for the portal needs at least:
  - **VM**: `VM.Allocate`, `VM.Clone`, `VM.Audit`, `VM.Config.CDROM`, `VM.Config.CPU`,
    `VM.Config.Disk`, `VM.Config.HWType`, `VM.Config.Memory`, `VM.Config.Network`,
    `VM.Config.Options`, `VM.PowerMgmt`, `VM.Console`, and for the guest agent
    `VM.GuestAgent.Audit` and `VM.GuestAgent.FileRead` (Proxmox VE 9) or `VM.Monitor`
    (8). FileRead lets the portal read cloud-init's result, to show on the VM page
    whether first-boot setup worked (the compose app among it); without it that
    shows as unknown
  - **Storage**: `Datastore.Audit` and `Datastore.AllocateSpace` on the VM disk
    storage; on the ISO storage also `Datastore.AllocateTemplate` (uploading ISOs)
    and `Datastore.Allocate` (deleting the portal's seed ISOs). Keep the ISO
    storage apart from VM disks: `Datastore.Allocate` can delete any volume on it
  - **Network**: `SDN.Use` on the bridge (`/sdn/zones/localnetwork`), **Node**: `Sys.Audit`
  - **Pool** (only with one set): `Pool.Allocate` on it

  Template builds need the same.

  The quick way, in a shell on a node (Proxmox VE 9; on 8 write `VM.Monitor`
  instead of `VM.GuestAgent.Audit VM.GuestAgent.FileRead`):

  ```sh
  pveum user add vmportal@pve
  pveum role add VMPortal -privs "VM.Allocate VM.Clone VM.Audit VM.Config.CDROM VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.PowerMgmt VM.Console VM.GuestAgent.Audit VM.GuestAgent.FileRead Datastore.Audit Datastore.AllocateSpace Datastore.AllocateTemplate Datastore.Allocate SDN.Use Sys.Audit"
  pveum acl modify / -user vmportal@pve -role VMPortal
  pveum user token add vmportal@pve vmportal        # prints the secret, once
  pveum acl modify / -token 'vmportal@pve!vmportal' -role VMPortal
  ```

  Both `acl` lines are needed: a privilege-separated token can't do more than
  its user, and gets nothing until it has a role of its own. This gives the role
  on the whole cluster (`/`); to narrow it, give it on the storages
  (`/storage/<name>`), the bridge, the node (`/nodes/<name>`) and a pool
  (`/pool/<name>`, plus `Pool.Allocate`) instead. Without permissions the token
  still connects but sees no storages or bridges; the backend page says so.
- **Certificate**: Proxmox's own certificate is signed by the cluster's CA,
  which nothing else trusts. The portal never turns verification off: paste the
  certificate's SHA-256 fingerprint (the node > System > Certificates) and it
  accepts exactly that certificate, and gives OpenTofu and Packer exactly that
  one too. Connect by a name or address the certificate covers. When a
  connection is refused, the message shows the fingerprint the server presented,
  to compare.
- **cloud-init** arrives on a small NoCloud seed ISO (label CIDATA) that the
  portal builds and uploads to the ISO storage for each new VM (Proxmox's own
  "snippets" would need SSH to every node). Once the VM's guest agent answers,
  the seed is replaced by a copy without keys, console password hash or compose
  file, which keeps cloud-init's instance id, and destroying the VM deletes it.
  If the agent doesn't answer in time, the VM's page says so and offers to clean
  up later.
- **Templates** come from the same image definitions (Packer's `proxmox-iso`
  builder); they install the QEMU guest agent and read cloud-init from NoCloud.
  Clones pass the host CPU through (Rocky Linux 10 needs x86-64-v3).
- **VLAN**: an optional VLAN tag per backend (1 to 4094) tags the network cards
  of new VMs and of template builds on the bridge. For several VLANs, add the
  cluster as several backends, each with its tag and IP pool.
- **Clusters**: a backend has one node of its own, where templates are built and
  VMs go by default. List more nodes under **More nodes for VMs** and the create
  form lets users pick one (only nodes that are online are offered). The
  storages and the bridge must exist on each under the same names; the backend's
  page says which nodes qualify.
  - The portal follows a VM that was migrated: power, console and autostart act
    on the node it's on, and before a resize or destroy the portal records that
    node in the VM's OpenTofu state (the copy from before is kept).
  - Every VM keeps its small seed ISO attached, so Proxmox only migrates the
    portal's VMs when the ISO storage is shared. The backend's page warns when
    it's local. Seeds are uploaded to the node the VM is created on.
  - A template is cloned from the node it's on; with local disk storage a VM for
    another node is cloned there and then moved.
  - The portal talks to the cluster through the one server in the backend's
    settings. To survive that node being down, point it at an address that moves
    (a virtual IP or DNS name) with a certificate that covers it.
  - Only tried against a fake of the API so far, not on a real multi-node cluster.
- **Autostart** is the VM's own "Start at boot" option; the node always honours it.
- A VM's addresses come from its cloud-init settings and from the QEMU guest agent.
- **Proxmox VE inside a VMware VM** (a lab): if a template build stays at
  "Waiting for SSH" and the installer has no network, the VM probably never gets
  its DHCP answer. The port group of the Proxmox VM needs promiscuous mode,
  forged transmits and MAC address changes set to Accept. With more than one
  uplink on that vSwitch, ESXi also sends the VMs' own broadcasts back, and the
  bridge then loses track of where they are: set
  `Net.ReversePathFwdCheckPromisc` to 1 on the ESXi host (and turn promiscuous
  mode off and on again), or add `bridge-ageing 0` to the bridge in
  `/etc/network/interfaces` on the Proxmox node.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
echo DATA_DIR=./data > .env          # then configure the rest in the UI
.venv/bin/uvicorn app.main:app --reload --port 8080
```

`tests/ui` drives the pages in headless Chromium (Playwright) against fake
backends. It needs `.venv/bin/playwright install chromium` once; where Chromium
doesn't start, `tests/ui/run-in-docker.sh` runs it in Playwright's image.
Without a browser those tests are skipped.

Provisioning and builds also need `tofu` (or `terraform`), `packer` and
`xorriso` on the PATH. Layout:

```
app/            FastAPI app (routes, jobs, backends, backup/, guests/, platforms/, templates/, static/)
docs/           backup and restore guide
images/         built-in image definitions (Packer + image.yaml)
terraform/vm/   the OpenTofu module that clones a VM on vSphere
terraform/proxmox/  the same for Proxmox VE
build/keys/     release signing keys the Dockerfile verifies tools with
tests/          pytest suite
```

## Website

The landing page lives in `site/` and is published to GitHub Pages; see
`site/README.md` for previewing it and regenerating the screenshots.

## License

vm-portal is licensed under the [Apache License 2.0](LICENSE). Bundled and
installed third-party components keep their own licenses; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Note that the container
image includes Packer, which is under HashiCorp's Business Source License: read
that section before offering vm-portal to third parties.

## Roadmap

- Proxmox VE: confirm clusters with several nodes and VLAN tags on real hardware
- Windows images (needs a sysprep or cloudbase-init guest driver)
- NetBox as IPAM provider
- Re-deploy or edit the compose app on an existing VM
- OIDC login
