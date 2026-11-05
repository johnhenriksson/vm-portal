#!/bin/bash
# Install Docker Engine + Compose plugin from Docker's official apt repo.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

sudo cloud-init status --wait || true

sudo apt-get update
sudo -E apt-get -y install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${codename}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

# Fails here if Docker hasn't published packages for this Ubuntu release yet.
sudo apt-get update
sudo -E apt-get -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Rotate container logs so they can't fill the disk.
sudo install -m 0755 -d /etc/docker
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  },
  "live-restore": true
}
EOF

sudo systemctl enable docker.service containerd.service
sudo systemctl restart docker.service
sudo usermod -aG docker "$USER"

docker --version
docker compose version
