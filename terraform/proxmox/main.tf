# Clone one VM on Proxmox VE from a Packer-built template. Guest configuration
# (hostname, network, SSH keys, compose app) arrives via cloud-init on a NoCloud
# seed ISO that the portal builds and uploads first (var.seed_iso). Reusable
# outside the portal too.

terraform {
  required_version = ">= 1.6"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.121"
    }
  }
}

# The certificate is checked: against the system's CAs, or (with a pinned fingerprint
# in the portal) against exactly that certificate, which the portal passes as
# SSL_CERT_FILE. There is no "insecure" here.
provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
}

resource "proxmox_virtual_environment_vm" "this" {
  name        = var.name
  node_name   = var.node
  pool_id     = var.pool != "" ? var.pool : null
  description = var.annotation

  clone {
    vm_id        = var.template_vmid
    node_name    = var.template_node != "" ? var.template_node : null
    datastore_id = var.datastore
    full         = true
  }

  cpu {
    cores = var.cpus
    # As the templates are built: Rocky Linux 10 needs x86-64-v3, so the host CPU is passed through.
    type = "host"
  }

  memory {
    dedicated = var.memory_mb
  }

  disk {
    interface    = "scsi0"
    datastore_id = var.datastore
    size         = var.disk_gb
    discard      = "on"
    iothread     = true
  }

  network_device {
    bridge  = var.bridge
    model   = "virtio"
    vlan_id = var.vlan != "" ? tonumber(var.vlan) : null
  }

  # The first-boot seed (cloud-init NoCloud, label CIDATA).
  cdrom {
    interface = "ide2"
    file_id   = var.seed_iso
  }

  scsi_hardware = "virtio-scsi-single"

  # The template's firmware: UEFI templates (Debian, Rocky) keep their EFI disk.
  bios = var.bios
  dynamic "efi_disk" {
    for_each = var.bios == "ovmf" ? [1] : []
    content {
      datastore_id      = var.datastore
      type              = "4m"
      pre_enrolled_keys = false
    }
  }

  operating_system {
    type = "l26"
  }

  agent {
    enabled = true
    trim    = true
  }

  # The portal sets autostart through the API (the VM page's switch), and starts and
  # stops VMs itself.
  on_boot = false
  started = true

  # After the first boot the portal swaps the seed for a copy without secrets, and the
  # template may be pruned: neither must make a later plan rebuild the VM.
  lifecycle {
    ignore_changes = [cdrom, clone, on_boot, started, efi_disk]
  }
}
