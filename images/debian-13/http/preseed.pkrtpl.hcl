# Debian installer answers (rendered by Packer; see debian.pkr.hcl).
# Values come from validated parameters; the password is random per build.
d-i debian-installer/locale string ${locale}
d-i keyboard-configuration/xkb-keymap select ${keyboard_layout}

d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string debian-packer
d-i netcfg/get_domain string
d-i hw-detect/load_firmware boolean false

d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string
d-i apt-setup/cdrom/set-first boolean false
d-i apt-setup/disable-cdrom-entries boolean true
d-i apt-setup/services-select multiselect security, updates
d-i apt-setup/non-free-firmware boolean false

d-i passwd/root-login boolean false
d-i passwd/user-fullname string ${username}
d-i passwd/username string ${username}
d-i passwd/user-password-crypted password ${password_hash}
d-i passwd/user-default-groups string sudo

d-i clock-setup/utc boolean true
d-i time/zone string UTC
d-i clock-setup/ntp boolean true

# One disk, LVM, everything in the root LV (grow-root extends it on clones).
d-i partman-auto/disk string /dev/sda
d-i partman-auto/method string lvm
d-i partman-auto-lvm/guided_size string max
d-i partman-auto/choose_recipe select atomic
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-lvm/confirm boolean true
d-i partman-lvm/confirm_nooverwrite boolean true
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string openssh-server open-vm-tools cloud-init cloud-guest-utils sudo curl ca-certificates lvm2
d-i pkgsel/upgrade select full-upgrade
popularity-contest popularity-contest/participate boolean false

d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default
# Also install to the removable-media path: VMware's NVRAM doesn't always keep the boot entry.
d-i grub-installer/force-efi-extra-removable boolean true
d-i finish-install/reboot_in_progress note

# Build-time key only (cleanup.sh removes it; clones get keys from cloud-init).
# cloud-init stays off during the build; cleanup.sh turns it back on.
d-i preseed/late_command string \
  in-target install -d -m 0700 -o ${username} -g ${username} /home/${username}/.ssh; \
  in-target sh -c "echo ${authorized_keys_b64} | base64 -d > /home/${username}/.ssh/authorized_keys"; \
  in-target chown ${username}:${username} /home/${username}/.ssh/authorized_keys; \
  in-target chmod 0600 /home/${username}/.ssh/authorized_keys; \
  echo '${username} ALL=(ALL) NOPASSWD:ALL' > /target/etc/sudoers.d/90-${username}; \
  chmod 0440 /target/etc/sudoers.d/90-${username}; \
  touch /target/etc/cloud/cloud-init.disabled
