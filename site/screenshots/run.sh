#!/bin/sh
# Regenerate site/img/: run the portal with made-up data (demo_server.py), serve site/,
# and take the screenshots with headless Chromium in the Playwright image.
# Needs the project's .venv (pip install -r requirements.txt) and Docker.
set -eu
cd "$(dirname "$0")/../.."
scratch=$(mktemp -d)
trap 'kill $portal $site 2>/dev/null || true; rm -rf "$scratch"' EXIT
.venv/bin/python site/screenshots/demo_server.py "$scratch/data" 8099 > "$scratch/portal.log" 2>&1 & portal=$!
python3 -m http.server 8098 --bind 127.0.0.1 --directory site > /dev/null 2>&1 & site=$!
for _ in $(seq 60); do curl -fs http://127.0.0.1:8099/healthz > /dev/null && break; sleep 0.5; done
docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/site:/site" \
  mcr.microsoft.com/playwright/python:v1.55.0-noble sh -c '
    pip install -q --target /tmp/pw playwright==1.55.0 2>/dev/null &&
    PYTHONPATH=/tmp/pw python /site/screenshots/capture.py http://127.0.0.1:8099 http://127.0.0.1:8098'
