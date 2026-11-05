#!/bin/bash
# Generalize the VM before it becomes a template. Clones are configured by
# cloud-init from VMware guestinfo (hostname, network, SSH keys).
set -euxo pipefail

# SSH: key-only, never passwords. 01- sorts first, and sshd uses the first value it reads.
echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/01-no-password.conf > /dev/null
sudo passwd -l "$USER"

# cloud-init: off during the build (kickstart %post), on for clones.
sudo rm -f /etc/cloud/cloud-init.disabled
echo 'datasource_list: [ VMware, None ]' | sudo tee /etc/cloud/cloud.cfg.d/90-vmportal-datasource.cfg > /dev/null
# Make the existing build user cloud-init's default user, so user-data without
# a users: list doesn't create a second account.
sudo tee /etc/cloud/cloud.cfg.d/91-default-user.cfg > /dev/null <<CFG
system_info:
  default_user:
    name: ${USER}
    lock_passwd: true
    groups: [wheel]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    shell: /bin/bash
CFG

# Network: name the NIC eth0 (image.yaml network_interface), so cloud-init's
# NetworkManager config for static IPs names the right interface. Drop the
# installer's connection; cloud-init writes its own.
sudo grubby --update-kernel=ALL --args="net.ifnames=0"
sudo sed -i -E '/net\.ifnames=0/! s/^GRUB_CMDLINE_LINUX="(.*)"/GRUB_CMDLINE_LINUX="\1 net.ifnames=0"/' /etc/default/grub
sudo rm -f /etc/NetworkManager/system-connections/*

# Packages
sudo dnf -y autoremove
sudo dnf clean all

# Identity: each clone must get its own SSH host keys (sshd-keygen recreates
# them on boot) and machine-id.
sudo rm -f /etc/ssh/ssh_host_*
sudo cloud-init clean --logs --seed
sudo truncate -s 0 /etc/machine-id

# Logs, history, temp
sudo find /var/log -type f -exec truncate -s 0 {} +
sudo rm -rf /tmp/* /var/tmp/*
rm -f ~/.bash_history
sudo rm -f /root/.bash_history

# Build keys must not reach clones: they get their keys from cloud-init. Our
# SSH session stays open after this, and Packer only needs it to shut down.
rm -f ~/.ssh/authorized_keys

sudo sync
