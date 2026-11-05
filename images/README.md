# Image definitions

Each directory here is an **image definition**: a Packer template plus an
`image.yaml` manifest that tells vm-portal how to build the image and how to
configure VMs cloned from it. The portal lists every definition under
**Images → Image definitions**, where admins set its ISO, sizes and parameters
and start builds.

- **Built-in** definitions live here and ship in the container image (read-only).
- **Custom** definitions live in the data volume (`/data/images/<id>/`). With
  `ALLOW_IMAGE_EDITING=true`, admins can duplicate a definition and edit its
  files in the browser (every change is audited and snapshotted).

## Layout

```
images/<id>/                  id: lowercase letters, digits, '.', '-'
  image.yaml                  the manifest (below)
  *.pkr.hcl                   the Packer template (any layout Packer accepts)
  http/ files/ scripts/ …     whatever the template needs
```

## Manifest (`image.yaml`)

```yaml
name: Ubuntu 26.04 LTS + Docker        # shown in the UI
description: …
os: {family: linux, distro: ubuntu, version: "26.04"}   # distro picks the icon
guest: cloud-init                      # guest driver used for clones (app/guests/)
features: [docker]                     # optional: enables compose.yaml on create
template_prefix: ubuntu-2604-docker    # templates are named <prefix>-YYYYMMDD-HHMM
login_user_parameter: ssh_username     # or `login_user: <name>` for a fixed user
build: {cpus: 2, memory_mb: 2048, disk_mb: 25600}   # defaults, editable per image
iso: {hint: "…", match: "ubuntu-26.04*live-server-amd64.iso"}  # preselects an ISO
parameters:                            # extra Packer variables admins can set
  - name: keyboard_layout
    label: Keyboard layout
    default: us
    pattern: "^[a-z]{2,3}$"            # required; values must match it entirely
```

Parameters become `PKR_VAR_<name>`. Every value is checked against its anchored
`pattern` before a build, so a parameter can't smuggle extra lines into a
generated file. Still quote values when templating them (e.g. `jsonencode()`).

## Variables the portal passes

The portal sets these for every build (as `PKR_VAR_*`). Declare the ones your
template uses; Packer ignores the rest.

| Variable | Type | Value |
|---|---|---|
| `vsphere_server`, `vsphere_user`, `vsphere_password` | string | vCenter connection |
| `vsphere_insecure` | bool | skip TLS verification (Settings → vCenter) |
| `vsphere_datacenter`, `vsphere_cluster`, `vsphere_datastore`, `vsphere_network`, `vsphere_folder` | string | placement |
| `vm_name` | string | the new template's name (`<template_prefix>-YYYYMMDD-HHMM`) |
| `template_prefix` | string | from the manifest |
| `iso_url` | string | datastore path of the installer ISO |
| `vm_cpu_num`, `vm_mem_size`, `vm_disk_size` | number | build VM size (MB for memory and disk) |
| `ssh_private_key_file` | string | the portal's build key (private half) |
| `ssh_authorized_keys` | list(string) | the build key's public half |

## Rules for definitions

- **Don't bake credentials or keys into templates.** Let the installer authorise
  `ssh_authorized_keys` for the build and remove them at the end (see
  `ubuntu-26.04/scripts/cleanup.sh`); clones get their keys from the guest driver.
- **Generalise the template:** remove SSH host keys and machine-id, clean
  cloud-init (`cloud-init clean`), and leave the guest agent (open-vm-tools) installed.
- **Linux with `guest: cloud-init`** needs cloud-init's VMware datasource enabled
  (`datasource_list: [ VMware, None ]`) so clones read `guestinfo.metadata/userdata`.
- **Windows** needs a guest driver that doesn't exist yet (sysprep or
  cloudbase-init). The manifest schema and the driver interface
  (`app/guests/__init__.py`) are ready for it.
- Only the vSphere Packer plugin is pre-installed. A definition that needs
  another plugin will fail at `packer init`, because the image has no writable
  plugin directory.

## Building outside the portal

Each definition is a normal Packer template:

```sh
cd images/ubuntu-26.04
cp ubuntu.auto.pkrvars.hcl.example ubuntu.auto.pkrvars.hcl   # gitignored
export PKR_VAR_vsphere_password='…'
packer init . && packer build .
```
