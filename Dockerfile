# syntax=docker/dockerfile:1

# --- tools: download and verify OpenTofu, Packer, the providers and plugins ---------------
FROM python:3.12-slim-bookworm AS tools

ARG TOFU_VERSION=1.13.8
ARG PACKER_VERSION=1.16.2
ARG PACKER_VSPHERE_PLUGIN_VERSION=2.5.1
ARG PACKER_PROXMOX_PLUGIN_VERSION=1.2.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg unzip \
 && rm -rf /var/lib/apt/lists/*

# Release signing keys, pinned in the repo (see build/keys/README.md for fingerprints).
COPY build/keys/ /keys/
RUN set -eux; \
    export GNUPGHOME=/tmp/gnupg; mkdir -m 700 "$GNUPGHOME"; \
    gpg --batch --import /keys/hashicorp.asc /keys/opentofu.asc; \
    gpg --batch --list-keys --with-colons | grep -q '^fpr:::::::::C874011F0AB405110D02105534365D9472D7468F:'; \
    gpg --batch --list-keys --with-colons | grep -q '^fpr:::::::::E3E6E43D84CB852EADB0051D0C0AF313E5FD9F80:'; \
    mkdir /dl /out; cd /dl; \
    # OpenTofu (MPL-2.0): signed SHA256SUMS, then the zip's checksum
    base=https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}; \
    curl -fsSLO "$base/tofu_${TOFU_VERSION}_linux_amd64.zip"; \
    curl -fsSLO "$base/tofu_${TOFU_VERSION}_SHA256SUMS"; \
    curl -fsSLO "$base/tofu_${TOFU_VERSION}_SHA256SUMS.gpgsig"; \
    gpg --batch --verify "tofu_${TOFU_VERSION}_SHA256SUMS.gpgsig" "tofu_${TOFU_VERSION}_SHA256SUMS"; \
    grep " tofu_${TOFU_VERSION}_linux_amd64.zip\$" "tofu_${TOFU_VERSION}_SHA256SUMS" | sha256sum -c -; \
    unzip -q "tofu_${TOFU_VERSION}_linux_amd64.zip" tofu -d /out; \
    # Packer (BUSL-1.1, see THIRD_PARTY_NOTICES.md): same procedure with HashiCorp's key
    base=https://releases.hashicorp.com/packer/${PACKER_VERSION}; \
    curl -fsSLO "$base/packer_${PACKER_VERSION}_linux_amd64.zip"; \
    curl -fsSLO "$base/packer_${PACKER_VERSION}_SHA256SUMS"; \
    curl -fsSLO "$base/packer_${PACKER_VERSION}_SHA256SUMS.sig"; \
    gpg --batch --verify "packer_${PACKER_VERSION}_SHA256SUMS.sig" "packer_${PACKER_VERSION}_SHA256SUMS"; \
    grep " packer_${PACKER_VERSION}_linux_amd64.zip\$" "packer_${PACKER_VERSION}_SHA256SUMS" | sha256sum -c -; \
    unzip -q "packer_${PACKER_VERSION}_linux_amd64.zip" packer -d /out; \
    /out/tofu version; /out/packer version

# Packer plugins (checksum-verified by `packer plugins install` against the release's SHA256SUMS).
ENV PACKER_PLUGIN_PATH=/opt/packer-plugins
RUN /out/packer plugins install github.com/hashicorp/vsphere v${PACKER_VSPHERE_PLUGIN_VERSION} \
 && /out/packer plugins install github.com/hashicorp/proxmox v${PACKER_PROXMOX_PLUGIN_VERSION}

# The providers for VM jobs (vSphere, Proxmox VE), baked in so jobs never download from a
# registry. Mirrored under both registry hostnames so state from Terraform-era VMs still resolves.
COPY terraform /tmp/modules
RUN set -eux; \
    /out/tofu -chdir=/tmp/modules/vm providers mirror /opt/tofu-mirror; \
    /out/tofu -chdir=/tmp/modules/proxmox providers mirror /opt/tofu-mirror; \
    ln -s registry.opentofu.org /opt/tofu-mirror/registry.terraform.io; \
    ls -R /opt/tofu-mirror | head -20


# --- runtime --------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates fping iputils-ping xorriso openssh-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=tools /out/tofu /out/packer /usr/local/bin/
COPY --from=tools /opt/packer-plugins /opt/packer-plugins
COPY --from=tools /opt/tofu-mirror /opt/tofu-mirror
COPY tofurc /etc/tofurc
ENV PACKER_PLUGIN_PATH=/opt/packer-plugins TF_CLI_CONFIG_FILE=/etc/tofurc

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY terraform ./terraform
COPY images ./images
COPY app ./app
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./

RUN useradd --uid 1000 --create-home portal \
 && mkdir -p /data /backups && chown portal:portal /data /backups && chmod 0700 /backups
USER portal

ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PORT=8080
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
# One worker only: jobs run inside the process.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers"]
