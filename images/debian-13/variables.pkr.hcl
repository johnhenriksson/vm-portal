# Variables the portal sets for every build (see images/README.md) ...
variable "vsphere_server" { type = string }
variable "vsphere_user" { type = string }
variable "vsphere_password" {
  type      = string
  sensitive = true
}
variable "vsphere_insecure" {
  type    = bool
  default = false
}
variable "vsphere_datacenter" { type = string }
variable "vsphere_cluster" { type = string }
variable "vsphere_datastore" { type = string }
variable "vsphere_network" { type = string }
variable "vsphere_folder" {
  type    = string
  default = ""
}
variable "vm_name" {
  type        = string
  default     = ""
  description = "Template name. Empty = <template_prefix>-YYYYMMDD-hhmm, so builds never replace the template in use."
}
variable "template_prefix" {
  type    = string
  default = "debian-13-docker"
}
variable "vm_cpu_num" { type = number }
variable "vm_mem_size" { type = number }
variable "vm_disk_size" {
  type        = number
  description = "Disk size in MB. Clones can be made larger; grow-root.service expands LVM on boot."
}
variable "iso_url" { type = string }
variable "ssh_private_key_file" {
  type    = string
  default = "~/.ssh/id_ed25519"
}
variable "ssh_authorized_keys" {
  type        = list(string)
  description = "Public keys for the build user during the build (one must match ssh_private_key_file). Removed from the template at the end."
  validation {
    condition     = length(var.ssh_authorized_keys) > 0
    error_message = "Set at least one public key in ssh_authorized_keys."
  }
}
variable "http_ip" {
  type        = string
  default     = ""
  description = "Address the build VM fetches the preseed file from. Empty: Packer picks one."
}

# ... and this definition's parameters (image.yaml).
variable "ssh_username" {
  type    = string
  default = "debian"
  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.ssh_username)) && var.ssh_username != "root"
    error_message = "Use a valid Linux user name (not root)."
  }
}
variable "keyboard_layout" {
  type    = string
  default = "us"
  validation {
    condition     = can(regex("^[a-z]{2,3}$", var.keyboard_layout))
    error_message = "Use an XKB layout code such as us, se or de."
  }
}
variable "locale" {
  type    = string
  default = "en_US.UTF-8"
  validation {
    condition     = can(regex("^[a-z]{2,3}_[A-Z]{2}\\.UTF-8$", var.locale))
    error_message = "Use a locale such as en_US.UTF-8."
  }
}
variable "guest_os_type" {
  type    = string
  default = "debian12_64Guest"
}
