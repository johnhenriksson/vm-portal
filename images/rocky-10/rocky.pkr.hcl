packer {
  required_plugins {
    vsphere = {
      source  = "github.com/hashicorp/vsphere"
      version = "~> 2.0"
    }
    proxmox = {
      source  = "github.com/hashicorp/proxmox"
      version = "~> 1.2"
    }
  }
}

locals {
  vm_name = var.vm_name != "" ? var.vm_name : "${var.template_prefix}-${formatdate("YYYYMMDD-hhmm", timestamp())}"
}

source "vsphere-iso" "rocky" {
  vcenter_server      = var.vsphere_server
  username            = var.vsphere_user
  password            = var.vsphere_password
  insecure_connection = var.vsphere_insecure

  datacenter = var.vsphere_datacenter
  cluster    = var.vsphere_cluster
  datastore  = var.vsphere_datastore
  folder     = var.vsphere_folder

  guest_os_type   = var.guest_os_type
  firmware        = "efi"
  CPUs            = var.vm_cpu_num
  RAM             = var.vm_mem_size
  RAM_reserve_all = false

  disk_controller_type = ["pvscsi"]

  storage {
    disk_size             = var.vm_disk_size
    disk_thin_provisioned = true
  }

  network_adapters {
    network      = var.vsphere_network
    network_card = "vmxnet3"
  }

  vm_name             = local.vm_name
  notes               = "Rocky Linux 10. Built ${formatdate("YYYY-MM-DD", timestamp())} by vm-portal (image rocky-10)"
  convert_to_template = true

  # Key-only: the public key must be in var.ssh_authorized_keys.
  ssh_username         = var.ssh_username
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "60m"

  iso_paths = [var.iso_url]

  # Anaconda reads the kickstart from the CD labelled OEMDRV.
  cd_content = {
    "ks.cfg" = templatefile("${path.root}/http/ks.pkrtpl.hcl", {
      ssh_authorized_keys = var.ssh_authorized_keys
      username            = var.ssh_username
      keyboard_layout     = var.keyboard_layout
      locale              = var.locale
    })
  }
  cd_label = "OEMDRV"

  # UEFI GRUB menu: select "Install", edit its kernel line, add the kickstart, boot.
  boot_wait = "10s"
  boot_command = [
    "<up>", "e", "<wait>", "<down><down><end>",
    " inst.text inst.ks=hd:LABEL=OEMDRV:/ks.cfg",
    "<leftCtrlOn>x<leftCtrlOff>",
  ]

  shutdown_command = "sudo shutdown -P now"
}

build {
  # One build per backend type: the portal runs `packer build -only=<type>-iso.*`.
  sources = ["source.vsphere-iso.rocky", "source.proxmox-iso.rocky"]

  provisioner "shell" {
    inline = [
      "set -eux",
      "sudo dnf -y upgrade --refresh",
    ]
  }

  # Grow the root LV on boot, so clones can use larger disks.
  provisioner "file" {
    source      = "./files/"
    destination = "/tmp/"
  }
  provisioner "shell" {
    inline = [
      "set -eux",
      "sudo install -m 0755 /tmp/grow-root.sh /usr/local/sbin/grow-root",
      "sudo install -m 0644 /tmp/grow-root.service /etc/systemd/system/grow-root.service",
      "sudo restorecon -v /usr/local/sbin/grow-root /etc/systemd/system/grow-root.service",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable grow-root.service",
    ]
  }

  # Proxmox: the QEMU guest agent reports the VM's addresses and shuts it down cleanly
  # (VMware Tools does that on vSphere).
  provisioner "shell" {
    only = ["proxmox-iso.rocky"]
    inline = [
      "set -eux",
      "sudo dnf -y install qemu-guest-agent",
      "sudo systemctl enable qemu-guest-agent",
    ]
  }

  # Must be last: generalizes the image for cloning. On Proxmox, clones read cloud-init
  # from the portal's seed ISO (NoCloud) instead of VMware guestinfo.
  provisioner "shell" {
    environment_vars = ["VMPORTAL_DATASOURCE=${source.type == "proxmox-iso" ? "NoCloud" : "VMware"}"]
    script           = "./scripts/cleanup.sh"
  }
}
