#!/bin/bash
# Generalize the VM before it becomes a template. Clones are configured by
# cloud-init from VMware guestinfo (hostname, network, SSH keys, compose app).
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# SSH: key-only, never passwords. 01- sorts first, and sshd uses the first value it reads.
echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/01-no-password.conf > /dev/null
# Locale: use the VM's own. Don't accept the client's LANG/LC_* (macOS sends e.g.
# LC_CTYPE=UTF-8, which isn't a Linux locale: warnings on every login). AcceptEnv
# lines add up, so a drop-in can't undo them; comment them out where they are.
sudo sed -i -E 's/^([[:space:]]*AcceptEnv[[:space:]].*)$/# \1  # vm-portal: VMs use their own locale/' \
  /etc/ssh/sshd_config $(sudo find /etc/ssh/sshd_config.d -name '*.conf' 2>/dev/null)
sudo sshd -t
sudo passwd -l "$USER"

# cloud-init: off during the build (preseed late_command), on for clones.
sudo rm -f /etc/cloud/cloud-init.disabled
echo 'datasource_list: [ VMware, None ]' | sudo tee /etc/cloud/cloud.cfg.d/90-vmportal-datasource.cfg > /dev/null
# Make the existing build user cloud-init's default user, so user-data without
# a users: list doesn't create a second account.
sudo tee /etc/cloud/cloud.cfg.d/91-default-user.cfg > /dev/null <<CFG
system_info:
  default_user:
    name: ${USER}
    lock_passwd: true
    groups: [adm, sudo, docker]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    shell: /bin/bash
CFG

# Network: name the NIC eth0 (image.yaml network_interface), so cloud-init's
# ifupdown config for static IPs names the right interface. Drop the
# installer's config; cloud-init writes /etc/network/interfaces.d/50-cloud-init.
sudo sed -i -E 's/^GRUB_CMDLINE_LINUX="(.*)"/GRUB_CMDLINE_LINUX="\1 net.ifnames=0"/' /etc/default/grub
grep -q 'net.ifnames=0' /etc/default/grub
sudo update-grub
sudo tee /etc/network/interfaces > /dev/null <<'NET'
# Managed by cloud-init: see /etc/network/interfaces.d/
source /etc/network/interfaces.d/*

auto lo
iface lo inet loopback
NET
sudo rm -f /etc/network/interfaces.d/*

# Packages
sudo apt-get -y autoremove --purge
sudo apt-get -y clean

# Identity: each clone must get its own SSH host keys and machine-id.
sudo rm -f /etc/ssh/ssh_host_*
sudo cloud-init clean --logs --seed
sudo truncate -s 0 /etc/machine-id
sudo rm -f /var/lib/dbus/machine-id
sudo ln -s /etc/machine-id /var/lib/dbus/machine-id

# Logs, history, temp
sudo find /var/log -type f -exec truncate -s 0 {} +
sudo rm -rf /tmp/* /var/tmp/*
rm -f ~/.bash_history
sudo rm -f /root/.bash_history

# Build keys must not reach clones: they get their keys from cloud-init. Our
# SSH session stays open after this, and Packer only needs it to shut down.
rm -f ~/.ssh/authorized_keys

sudo sync
