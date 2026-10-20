#!/usr/bin/env bash
# Runs INSIDE the dedicated WSL distribution, as root, every time windows/install.ps1
# installs or updates (it is safe to run again). It prepares the distribution for
# the app and then runs the normal ../install.sh as an ordinary user.
#
#   provision.sh <folder holding the app, as a Linux path> [user name, default: edu]
set -euo pipefail

STAGE="${1:?usage: provision.sh <folder holding the app> [user]}"
APP_USER="${2:-edu}"

say() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "provision.sh must run as root" >&2; exit 1; }
[ -f "$STAGE/install.sh" ] || { echo "there is no install.sh in $STAGE" >&2; exit 1; }

say "Preparing the Linux distribution"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
apt-get update -qq
apt-get install -y -qq --no-install-recommends sudo ca-certificates curl python3

say "Creating the user '$APP_USER'"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$APP_USER"
echo "$APP_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$APP_USER"
chmod 0440 "/etc/sudoers.d/90-$APP_USER"

say "Setting WSL up for it"
# Merge into the file that is there (Ubuntu's image already turns systemd on) rather than replace it.
python3 - "$APP_USER" <<'PY'
import configparser
import sys

user = sys.argv[1]
conf = configparser.ConfigParser(delimiters=("=",), interpolation=None)
conf.optionxform = str
conf.read("/etc/wsl.conf")
for section, key, value in (("user", "default", user), ("interop", "appendWindowsPath", "false")):
    if not conf.has_section(section):
        conf.add_section(section)
    conf.set(section, key, value)
with open("/etc/wsl.conf", "w") as handle:
    conf.write(handle)
PY
# The user exists, so the first launch must not ask for one.
if [ -f /etc/wsl-distribution.conf ]; then
  sed -i '/^\[oobe\]/,/^$/d' /etc/wsl-distribution.conf
fi

say "Installing Docker and the app as '$APP_USER'"
su - "$APP_USER" -c "bash '$STAGE/install.sh' --yes --source '$STAGE'"
