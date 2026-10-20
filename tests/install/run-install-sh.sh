#!/usr/bin/env bash
# Tests ../../install.sh in a clean Ubuntu 24.04 as an ordinary user with sudo - the
# situation of a fresh WSL distribution. Three scenarios:
#
#   fresh      no Docker anywhere: install.sh must install Docker Engine, give the user
#              access, install the app, start it - and a second run must be harmless
#   existing   a working Docker is already reachable (the host's, through its socket:
#              what Docker Desktop's WSL integration looks like): install.sh must leave it
#              alone and still install the app
#   wsl        a bare Ubuntu pretending to be an older WSL distribution (no systemd, a
#              wsl.conf of its own): systemd must be switched on without losing the user's
#              settings, and a WSL1 distribution must be refused. Installed with --no-setup,
#              so the start-up check has to wait out the app's first start (the database
#              image download)
#
#   ./tests/install/run-install-sh.sh [fresh|existing|wsl|all]
#
# Not part of pytest: it needs this machine's Docker and the internet, and the fresh
# scenario runs a privileged container so that a Docker inside it can start.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${IMAGE:-ubuntu:24.04}"
WHICH="${1:-all}"
failed=0

if [ "$WHICH" = fresh ] || [ "$WHICH" = all ]; then
  echo "##### scenario: fresh (no Docker anywhere)"
  docker run --rm --privileged -v "$REPO:/src:ro" -v /var/lib/docker "$IMAGE" \
    bash /src/tests/install/inside/fresh.sh || failed=1
fi

if [ "$WHICH" = existing ] || [ "$WHICH" = all ]; then
  echo; echo "##### scenario: existing (a working Docker is already reachable)"
  docker run --rm -v "$REPO:/src:ro" -v /var/run/docker.sock:/var/run/docker.sock "$IMAGE" \
    bash /src/tests/install/inside/existing.sh || failed=1
fi

if [ "$WHICH" = wsl ] || [ "$WHICH" = all ]; then
  echo; echo "##### scenario: wsl (older WSL distribution: no systemd, own wsl.conf)"
  docker run --rm --privileged -v "$REPO:/src:ro" -v /var/lib/docker "$IMAGE" \
    bash /src/tests/install/inside/wsl.sh || failed=1
fi

echo
if [ "$failed" -eq 0 ]; then echo "ALL SCENARIOS PASSED"; else echo "SOME SCENARIOS FAILED"; exit 1; fi
