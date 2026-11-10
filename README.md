# vm-portal

A self-service portal for VMware vSphere. Users create VMs from templates in a
few clicks; admins build and maintain those templates with Packer, from the
same web interface. Runs as a single container.

- **Create VMs**: pick an image and version, size with sliders, DHCP or a static
  IP from a pool (with a free-IP finder), SSH keys, an optional console password
  and an optional `compose.yaml` that starts on first boot
- **Manage VMs**: power on/off/reboot, **resize** (vCPUs, memory, grow the
  disk), **autostart** with the host, notes, change owner, destroy, SSH command,
  and a **browser console** (noVNC) that works without access to the ESXi hosts
- **Images**: a catalog of image definitions (Packer templates): Ubuntu 26.04
  + Docker, Debian 13 + Docker and Rocky Linux 10 are built in. Configure each
  one's installer ISO, sizes and parameters; build with a live log; old
  templates are pruned automatically. With editing turned on, admins create new
  definitions from any existing one and edit them in a browser editor (syntax
  colouring, `packer validate` on save, history with restore).
- **Users**: admins and regular users; users only see their own VMs and jobs
- **Settings in the UI**: vCenter connection with resource pickers, IP pool,
  default SSH keys; secrets encrypted at rest
- **Backups**: encrypted full backups (download, schedule, or command line) to
  rebuild the portal or move it to another host, and per-VM state exports;
  restore in the browser, during first-run setup or from the command line
- **Audit log** and every OpenTofu/Packer run kept with its full log

## How it works

```
browser ──► FastAPI + htmx ──► jobs (in-process; logs in /data/jobs)
   │             │                 ├─ OpenTofu: one workspace + state per VM (/data/vms/<name>)
   │             │                 └─ Packer:   builds a copy of an image definition (images/<id>)
   │             └─► pyVmomi: inventory, templates, power, IPs, console tickets
   └── console ──wss──► portal ──wss (pinned TLS)──► ESXi host
```

- **Provisioning** clones a template with the module in `terraform/vm` (run by
  OpenTofu). Each VM gets its own directory with a copy of the module and its
  own state. Before a resize or destroy the copy is refreshed and the
  template's details are taken from the state, so pruned templates don't
  matter. A resize applies only if OpenTofu's plan changes nothing but vCPUs,
  memory and disk size (a change made by hand in vCenter stops it instead of
  being undone).
- **Guest setup** is done by a *guest driver* chosen by the image: `cloud-init`
  (via VMware guestinfo) for Linux. The interface in `app/guests/` is ready for
  others, such as Windows.
- **Image builds** create a new template `<prefix>-YYYYMMDD-HHMM`, so the
  template in use is never touched and a failed build costs nothing. See
  [`images/README.md`](images/README.md) for how definitions work.
- **Free IPs**: an address in the pool is offered only if the portal hasn't
  assigned it, vCenter doesn't report it, it doesn't answer ping and isn't in
  the ARP table; it's checked again just before provisioning (`app/ipam.py`).

## Quick start

```sh
git clone <this repository> vm-portal && cd vm-portal
docker compose up -d --build
docker compose logs portal | grep "setup token"
```

Open `http://<docker-host>:8080`. On first start the portal asks for the
one-time setup token from the log (so nobody else on the network can claim the
admin account), then walks you through connecting to vCenter (with dropdowns for
datacenter, cluster, datastore, network and folder), the IP pool and default SSH
keys. Then open **Images → Image definitions**, pick the installer ISO for an
image and build your first template.

Requirements:
- Linux Docker host. The container uses `network_mode: host` so the IP scanner
  sees the LAN's ARP table and Packer can reach build VMs. Debian builds also
  need the build VM to reach the Docker host on a TCP port between 8000 and 9000
  (Packer's HTTP server for the preseed file); open that range in the host's
  firewall if it has one.
- vCenter 7 or 8 and a service account (see below). Installer ISOs on a datastore.
- Behind HTTPS (a reverse proxy) for anything but a lab; then turn on
  **Settings → Security → HTTPS only** from an HTTPS connection.

Everything is configured on the **Settings** page: vCenter, network, VM
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
| Any setting, e.g. `VSPHERE_SERVER` | Overrides the UI; shown read-only there. Use it to pin security switches such as `ALLOW_IMAGE_EDITING=false` or `HTTPS_ONLY_COOKIES=true` |

The data volume (`portal-data`) holds the database, keys, OpenTofu state and
logs. The OpenTofu state is how the portal knows which VM in vCenter is which,
so **set up backups** before you rely on the portal.

## Backup and restore

**Settings → Backup** makes encrypted backups, with a passphrase or to
[age](https://age-encryption.org) public keys:

- **Full backup**: users, settings, every VM's record and OpenTofu state, custom
  image definitions, jobs and the audit log. Restore it to recover a lost data
  volume or to move the portal to another host. It also works from the first-run
  page of a fresh portal, or from the command line.
- **VM backup**: chosen VMs' OpenTofu state, to recover them or move them to
  another portal. Each VM also keeps its last five states for rollback. After a
  move, **Forget** on the VM's page makes the old portal let go of it without
  touching it in vCenter.
- **Scheduled backups** to `/backups`, encrypted to public keys whose secret keys
  stay offline, so the portal can write backups but never read them.

Nothing in a backup is trusted on restore. Everything is checked with the UI's
own rules, and an imported VM's id must match that very VM in vCenter. The
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
- **Secrets** (the vCenter password) are encrypted with Fernet and never sent
  back to the browser. Changing the vCenter server requires re-entering it.
- **Forms** are CSRF-protected; responses carry a strict Content Security Policy
  (no inline scripts), `X-Frame-Options: DENY` and `no-store`.
- **Console**: the portal fetches a one-time WebMKS ticket and relays the
  connection, pinning the ESXi certificate to the thumbprint vCenter reports.
  Browsers never reach ESXi or see the ticket. At most 3 consoles per user,
  8 hours each; sessions end when access is revoked.
- **VMs** are SSH key-only. The optional console password is stored only as a
  bcrypt hash in cloud-init (SSH stays key-only); cloud-init data is marked
  sensitive and redacted from vCenter after first boot.
- **Templates** contain no SSH keys: the build key the portal generates is
  removed at the end of every build.
- **Processes**: OpenTofu and Packer run with a minimal environment, never the
  portal's own secrets. The container runs as uid 1000 with only `NET_RAW`.
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

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
echo DATA_DIR=./data > .env          # then configure the rest in the UI
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Provisioning and builds also need `tofu` (or `terraform`), `packer` and
`xorriso` on the PATH. Layout:

```
app/            FastAPI app (routes, vSphere, jobs, backup/, guests/, templates/, static/)
docs/           backup and restore guide
images/         built-in image definitions (Packer + image.yaml)
terraform/vm/   the OpenTofu module that clones a VM
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

- Windows images (needs a sysprep or cloudbase-init guest driver)
- NetBox as IPAM provider
- Re-deploy or edit the compose app on an existing VM
- OIDC login
