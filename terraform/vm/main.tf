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

# The template is only needed to create the VM. Later plans (resize, destroy) get its
# facts from the VM's state through var.template_facts, because old templates are pruned.
data "vsphere_virtual_machine" "template" {
  count         = var.template_facts == null ? 1 : 0
  name          = var.template
  datacenter_id = data.vsphere_datacenter.this.id
}

locals {
  template = var.template_facts != null ? merge(var.template_facts, { disk_size = 0 }) : {
    guest_id     = data.vsphere_virtual_machine.template[0].guest_id
    scsi_type    = data.vsphere_virtual_machine.template[0].scsi_type
    firmware     = data.vsphere_virtual_machine.template[0].firmware
    adapter_type = data.vsphere_virtual_machine.template[0].network_interface_types[0]
    uuid         = data.vsphere_virtual_machine.template[0].id
    disk_size    = data.vsphere_virtual_machine.template[0].disks[0].size
  }
}

resource "vsphere_virtual_machine" "this" {
  name             = var.name
  folder           = var.folder != "" ? var.folder : null
  resource_pool_id = data.vsphere_compute_cluster.this.resource_pool_id
  datastore_id     = data.vsphere_datastore.this.id

  num_cpus  = var.cpus
  memory    = var.memory_mb
  guest_id  = local.template.guest_id
  scsi_type = local.template.scsi_type
  firmware  = local.template.firmware

  network_interface {
    network_id   = data.vsphere_network.this.id
    adapter_type = local.template.adapter_type
  }

  disk {
    label            = "disk0"
    size             = max(var.disk_gb, local.template.disk_size)
    thin_provisioned = true
  }

  clone {
    template_uuid = local.template.uuid
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
