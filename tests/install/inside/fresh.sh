#!/usr/bin/env bash
# Runs as root in a bare Ubuntu 24.04 with no Docker anywhere (see run-install-sh.sh).
set -uo pipefail
# shellcheck source=lib.sh
. /src/tests/install/inside/lib.sh

echo "== a bare Ubuntu with an ordinary user, like a fresh WSL distribution =="
make_tester
ok "ordinary user with sudo"

echo; echo "== first run of install.sh =="
as_tester "bash /src/install.sh --yes" >/tmp/run1.log 2>&1
rc=$?
show /tmp/run1.log
if [ "$rc" -eq 0 ]; then ok "install.sh exits 0"; else bad "install.sh exited $rc"; fi

echo; echo "== what it promised =="
check "Docker Engine is installed" command -v dockerd
check "the user is recorded in the docker group" bash -c "getent group docker | cut -d: -f4 | tr , '\n' | grep -qx tester"
check "the Docker daemon answers" docker info
check "the user can run a container in a new login" bash -c "su - tester -c 'docker run --rm hello-world' | grep -q 'Hello from Docker'"
check "the app is installed for the user" test -x /home/tester/.local/bin/odoo-edu-manager
check "the summary tells the user how to start it" grep -q "odoo-edu-manager" /tmp/run1.log
check "it said the group needs a new login" grep -qi "NEW login" /tmp/run1.log
data=/home/tester/.local/share/odoo-edu-manager
check "the shared database was prepared at install time" bash -c "[ -n \"\$(docker ps --filter name=odoo-manager-db --filter status=running -q)\" ]"
check "the list of Odoo versions was fetched at install time" bash -c "[ \"\$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))[\"versions\"]))' $data/available_versions.json)\" -ge 5 ]"
check "the code-server container is running" bash -c "[ -n \"\$(docker ps --filter name=odoo-manager-code-server --filter status=running -q)\" ]"
check "the code-server answers on port 8443" bash -c "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8443/ | grep -Eq '^(200|302)\$'"
check "its password was generated and kept for the Settings page" bash -c "python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get(\"code_server_password\") else 1)' $data/settings.json"
check "the summary says where the code-server is" grep -q "code-server:  http://localhost:8443" /tmp/run1.log
app_serves "after the first run"

echo; echo "== run it again: nothing to do twice =="
as_tester "bash /src/install.sh --yes" >/tmp/run2.log 2>&1
rc=$?
show /tmp/run2.log
if [ "$rc" -eq 0 ]; then ok "second run exits 0"; else bad "second run exited $rc"; fi
check "second run reuses the Docker that is now there" grep -q "Docker is working here" /tmp/run2.log
check "second run does not install Docker again" bash -c "! grep -q 'Installing Docker Engine' /tmp/run2.log"
check "second run finds uv already installed" grep -q "already installed" /tmp/run2.log
check "second run leaves the same shared database running" bash -c "[ \"\$(docker ps --filter name=odoo-manager-db --filter status=running -q | wc -l)\" = 1 ]"
check "second run leaves the same code-server running" bash -c "[ \"\$(docker ps --filter name=odoo-manager-code-server --filter status=running -q | wc -l)\" = 1 ]"
app_serves "after the second run"

finish
