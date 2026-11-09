# Third-party notices

vm-portal itself is licensed under the Apache License 2.0 (`LICENSE`).
It bundles or downloads the components below. Their licenses apply to
those components; this file is a summary, not legal advice.

## Bundled in this repository

| Component | Version | License | Where |
|---|---|---|---|
| [noVNC](https://github.com/novnc/noVNC) | 1.7.0 | MPL-2.0 | `app/static/novnc/` (unmodified; `LICENSE.txt` and `AUTHORS` included) |
| [pako](https://github.com/nodeca/pako) (via noVNC) | — | MIT | `app/static/novnc/vendor/pako/` |
| [htmx](https://htmx.org) | 2.0.4 | 0BSD | `app/static/htmx.min.js` |

MPL-2.0 is file-level copyleft: if you modify noVNC's files, those files stay
under MPL-2.0 and their source must be available (it is, as they're served as
source).

## Installed into the container image

| Component | Version | License | Notes |
|---|---|---|---|
| [OpenTofu](https://opentofu.org) | see `Dockerfile` | MPL-2.0 | Provisions VMs. Signature-verified at build. |
| [Packer](https://www.packer.io) | see `Dockerfile` | **BUSL-1.1** | Builds templates. Signature-verified at build. |
| [packer-plugin-vsphere](https://github.com/hashicorp/packer-plugin-vsphere) | see `Dockerfile` | MPL-2.0 | |
| [terraform-provider-vsphere](https://github.com/vmware/terraform-provider-vsphere) | `~> 2.17` | MPL-2.0 | Mirrored into the image. |
| Python packages (`requirements.txt`) | | MIT, BSD-2/3-Clause, Apache-2.0, PSF-2.0, MPL-2.0 (certifi) | FastAPI, Starlette, Uvicorn, Jinja2, pydantic, pyVmomi, cryptography, bcrypt, pyrage, websockets, … |
| Debian packages (base image, `fping`, `xorriso`, `openssh-client`, …) | Debian bookworm | various, incl. GPL | Unmodified Debian packages; sources at https://sources.debian.org |

### Packer and the Business Source License

Packer is licensed under the [Business Source License 1.1](https://github.com/hashicorp/packer/blob/main/LICENSE).
It allows copying, modifying and redistributing Packer, and production use,
**except** offering it to third parties on a hosted or embedded basis in a way
that competes with HashiCorp's paid products. Running vm-portal to build your
own (or your organisation's) templates is ordinary production use. If you plan
to sell vm-portal, or run it as a hosted service for other organisations, read
the license and HashiCorp's BSL FAQ, or get legal advice, first.

Terraform is under the same license, which is why vm-portal uses OpenTofu
(MPL-2.0) instead. Packer has no equivalent open-source fork today.

## Not distributed

Operating system installers (ISOs) are never bundled: you provide them on a
vSphere datastore, under their own licenses (e.g. Windows and RHEL need your own
entitlements).
