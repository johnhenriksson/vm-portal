packer {
  required_plugins {
    vsphere = {
      source  = "github.com/hashicorp/vsphere"
      version = "~> 2.0"
    }
  }
}

locals {
  vm_name = var.vm_name != "" ? var.vm_name : "${var.template_prefix}-${formatdate("YYYYMMDD-hhmm", timestamp())}"
  # Nobody needs this password (logins are key-only and cleanup.sh locks it):
  # a random one per build means no known hash ships in the repo or template.
  build_password_hash = bcrypt(uuidv4(), 12)
}

source "vsphere-iso" "ubuntu" {
  vcenter_server      = var.vsphere_server
  username            = var.vsphere_user
  password            = var.vsphere_password
  insecure_connection = var.vsphere_insecure

  datacenter = var.vsphere_datacenter
  cluster    = var.vsphere_cluster
  datastore  = var.vsphere_datastore

  guest_os_type   = "ubuntu64Guest"
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
  notes               = "Ubuntu 26.04 + Docker. Built ${formatdate("YYYY-MM-DD", timestamp())} by vm-portal (image ubuntu-26.04)"
  convert_to_template = true

  # Key-only: the public key must be in var.ssh_authorized_keys.
  ssh_username         = var.ssh_username
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "60m"
  # The live installer runs its own sshd on the same IP and rejects us until
  # the install finishes and reboots; each rejection counts as an attempt.
  # Keep this high so ssh_timeout is the real limit.
  ssh_handshake_attempts = 1000

  iso_paths = [var.iso_url]

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
  cd_label  = "cidata"
  boot_wait = "10s"

  boot_command = [
    "c", "<wait3s>",
    "linux /casper/vmlinuz autoinstall ds=nocloud;s=/cdrom/", "<enter><wait3s>",
    "initrd /casper/initrd", "<enter><wait3s>",
    "boot", "<enter>",
  ]

  shutdown_command = "sudo shutdown -P now"
}

build {
  sources = ["source.vsphere-iso.ubuntu"]

  # Base system: wait for first boot to settle, then fully patch.
  provisioner "shell" {
    environment_vars = ["DEBIAN_FRONTEND=noninteractive"]
    inline = [
      "set -eux",
      "sudo cloud-init status --wait || true",
      "echo 'debconf debconf/frontend select Noninteractive' | sudo debconf-set-selections",
      "sudo apt-get update",
      "sudo -E apt-get -y full-upgrade",
      "sudo -E apt-get -y install util-linux-extra cloud-guest-utils lvm2",
    ]
  }

  provisioner "shell" {
    script = "./scripts/docker-engine.sh"
  }

  # Grow the root LV on boot, so Terraform clones can use larger disks.
  provisioner "file" {
    source      = "./files/grow-root.sh"
    destination = "/tmp/grow-root.sh"
  }
  provisioner "file" {
    source      = "./files/grow-root.service"
    destination = "/tmp/grow-root.service"
  }
  provisioner "file" {
    source      = "./files/ssh-host-keys.service"
    destination = "/tmp/ssh-host-keys.service"
  }
  provisioner "shell" {
    inline = [
      "set -eux",
      "sudo install -m 0755 /tmp/grow-root.sh /usr/local/sbin/grow-root",
      "sudo install -m 0644 /tmp/grow-root.service /etc/systemd/system/grow-root.service",
      "sudo install -m 0644 /tmp/ssh-host-keys.service /etc/systemd/system/ssh-host-keys.service",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable grow-root.service ssh-host-keys.service",
    ]
  }

  # Must be last: generalizes the image for cloning.
  provisioner "shell" {
    script = "./scripts/cleanup.sh"
  }
}
