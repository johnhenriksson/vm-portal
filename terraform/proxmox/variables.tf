variable "proxmox_endpoint" {
  type        = string
  description = "https://<node>:8006/"
}

variable "proxmox_api_token" {
  type        = string
  sensitive   = true
  description = "user@realm!tokenid=secret"
}

variable "node" {
  type = string
}

variable "datastore" {
  type        = string
  description = "Storage for the VM's disk"
}

variable "bridge" {
  type = string
}

variable "pool" {
  type    = string
  default = ""
}

variable "vlan" {
  type        = string
  default     = ""
  description = "VLAN tag of the network card on the bridge (1-4094); empty = untagged"
  validation {
    condition     = var.vlan == "" || can(tonumber(var.vlan) >= 1 && tonumber(var.vlan) <= 4094)
    error_message = "vlan is empty or a number from 1 to 4094."
  }
}

variable "template" {
  type        = string
  description = "Name of the Packer-built template (for people; the clone uses template_vmid)"
}

variable "template_vmid" {
  type = number
}

variable "template_node" {
  type    = string
  default = ""
}

variable "bios" {
  type        = string
  default     = "seabios"
  description = "The template's firmware: seabios or ovmf (UEFI)"
  validation {
    condition     = contains(["seabios", "ovmf"], var.bios)
    error_message = "bios is seabios or ovmf."
  }
}

variable "seed_iso" {
  type        = string
  description = "Volume id of the cloud-init seed ISO, e.g. local:iso/vm-portal-seed-web01.iso"
}

variable "name" {
  type = string
}

variable "cpus" {
  type    = number
  default = 2
}

variable "memory_mb" {
  type    = number
  default = 4096
}

variable "disk_gb" {
  type        = number
  default     = 40
  description = "Never smaller than the template disk; the guest grows LVM on boot"
}

# The seed ISO carries these; they're declared so every platform's module takes the
# same variables.
variable "metadata" {
  type      = string
  sensitive = true
  default   = ""
}

variable "userdata" {
  type      = string
  sensitive = true
  default   = ""
}

variable "annotation" {
  type    = string
  default = ""
}
