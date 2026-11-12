# The same image on a Proxmox VE backend: proxmox-iso builds the template through the API
# (with an API token; the portal passes the pinned certificate as SSL_CERT_FILE, so there is
# no "skip TLS verification"). The build steps are shared with vSphere (debian.pkr.hcl).

source "proxmox-iso" "debian" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_token_id
  token                    = var.proxmox_token_secret
  insecure_skip_tls_verify = false
  node                     = var.proxmox_node
  pool                     = var.proxmox_pool != "" ? var.proxmox_pool : null

  vm_name              = local.vm_name
  template_name        = local.vm_name
  template_description = "Debian 13 + Docker. Built ${formatdate("YYYY-MM-DD", timestamp())} by vm-portal (image debian-13)"

  os              = "l26"
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

  ssh_username         = var.ssh_username
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "60m"

  # The preseed is served over HTTP, checked by its MD5 on the boot line (see debian.pkr.hcl).
  http_content = {
    "/preseed.cfg" = local.preseed
  }

  # "c" opens the GRUB command line. It's sent twice, because the firmware takes longer on
  # some hosts and a "c" that comes too early is lost; the rest would then be typed into the
  # menu, where "i" starts a manual install. The second "c" is deleted again if the first one
  # arrived. The three GRUB commands are on one line so nothing is typed while GRUB loads the
  # kernel (keys typed then can be lost).
  boot_wait = "15s"
  boot_command = [
    "c", "<wait5s>", "c", "<wait2s>", "<bs>",
    "linux /install.amd/vmlinuz auto=true priority=critical ",
    "url=http://${var.http_ip != "" ? var.http_ip : "{{ .HTTPIP }}"}:{{ .HTTPPort }}/preseed.cfg preseed/url/checksum=${md5(local.preseed)} ",
    "netcfg/get_hostname=debian-packer netcfg/get_domain= --- ; ",
    "initrd /install.amd/initrd.gz ; boot", "<enter>",
  ]
}
