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
- Back up the data volume and keep the backup as secret as the vCenter password:
  it holds the encryption key (unless `ENCRYPTION_KEY` is set elsewhere), the
  Terraform state and the Packer build key.

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
  portal's own secrets.
- Browser console: ESXi certificate pinned to the thumbprint from vCenter;
  Origin, session and ownership checked; session limits.
- VMs: SSH key-only; optional console password stored only as a bcrypt hash;
  cloud-init data marked sensitive and redacted from vCenter after first boot;
  template build keys removed from every template.
- Container: non-root user, all Linux capabilities dropped except `NET_RAW`
  (ping), tool downloads verified against publishers' signatures.
- Audit log of logins, settings, user, VM and image changes.
