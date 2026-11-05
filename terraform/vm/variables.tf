variable "vsphere_server" {
  type = string
}

variable "vsphere_user" {
  type = string
}

variable "vsphere_password" {
  type      = string
  sensitive = true
}

variable "vsphere_insecure" {
  type    = bool
  default = true
}

variable "datacenter" {
  type = string
}

variable "cluster" {
  type = string
}

variable "datastore" {
  type = string
}

variable "network" {
  type = string
}

variable "folder" {
  type    = string
  default = ""
}

variable "template" {
  type        = string
  description = "Name of the Packer-built template to clone"
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

variable "metadata" {
  type        = string
  sensitive   = true # keep it out of plan output / job logs
  description = "cloud-init meta-data (YAML): instance-id, local-hostname, optional network"
}

variable "userdata" {
  type        = string
  sensitive   = true # may contain the console password hash
  description = "cloud-init user-data (#cloud-config YAML)"
}

variable "annotation" {
  type    = string
  default = ""
}
