#!/usr/bin/env bash
# Runs the real windows\*.ps1 against a pretend Windows (see windows-scripts.ps1), inside
# Microsoft's PowerShell container, so it works on any machine with Docker. It checks
# the logic, ordering and error handling of the installer; it cannot show how a real
# Windows behaves - run windows\install.cmd on one for that.
#
#   ./tests/install/run-windows-scripts.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec docker run --rm -v "$REPO:/repo:ro" mcr.microsoft.com/powershell:latest \
  pwsh -NoProfile -File /repo/tests/install/windows-scripts.ps1
