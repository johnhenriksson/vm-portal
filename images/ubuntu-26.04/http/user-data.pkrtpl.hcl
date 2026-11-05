#cloud-config
autoinstall:
  version: 1
  early-commands:
    - timedatectl set-ntp true
  locale: ${jsonencode(locale)}
  keyboard:
    layout: ${jsonencode(keyboard_layout)}
  storage:
    layout:
      name: lvm
      # Default LVM layout leaves half the VG unallocated; use all of it.
      sizing-policy: all
  packages:
    - openssh-server
    - open-vm-tools
    - cloud-guest-utils
  identity:
    hostname: ubuntu-packer
    username: ${jsonencode(username)}
    # Random per build and never known; cleanup.sh locks it anyway. Key-only.
    password: ${jsonencode(password_hash)}
  ssh:
    install-server: true
    allow-pw: false
    # Build-time keys only (var.ssh_authorized_keys); cleanup.sh removes them.
    # Clones get their keys from cloud-init.
    authorized-keys: ${jsonencode(ssh_authorized_keys)}
  late-commands:
    - echo '${username} ALL=(ALL) NOPASSWD:ALL' > /target/etc/sudoers.d/90-${username}
    - chmod 0440 /target/etc/sudoers.d/90-${username}
