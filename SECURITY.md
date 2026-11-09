# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
"Report a vulnerability" (Security advisories) on this repository rather than in
a public issue. Include steps to reproduce and the version or commit.

## Threat model in short

vm-portal holds powerful credentials (a vCenter account) and runs Terraform/OpenTofu
and Packer. Treat it like infrastructure tooling:

- **Admins are fully trusted.** They configure vCenter, manage users and build
  images. With image editing turned on (Settings → Images) they can also edit
  Packer definitions, which is equivalent to running code inside the portal
  container. Turning it on requires the admin's password and is audited; set
  `ALLOW_IMAGE_EDITING=false` in the environment to make sure it stays off.
- **Users** can create, power and destroy their own VMs and open their consoles.
  They can't see other users' VMs or jobs, settings, images or the audit log.
- Put the portal behind HTTPS (a reverse proxy) and turn on *HTTPS only* under
  Settings → Security (or set `HTTPS_ONLY_COOKIES=true`). The portal only lets
  you turn it on from an HTTPS connection, so it can't lock everyone out.
  Don't expose it to the internet unless you have to.
- Use a dedicated vCenter account with only the permissions in the README.
- Set up backups (Settings → Backup, see [`docs/backup.md`](docs/backup.md)).
  A backup holds the stored vCenter password and every user's password hash,
  so keep backups and their passphrases or secret keys as secret as the vCenter
  password. Scheduled backups are encrypted to age public keys: keep the secret
  keys off the portal's host, so a compromised portal can't read its backups.
  If you copy the raw data volume instead, that copy also holds the encryption
  key (unless `ENCRYPTION_KEY` is set elsewhere) and the Packer build key.
- **Restoring a backup trusts its users and settings.** A full restore replaces
  every account with the backup's. Anyone who has a public key can create a file
  encrypted to it, so only restore backups from your own storage. Admins already
  hold this power, and restoring needs an admin's password (or the setup token
  on a fresh portal) plus the backup's passphrase or secret key.

## What's in place

- Passwords hashed with scrypt; login throttling per IP and per username; a
  one-time setup token for the first admin; sessions revoked when a password,
  role or account status changes.
- CSRF tokens on every form; strict Content Security Policy (no inline scripts);
  `X-Frame-Options: DENY`, `no-store` caching, HSTS behind HTTPS.
- Turning on a sensitive switch (image editing, HTTPS only) needs the admin's
  password, so a stolen session can't quietly do it.
- Secrets encrypted at rest (Fernet) and never sent back to the browser;
  changing the vCenter server requires re-entering its password.
- Child processes (OpenTofu, Packer) get a minimal environment, never the
  portal's own secrets. Saved OpenTofu plans (they contain the vCenter
  password) are written to a 0700 folder and deleted as soon as the resize job ends.
- Browser console: ESXi certificate pinned to the thumbprint from vCenter;
  Origin, session and ownership checked; session limits.
- VMs: SSH key-only; optional console password stored only as a bcrypt hash;
  cloud-init data marked sensitive and redacted from vCenter after first boot;
  template build keys removed from every template.
- Container: non-root user, all Linux capabilities dropped except `NET_RAW`
  (ping), tool downloads verified against publishers' signatures.
- Backups always encrypted (age: passphrase or public keys); nothing in them is
  trusted on restore:
  - **Archive:** an allowlist of paths, no links or devices, size and count
    limits, and per-file checksums.
  - **Database:** rows are validated with the UI's own rules and imported into a
    fresh database; a SQLite file from a backup is never opened.
  - **OpenTofu state:** limited to the portal's own module. An imported VM's id
    must match that VM in vCenter, in the portal's datacenter and folder, and the
    VM must be one vm-portal created.
  - **Secrets:** re-encrypted with the target's own key, so the encryption key is
    never in a backup.
  - **Afterwards:** a full restore ends every session (new session epoch), even
    with `SECRET_KEY` set in the environment.
  - **Uploads:** an opened backup stays in memory only, bound to the admin's
    session, for 15 minutes. Uploads are size-checked before parsing.
- One portal process per data directory (file lock); the command-line restore
  refuses to run while the portal is up.
- Audit log of logins, settings, user, VM and image changes, backups and restores.
