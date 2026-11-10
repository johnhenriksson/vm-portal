#!/bin/sh
# Make the demo videos: run the portal with made-up data (../screenshots/demo_server.py),
# click through it with headless Chromium (record.py), and put the frames together with
# ffmpeg (compose.py). Needs the project's .venv and Docker.
#
#   site/video/run.sh [OUT_DIR] [VARIANT...]     default: site/video/out, all of wide square tall
#
# wide:   16:9, about 80 s, the full tour (website)
# square: 1:1 and tall: 9:16, about 35 s (social media)
set -eu
cd "$(dirname "$0")/../.."
out=${1:-site/video/out}
[ $# -gt 0 ] && shift
variants=${*:-wide square tall}
mkdir -p "$out"
out=$(cd "$out" && pwd)
scratch=$(mktemp -d)
portal=
trap '[ -n "$portal" ] && kill $portal 2>/dev/null; rm -rf "$scratch"' EXIT

# The end card shows where the code is: this checkout's GitHub remote, if it has one.
repo=$(git remote get-url origin 2>/dev/null | sed -E 's#^(git@|ssh://git@|https://)##; s#^([^/:]+):#\1/#; s#\.git$##' || true)

docker build -q -t vm-portal-video-ffmpeg -f site/video/ffmpeg.Dockerfile site/video > /dev/null

for v in $variants; do
  # A fresh portal for each cut, so each starts from the same data.
  .venv/bin/python site/screenshots/demo_server.py "$scratch/data-$v" 8099 > "$scratch/portal-$v.log" 2>&1 &
  portal=$!
  for _ in $(seq 60); do curl -fs http://127.0.0.1:8099/healthz > /dev/null && break; sleep 0.5; done
  mkdir -p "$scratch/$v"
  docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD/site:/site:ro" -v "$scratch/$v:/work" \
    mcr.microsoft.com/playwright/python:v1.55.0-noble sh -c '
      pip install -q --target /tmp/pw playwright==1.55.0 2>/dev/null &&
      PYTHONPATH=/tmp/pw python /site/video/record.py http://127.0.0.1:8099 "$0" /work "$1"' "$v" "$repo"
  kill $portal; wait $portal 2>/dev/null || true; portal=
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    -v "$PWD/site/video:/video:ro" -v "$scratch/$v:/work" -v "$out:/out" \
    vm-portal-video-ffmpeg python3 /video/compose.py /work "/out/vm-portal-$v.mp4"
done
