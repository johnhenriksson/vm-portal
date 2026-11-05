output "ip" {
  value = vsphere_virtual_machine.this.default_ip_address
}

output "uuid" {
  value = vsphere_virtual_machine.this.uuid
}
