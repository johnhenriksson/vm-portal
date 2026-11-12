# Rocky Linux kickstart (rendered by Packer; see rocky.pkr.hcl).
# Values come from validated parameters. No passwords: root and the build user are locked.
text
lang ${locale}
keyboard --xlayouts=${keyboard_layout}
timezone UTC --utc
network --bootproto=dhcp --device=link --activate --onboot=yes --hostname=rocky-packer

# Packages from Rocky's own HTTPS origin, so any Rocky 10 ISO works and the template
# starts patched. Not the mirrorlist: it can hand out plain-HTTP third-party mirrors, and
# the installer doesn't GPG-check packages, so TLS to the origin is what authenticates them.
url --url="https://dl.rockylinux.org/pub/rocky/10/BaseOS/x86_64/os/"
repo --name=AppStream --baseurl="https://dl.rockylinux.org/pub/rocky/10/AppStream/x86_64/os/"

rootpw --lock
user --name=${username} --groups=wheel --lock
%{ for key in ssh_authorized_keys ~}
sshkey --username=${username} ${jsonencode(key)}
%{ endfor ~}

selinux --enforcing
firewall --enabled --service=ssh
# Both guest agents: each only runs on its own hypervisor. On Proxmox, Packer finds the
# build VM's address through the QEMU agent, so it must come with the install.
services --enabled=sshd,vmtoolsd,qemu-guest-agent
firstboot --disable

# One disk, LVM; grow-root extends the root LV on clones.
ignoredisk --only-use=sda
zerombr
clearpart --all --initlabel --drives=sda
autopart --type=lvm --nohome
bootloader --timeout=1
reboot --eject

%packages
@^minimal-environment
open-vm-tools
qemu-guest-agent
cloud-init
cloud-utils-growpart
lvm2
sudo
%end

%post --erroronfail
echo '${username} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-${username}
chmod 0440 /etc/sudoers.d/90-${username}
# cloud-init stays off during the build; cleanup.sh turns it back on.
touch /etc/cloud/cloud-init.disabled
%end
