# Rocky Linux kickstart (rendered by Packer; see rocky.pkr.hcl).
# Values come from validated parameters. No passwords: root and the build user are locked.
text
lang ${locale}
keyboard --xlayouts=${keyboard_layout}
timezone UTC --utc
network --bootproto=dhcp --device=link --activate --onboot=yes --hostname=rocky-packer

# Packages from the mirrors, so any Rocky 10 ISO works and the template starts patched.
url --mirrorlist="https://mirrors.rockylinux.org/mirrorlist?arch=x86_64&repo=BaseOS-10"
repo --name=AppStream --mirrorlist="https://mirrors.rockylinux.org/mirrorlist?arch=x86_64&repo=AppStream-10"

rootpw --lock
user --name=${username} --groups=wheel --lock
%{ for key in ssh_authorized_keys ~}
sshkey --username=${username} ${jsonencode(key)}
%{ endfor ~}

selinux --enforcing
firewall --enabled --service=ssh
services --enabled=sshd,vmtoolsd
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
