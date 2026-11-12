# The first IPv4 address the guest agent reports on a real interface (not loopback or a
# container bridge); null until the agent answers.
output "ip" {
  value = try([
    for ip in flatten(proxmox_virtual_environment_vm.this.ipv4_addresses) : ip
    if !startswith(ip, "127.") && !startswith(ip, "172.17.") && !startswith(ip, "169.254.")
  ][0], null)
}

output "vm_id" {
  value = proxmox_virtual_environment_vm.this.vm_id
}
