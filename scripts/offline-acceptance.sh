#!/usr/bin/env sh
# Reproduce acceptance-report.txt with the network switched off.
#
# Boots the portal inside a container that has no network interface except
# loopback (--network none), then runs the unmodified official checker
# (run.py) against it with the repository's .dogfood.toml. No host port is
# published, so this works even when 8080 is taken on your machine.
#
#   docker compose build portal
#   sh scripts/offline-acceptance.sh > acceptance-report.txt
set -eu
cd "$(dirname "$0")/.."
REPO="$(pwd)"

docker run --rm --network none -e DOGFOOD_DEMO=1 \
  -v "$REPO/run.py:/repo/run.py:ro" \
  -v "$REPO/.dogfood.toml:/repo/.dogfood.toml:ro" \
  -v "$REPO/fixtures.json:/repo/fixtures.json:ro" \
  doggfather:latest sh -c '
    python -m doggfather serve >/tmp/portal.log 2>&1 &
    for i in $(seq 1 60); do
      python -c "import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8080/healthz\", timeout=1)" 2>/dev/null && break
      sleep 0.5
    done
    cd /repo && python run.py .dogfood.toml'
