# Clone one VM from a Packer-built template. Guest configuration (hostname,
# network, SSH keys, compose app) arrives via cloud-init in guestinfo; the
# caller renders metadata/userdata. Reusable outside the portal too.

terraform {
  required_version = ">= 1.6"
  required_providers {
    vsphere = {
      source  = "vmware/vsphere"
      version = "~> 2.17"
    }
  }
}

provider "vsphere" {
  vsphere_server       = var.vsphere_server
  user                 = var.vsphere_user
  password             = var.vsphere_password
  allow_unverified_ssl = var.vsphere_insecure
}

data "vsphere_datacenter" "this" {
  name = var.datacenter
}

data "vsphere_compute_cluster" "this" {
  name          = var.cluster
  datacenter_id = data.vsphere_datacenter.this.id
}

data "vsphere_datastore" "this" {
  name          = var.datastore
  datacenter_id = data.vsphere_datacenter.this.id
}

data "vsphere_network" "this" {
  name          = var.network
  datacenter_id = data.vsphere_datacenter.this.id
}

data "vsphere_virtual_machine" "template" {
  name          = var.template
  datacenter_id = data.vsphere_datacenter.this.id
}

resource "vsphere_virtual_machine" "this" {
  name             = var.name
  folder           = var.folder != "" ? var.folder : null
  resource_pool_id = data.vsphere_compute_cluster.this.resource_pool_id
  datastore_id     = data.vsphere_datastore.this.id

  num_cpus  = var.cpus
  memory    = var.memory_mb
  guest_id  = data.vsphere_virtual_machine.template.guest_id
  scsi_type = data.vsphere_virtual_machine.template.scsi_type
  firmware  = data.vsphere_virtual_machine.template.firmware

  network_interface {
    network_id   = data.vsphere_network.this.id
    adapter_type = data.vsphere_virtual_machine.template.network_interface_types[0]
  }

  disk {
    label            = "disk0"
    size             = max(var.disk_gb, data.vsphere_virtual_machine.template.disks[0].size)
    thin_provisioned = true
  }

  clone {
    template_uuid = data.vsphere_virtual_machine.template.id
  }

  extra_config = {
    "guestinfo.metadata"          = base64encode(var.metadata)
    "guestinfo.metadata.encoding" = "base64"
    "guestinfo.userdata"          = base64encode(var.userdata)
    "guestinfo.userdata.encoding" = "base64"
  }

  annotation = var.annotation

  # Only the first boot consumes cloud-init data; later edits must not rebuild the VM.
  lifecycle {
    ignore_changes = [extra_config, clone[0].template_uuid]
  }
}
