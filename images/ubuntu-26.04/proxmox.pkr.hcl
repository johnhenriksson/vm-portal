# The same image on a Proxmox VE backend: proxmox-iso builds the template through the API
# (with an API token; the portal passes the pinned certificate as SSL_CERT_FILE, so there is
# no "skip TLS verification"). The build steps are shared with vSphere (ubuntu.pkr.hcl).

source "proxmox-iso" "ubuntu" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_token_id
  token                    = var.proxmox_token_secret
  insecure_skip_tls_verify = false
  node                     = var.proxmox_node
  pool                     = var.proxmox_pool != "" ? var.proxmox_pool : null

  vm_name              = local.vm_name
  template_name        = local.vm_name
  template_description = "Ubuntu 26.04 + Docker. Built ${formatdate("YYYY-MM-DD", timestamp())} by vm-portal (image ubuntu-26.04)"

  os              = "l26"
  cpu_type        = "host"
  cores           = var.vm_cpu_num
  memory          = var.vm_mem_size
  scsi_controller = "virtio-scsi-single"
  qemu_agent      = true
  # Clones get cloud-init from the portal's seed ISO, not from a Proxmox cloud-init drive.
  cloud_init = false

  disks {
    type         = "scsi"
    disk_size    = "${ceil(var.vm_disk_size / 1024)}G"
    storage_pool = var.proxmox_storage
    discard      = true
    io_thread    = true
  }

  network_adapters {
    bridge   = var.proxmox_bridge
    model    = "virtio"
    vlan_tag = var.proxmox_vlan # "" = untagged
  }

  boot_iso {
    type     = "ide"
    iso_file = var.iso_url
    unmount  = true
  }

  # The autoinstall answers, on a second CD labelled cidata (uploaded to the ISO storage).
  additional_iso_files {
    type             = "ide"
    iso_storage_pool = var.proxmox_iso_storage
    unmount          = true
    cd_label         = "cidata"
    cd_content = {
      "user-data" = templatefile("${path.root}/http/user-data.pkrtpl.hcl", {
        ssh_authorized_keys = var.ssh_authorized_keys
        username            = var.ssh_username
        password_hash       = local.build_password_hash
        keyboard_layout     = var.keyboard_layout
        locale              = var.locale
      })
      "meta-data" = file("${path.root}/http/meta-data")
    }
  }

  ssh_username           = var.ssh_username
  ssh_private_key_file   = var.ssh_private_key_file
  ssh_timeout            = "60m"
  ssh_handshake_attempts = 1000

  # SeaBIOS: the live server ISO boots GRUB here too. The three GRUB commands go on one line:
  # GRUB reads the kernel off the CD slowly here, and keys typed meanwhile are lost once the
  # BIOS keyboard buffer (15 keys) is full, so a separate "initrd" line arrives cut short.
  # The answers are found by the CD's label (cidata), so no "s=/cdrom/" (its ";" would end
  # the GRUB command).
  boot_wait = "10s"
  boot_command = [
    "c", "<wait3s>",
    "linux /casper/vmlinuz autoinstall ds=nocloud ; initrd /casper/initrd ; boot", "<enter>",
  ]
}
