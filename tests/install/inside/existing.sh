#!/usr/bin/env bash
# Runs as root in Ubuntu 24.04 where a working Docker is already reachable through
# the host's socket - what Docker Desktop's WSL integration looks like from inside.
set -uo pipefail
# shellcheck source=lib.sh
. /src/tests/install/inside/lib.sh

echo "== Docker already works for this user =="
make_tester
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d   # install packages, start nothing
apt-get install -y -qq --no-install-recommends docker.io >/dev/null
sock_gid="$(stat -c %g /var/run/docker.sock)"
group="$(getent group "$sock_gid" | cut -d: -f1 || true)"
if [ -z "$group" ]; then groupadd -g "$sock_gid" hostdocker; group=hostdocker; fi
usermod -aG "$group" tester
check "the user reaches the existing Docker" su - tester -c 'docker info'
before="$(pgrep -c dockerd || true)"

echo; echo "== install.sh must leave that Docker alone =="
as_tester "bash /src/install.sh --yes --no-code-server" >/tmp/run.log 2>&1
rc=$?
show /tmp/run.log
if [ "$rc" -eq 0 ]; then ok "install.sh exits 0"; else bad "install.sh exited $rc"; fi
check "it used the Docker that was there" grep -q "Docker is working here" /tmp/run.log
check "it did not install Docker Engine" bash -c "! grep -q 'Installing Docker Engine' /tmp/run.log"
check "it started no daemon of its own" bash -c "[ \"\$(pgrep -c dockerd || true)\" = \"$before\" ]"
check "it did not touch the docker group" bash -c "! grep -qi 'NEW login' /tmp/run.log"
check "the app is installed for the user" test -x /home/tester/.local/bin/odoo-edu-manager
check "the list of Odoo versions was still fetched" test -s /home/tester/.local/share/odoo-edu-manager/available_versions.json
check "the shared database step ran" grep -q "Preparing the shared database" /tmp/run.log
check "no code-server was started on that Docker" bash -c "! grep -q 'Starting code-server' /tmp/run.log"
app_serves "against the existing Docker"

finish
