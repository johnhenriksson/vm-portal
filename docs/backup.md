# Backup and restore

vm-portal can back up two things:

- **Everything** (a *full backup*): users, settings, every VM's record and
  OpenTofu state, custom image definitions, jobs and the audit log, and
  optionally the job logs. Use it to recover from a lost or damaged data
  volume, or to move the portal to another host.
- **Chosen VMs** (a *VM backup*): only those VMs' records and OpenTofu state.
  Use it to recover a VM whose state was lost or damaged, or to move VMs to
  another portal.

Backups are made in the browser (**Settings → Backup**), on a schedule, or
from the command line. Every backup is encrypted; there is no way to make an
unencrypted one.

> The OpenTofu state is how the portal knows which VM in vCenter is which.
> Without it, the portal can still show and power a VM, but can't resize or
> destroy it.

## What a backup contains, and what it doesn't

| Included | Not included, and why |
|---|---|
| Users with their password hashes and roles | The encryption key: stored secrets are re-encrypted with the target portal's own key on restore |
| Settings stored in the portal | The session key and logged-out sessions: a restore logs everyone out |
| The vCenter password stored in the portal (inside the encryption) | Anything set in the environment (`.env`, `compose.yaml`), such as `VSPHERE_PASSWORD`: set it again on the new host |
| Each VM's record, OpenTofu state and variables | The Packer build key: a new one is generated |
| Custom image definitions (current version) | Their edit history, built-in images (they come with the portal), OpenTofu provider caches, saved plans |
| Jobs and the audit log; job logs if you ask for them | Destroyed VMs' archived files |

A backup is a `.vmpbak` file: a gzip-compressed tar archive with a
`manifest.json` that lists every file with its SHA-256 checksum, encrypted
with [age](https://age-encryption.org).

## Encryption: passphrase or public key

- **Passphrase** (downloads from the browser or the command line): at least
  16 characters. The browser suggests a random one. Store it in your password
  manager *before* you download: without it the backup can't be opened, and
  anyone who has it and the file can read everything in it.
- **age public key** (scheduled backups, and optionally downloads): the portal
  encrypts to one or more public keys (`age1…`). Only the matching *secret*
  key can open the backup, and **the portal never has it**. So someone who
  takes over the portal or the backup folder still can't read your backups.

Make a key pair on another machine, not on the portal's host:

```sh
age-keygen -o vm-portal-backup.key          # prints the public key
# or, without installing age:
docker compose run --rm --no-deps -v "$PWD:/out" portal \
  python -m app.backup keygen --out /out/vm-portal-backup.key
```

Paste the public key (`age1…`) into **Settings → Backup**. Keep the secret
key file (`AGE-SECRET-KEY-1…`) offline: in a password manager, on a USB stick
in a safe, or both. You can add several public keys, for example one per
person who should be able to restore.

**Who can make a backup that the portal accepts?** With a passphrase, only
someone who knows it. With a public key, anyone who has the public key, which
isn't secret. That's why the portal checks everything in a backup before
using it (see [Security](#security)), and why you should only restore backups
from your own backup storage.

## Scheduled backups

1. Mount storage at `/backups` in the container. `compose.yaml` mounts a
   named volume (`portal-backups`), which sits on the same disk as the portal's
   data. For real protection use another disk or a network share:

   ```yaml
   volumes:
     - portal-data:/data
     - /mnt/nas/vm-portal:/backups        # owned by uid 1000, mode 0700
   ```

   The portal refuses a backup folder inside its own data volume. Use
   `BACKUP_DIR` to change the path.
2. In **Settings → Backup**, turn on **Make scheduled backups**, choose how
   often and how many to keep, paste the public key(s) and save.
3. Click **Back up now** once and check the job log under **Jobs**.

Each backup is a job (visible under **Jobs**) and an audit log entry. Files are
named `vm-portal-full-<installation>-<UTC time>.vmpbak`, written atomically
with mode 0600, and only this installation's older scheduled files are
deleted beyond the number you keep. A backup waits while a VM job is running
(its state may be mid-write) and never runs during a restore. A failed backup
is retried within the hour.

If backups fail, or the last good one is older than twice the interval, admins
see a warning on the dashboard and under **Settings → Backup**.

**Copy backups off the host.** A backup on the same machine doesn't survive
losing that machine. Sync `/backups` somewhere else (rsync, rclone, your NAS's
replication); the files are encrypted, so the copy can live anywhere.

## Making a backup by hand

**Browser:** **Settings → Backup → Download a backup**. Choose *Everything* or
the VMs, a passphrase or the scheduled-backup public keys, and enter your
password. A VM's page also has an *Export it* link that preselects that VM.

**Command line**, for scripts:

```sh
docker compose stop portal
docker compose run --rm -v "$PWD:/out" portal python -m app.backup create \
  --out /out/portal.vmpbak --scheduled-keys
docker compose start portal
```

`create` takes `--passphrase-file FILE` (or `-` for standard input),
`--recipient age1…` (repeatable), `--scheduled-keys`, `--vm NAME`
(repeatable) and `--include-logs`. It refuses to run while the portal is up,
because it can't see the portal's running jobs; `--allow-running` overrides
that if you accept that a VM job running at that moment may leave a
half-written state in the backup. Scheduled backups and browser downloads
don't have this problem.

Passphrases and keys are never taken as command-line arguments (those end up
in shell history and the process list).

## Checking a backup

Test your backups: a backup you've never opened is a hope, not a backup.

```sh
docker compose run --rm -v "$PWD:/in:ro" portal python -m app.backup verify \
  /in/portal.vmpbak --identity-file /in/vm-portal-backup.key
```

`verify` decrypts the file, checks every checksum and runs every check a
restore would, without changing anything. Opening a backup under
**Settings → Backup** does the same and shows what's inside.

## Recovering from a failure

### Damaged state of one VM

Each resize and destroy first saves a copy of the VM's state. On the VM's page,
under **OpenTofu state**, an admin can roll back to one of the last five copies.
Only what the portal knows about the VM changes, not the VM in vCenter.

Without a usable copy, import the VM from a backup instead: **Settings →
Backup → Restore or import**, open the backup, tick the VM under **Import
VMs**. The VM's current state is kept in its history first.

### Lost or damaged data volume

On the same or a new host:

1. Start a fresh portal: `docker compose up -d`, with the same `.env` as before
   if you have it (environment-only values such as `VSPHERE_PASSWORD` aren't in
   the backup). Don't set `ADMIN_PASSWORD`.
2. Open the portal. On the first-run page, choose **Restore a full backup
   instead**, enter the setup token from `docker compose logs portal`, the
   backup file and its passphrase or secret key.
3. Check the summary and warnings, type `RESTORE` and confirm.
4. Log in with an admin account from the backup.
5. Under **Settings → Backup**, click **Check now** under *Check VMs against
   vCenter*.

Or from the command line, with the portal stopped:

```sh
docker compose stop portal
docker compose run --rm -v /path/to/backups:/in:ro portal python -m app.backup restore \
  /in/vm-portal-full-….vmpbak --identity-file /in/vm-portal-backup.key
docker compose start portal
```

### Undoing a restore

A full restore keeps the data it replaced in the data volume
(`/data/.pre-restore`) until the next restore. To go back:

```sh
docker compose stop portal
docker compose run --rm portal python -m app.backup undo-restore
docker compose start portal
```

Running `undo-restore` again switches back to the restored data.

## Moving to another host

1. On the old host, make a full backup (or use the latest scheduled one).
2. On the new host, restore it as in [Lost or damaged data volume](#lost-or-damaged-data-volume).
3. Check the VMs against vCenter (**Settings → Backup → Check now**).
4. **Shut the old portal down for good** (`docker compose down`). Two portals
   managing the same VMs undo each other's changes. The restore preview warns
   when a backup comes from another installation.

To move only some VMs, download a VM backup on the old portal and import it on
the new one. The old portal still lists them afterwards, and there's no button
yet to make it forget a VM without destroying it. So never resize or destroy
moved VMs there; ideally, stop using the old portal altogether.

### What's checked when VMs are imported

For each VM, the portal checks:

- The state holds only what the portal's own OpenTofu module creates.
- The variables hold only what the portal writes; old connection settings are
  dropped.
- In vCenter, the VM id in the state still belongs to a VM with that name. It
  must not be a template, it must be in this portal's datacenter (and folder, if
  set), and its vCenter notes must say vm-portal created it.
- The name and static IP aren't already in use in this portal.
- An existing VM is never pointed at a different vCenter id.

Owners are matched by username; a VM whose owner doesn't exist here goes to the
importing admin.

## Security

- **Encryption is mandatory.** Passphrase backups use age's scrypt; public-key
  backups use X25519. Both are authenticated: a changed file doesn't decrypt.
  The manifest's checksums then catch anything added, dropped or swapped inside.
- **Admins only, with their password.** Downloading, opening, restoring,
  importing and rolling back state all need the acting admin's password (with
  the same throttling as other password checks) and are written to the audit
  log, without any secrets.
- **Nothing is trusted.** An opened backup is read entirely in memory:
  - **Archive:** only regular files at known paths are accepted; links,
    devices, unexpected names, oversized files and archives over
    `BACKUP_MAX_MB` (default 512) are refused.
  - **Database:** the restore builds a fresh database from rows checked by the
    same rules as the UI. It never loads a database file from the backup.
  - **Settings, state and images:** settings are validated, state as described
    above, and image definitions by the editor's path rules. Built-in images
    are never replaced.
- **Custom image definitions are code** (Packer runs them inside the portal). If
  image editing is off where you restore, they come back disabled for review.
- **Plain text never touches the disk until you confirm.** An opened backup waits
  in the portal's memory, bound to your session, for at most 15 minutes. Uploads
  without a declared size or over the limit are refused before they're read.
- **A full restore** refuses to start while jobs run and answers every request
  with "try again" while it works. It builds the new data beside the old and
  swaps it in, then ends every session. That includes your own, and also when
  `SECRET_KEY` comes from the environment. It keeps this installation's id.
- **One portal per data volume.** The portal locks its data directory, so a second
  portal won't start on it, and the command-line restore won't run while the
  portal is up.

## Settings

| Where | Setting | Default |
|---|---|---|
| Settings → Backup | Make scheduled backups | off |
| | Every (hours) | 24 |
| | Keep the newest | 14 |
| | Encrypt to these age public keys | – |
| | Include job logs | off |
| Environment | `BACKUP_DIR`: where scheduled backups go | `/backups` |
| | `BACKUP_MAX_MB`: largest backup the portal writes, uploads or restores | 512 |

The schedule settings can also be set in the environment (`BACKUP_ENABLED`,
`BACKUP_INTERVAL_HOURS`, `BACKUP_KEEP`, `BACKUP_RECIPIENTS` with keys separated
by `|` or `,`, `BACKUP_INCLUDE_LOGS`); they're then read-only in the UI.

## Opening a backup without the portal

If the portal itself is gone, the standard `age` tool opens a backup:

```sh
age -d -i vm-portal-backup.key vm-portal-full-….vmpbak | tar -xz    # public-key backup
age -d vm-portal-full-….vmpbak | tar -xz                             # passphrase backup (asks for it)
```

You get `manifest.json`, `db/*.json` (database rows), `secrets.json` (the
stored vCenter password, in plain text: handle with care), `vms/<name>/` (each
VM's `terraform.tfstate`), `images/` and `jobs/`. With a VM's state and the
module in `terraform/vm`, OpenTofu can manage that VM without the portal.
