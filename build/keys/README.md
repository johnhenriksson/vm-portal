# Release signing keys

The Dockerfile verifies downloaded tools against their publishers' signed
`SHA256SUMS`, using only these keys:

| File | Publisher | Primary key fingerprint | Source |
|---|---|---|---|
| `hashicorp.asc` | HashiCorp (Packer) | `C874 011F 0AB4 0511 0D02 1055 3436 5D94 72D7 468F` | https://www.hashicorp.com/.well-known/pgp-key.txt |
| `opentofu.asc` | OpenTofu | `E3E6 E43D 84CB 852E ADB0 051D 0C0A F313 E5FD 9F80` | https://get.opentofu.org/opentofu.asc |

The build fails if either fingerprint changes. When a publisher rotates its key,
download the new one from the source above, compare the fingerprint with the
publisher's documentation, and update both this table and the Dockerfile.
