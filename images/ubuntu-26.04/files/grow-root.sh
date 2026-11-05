#!/bin/bash
# Grow the partition(s) backing the root LVM volume to fill the disk, then
# extend the root LV and its filesystem. Safe to run every boot: it does nothing
# when there is no free space.
set -euo pipefail

root_lv="$(findmnt -no SOURCE /)"
vg="$(lvs --noheadings -o vg_name "$root_lv" | tr -d ' ')"

for pv in $(pvs --noheadings -o pv_name -S vg_name="$vg"); do
  part_name="$(basename "$(readlink -f "$pv")")"
  disk="/dev/$(lsblk -ndo pkname "/dev/$part_name")"
  part_num="$(cat "/sys/class/block/$part_name/partition")"
  # growpart exits 1 when there is nothing to grow (NOCHANGE).
  rc=0; growpart "$disk" "$part_num" || rc=$?
  if [ "$rc" -gt 1 ]; then exit "$rc"; fi
  pvresize "$pv"
done

if [ "$(vgs --noheadings --units b --nosuffix -o vg_free "$vg" | tr -d ' ')" -gt 0 ]; then
  lvextend -r -l +100%FREE "$root_lv"
fi
