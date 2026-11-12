#!/bin/sh
# The browser tests in Playwright's own image, for machines where Chromium doesn't start
# (missing system libraries). Arguments go to pytest, e.g. -k phone. UI_SHOTS=<dir> keeps
# a screenshot of every test's last page (tests/ui/shots is git-ignored).
# The first run installs the Python packages into ~/.cache/vm-portal-ui; later runs reuse them.
set -eu
cd "$(dirname "$0")/../.."
version=$(sed -n 's/^playwright==//p' requirements-dev.txt)
cache="${XDG_CACHE_HOME:-$HOME/.cache}/vm-portal-ui"
mkdir -p "$cache"
exec docker run --rm --init --ipc=host --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/src -v "$cache":/pkgs -w /src \
  -e UI_REQUIRED=1 -e UI_SHOTS="${UI_SHOTS:-}" -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUSERBASE=/pkgs \
  "mcr.microsoft.com/playwright/python:v${version}-noble" sh -ec '
    pip install -q --user --break-system-packages -r requirements-dev.txt
    exec python -m pytest -p no:cacheprovider tests/ui "$@"' sh "$@"
