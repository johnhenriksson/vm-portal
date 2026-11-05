# vm-portal

A self-service portal for VMware vSphere. Users create VMs from templates in a
few clicks; admins build and maintain those templates with Packer, from the
same web interface. Runs as a single container.

- **Create VMs**: pick an image and version, size with sliders, DHCP or a static
  IP from a pool (with a free-IP finder), SSH keys, an optional console password
  and an optional `compose.yaml` that starts on first boot
- **Manage VMs**: power on/off/reboot, destroy, SSH command, and a **browser
  console** (noVNC) that works without access to the ESXi hosts
- **Images**: a catalog of image definitions (Packer templates): Ubuntu 26.04
  + Docker, Debian 13 + Docker and Rocky Linux 10 are built in. Configure each
  one's installer ISO, sizes and parameters; build with a live log; old
  templates are pruned automatically. With editing turned on, admins create new
  definitions from any existing one and edit them in a browser editor (syntax
  colouring, `packer validate` on save, history with restore).
- **Users**: admins and regular users; users only see their own VMs and jobs
- **Settings in the UI**: vCenter connection with resource pickers, IP pool,
  default SSH keys; secrets encrypted at rest
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
  OpenTofu). Each VM gets its own directory with a snapshot of the module and
  its own state, so destroying a VM always uses the code that created it.
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
| Any setting, e.g. `VSPHERE_SERVER` | Overrides the UI; shown read-only there. Use it to pin security switches such as `ALLOW_IMAGE_EDITING=false` or `HTTPS_ONLY_COOKIES=true` |

The data volume (`portal-data`) holds the database, keys, OpenTofu state and
logs. **Back it up**, and treat backups as secret: the state is how the portal
knows what to destroy, and the encryption key unlocks stored credentials.

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
- **Sessions**: Validate session
- **Profile-driven storage**: View

Assign it on the datacenter (propagate to children). Packer builds need the same
permissions plus upload access to the datastore with the ISOs.

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
app/            FastAPI app (routes, vSphere, jobs, guests/, templates/, static/)
images/         built-in image definitions (Packer + image.yaml)
terraform/vm/   the OpenTofu module that clones a VM
build/keys/     release signing keys the Dockerfile verifies tools with
tests/          pytest suite
```

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
