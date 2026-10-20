#!/usr/bin/env bash
# Install smoke-test on a bare Ubuntu container — a stand-in for a fresh
# machine, and specifically for WSL2 (which is Ubuntu plus Docker
# Desktop's socket).
#
#   ./tests/install/run.sh              # default base domain (localhost)
#   ./tests/install/run.sh lvh.me       # the base domain macOS/Windows need
#
# Not run by pytest: it builds an image and needs the Docker socket.
#
# What this does NOT cover: Docker Desktop itself. Its daemon runs in a VM,
# so `--network host` and `localhost` behave differently there — but macOS
# cannot be containerised at all, and a container here talks to *this*
# Linux daemon. Those branches are pinned in tests/test_unit.py instead,
# against a faked client.info(). See tests/install/README.md.
set -euo pipefail

BASE_DOMAIN="${1:-localhost}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOCK=/var/run/docker.sock

[ -S "$SOCK" ] || { echo "No Docker socket at $SOCK"; exit 1; }

echo ">> building test image"
docker build -q -t odoo-edu-manager-install-test "$REPO/tests/install" >/dev/null

echo ">> running install check (base domain: $BASE_DOMAIN)"
exec docker run --rm \
  -v "$SOCK:$SOCK" \
  -v "$REPO:/src:ro" \
  -e "ODOO_EDU_MANAGER_BASE_DOMAIN=$BASE_DOMAIN" \
  odoo-edu-manager-install-test
