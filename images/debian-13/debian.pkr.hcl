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
  # Rendered once, so the file served and the checksum on the boot line always match.
  preseed = templatefile("${path.root}/http/preseed.pkrtpl.hcl", {
    authorized_keys_b64 = base64encode(join("\n", var.ssh_authorized_keys))
    username            = var.ssh_username
    password_hash       = local.build_password_hash
    keyboard_layout     = var.keyboard_layout
    locale              = var.locale
  })
}

source "vsphere-iso" "debian" {
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
  notes               = "Debian 13 + Docker. Built ${formatdate("YYYY-MM-DD", timestamp())} by vm-portal (image debian-13)"
  convert_to_template = true

  # Key-only: the public key must be in var.ssh_authorized_keys.
  ssh_username         = var.ssh_username
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "60m"

  iso_paths = [var.iso_url]

  # The Debian installer reads its answers over HTTP (it can't use a second CD).
  # The file holds only the build key's public half and a random password hash, but its
  # late_command runs as root: the boot line (typed over the vSphere console, a trusted
  # channel) carries its MD5, and the installer refuses a file that doesn't match, so
  # nobody on the build network can swap it.
  http_ip = var.http_ip
  http_content = {
    "/preseed.cfg" = local.preseed
  }

  # UEFI GRUB menu: open its command line and boot the installer in auto mode.
  boot_wait = "10s"
  boot_command = [
    "c", "<wait3s>",
    "linux /install.amd/vmlinuz auto=true priority=critical ",
    "url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/preseed.cfg preseed/url/checksum=${md5(local.preseed)} ",
    "netcfg/get_hostname=debian-packer netcfg/get_domain= ---", "<enter><wait3s>",
    "initrd /install.amd/initrd.gz", "<enter><wait3s>",
    "boot", "<enter>",
  ]

  shutdown_command = "sudo shutdown -P now"
}

build {
  sources = ["source.vsphere-iso.debian"]

  provisioner "shell" {
    environment_vars = ["DEBIAN_FRONTEND=noninteractive"]
    inline = [
      "set -eux",
      "sudo apt-get update",
      "sudo -E apt-get -y full-upgrade",
    ]
  }

  provisioner "shell" {
    script = "./scripts/docker-engine.sh"
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
