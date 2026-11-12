# The same image on a Proxmox VE backend: proxmox-iso builds the template through the API
# (with an API token; the portal passes the pinned certificate as SSL_CERT_FILE, so there is
# no "skip TLS verification"). The build steps are shared with vSphere (rocky.pkr.hcl).

source "proxmox-iso" "rocky" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_token_id
  token                    = var.proxmox_token_secret
  insecure_skip_tls_verify = false
  node                     = var.proxmox_node
  pool                     = var.proxmox_pool != "" ? var.proxmox_pool : null

  vm_name              = local.vm_name
  template_name        = local.vm_name
  template_description = "Rocky Linux 10. Built ${formatdate("YYYY-MM-DD", timestamp())} by vm-portal (image rocky-10)"

  os = "l26"
  # Rocky Linux 10 needs an x86-64-v3 CPU: pass the host's through.
  cpu_type        = "host"
  cores           = var.vm_cpu_num
  memory          = var.vm_mem_size
  scsi_controller = "virtio-scsi-single"
  qemu_agent      = true
  cloud_init      = false

  # UEFI, as on vSphere, so the boot line below is the same.
  bios = "ovmf"
  efi_config {
    efi_storage_pool  = var.proxmox_storage
    efi_type          = "4m"
    pre_enrolled_keys = false
  }

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

  # Anaconda reads the kickstart from the CD labelled OEMDRV.
  additional_iso_files {
    type             = "ide"
    iso_storage_pool = var.proxmox_iso_storage
    unmount          = true
    cd_label         = "OEMDRV"
    cd_content = {
      "ks.cfg" = templatefile("${path.root}/http/ks.pkrtpl.hcl", {
        ssh_authorized_keys = var.ssh_authorized_keys
        username            = var.ssh_username
        keyboard_layout     = var.keyboard_layout
        locale              = var.locale
      })
    }
  }

  ssh_username         = var.ssh_username
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "60m"

  boot_wait = "10s"
  boot_command = [
    "<up>", "e", "<wait>", "<down><down><end>",
    " inst.text inst.ks=hd:LABEL=OEMDRV:/ks.cfg",
    "<leftCtrlOn>x<leftCtrlOff>",
  ]
}
