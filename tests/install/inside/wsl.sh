#!/usr/bin/env bash
# Runs as root in a bare Ubuntu 24.04 that pretends to be a WSL distribution WITHOUT
# systemd (what an older WSL distro looks like): WSL_DISTRO_NAME is set and /etc/wsl.conf
# already has settings of its own. install.sh must turn systemd on there without
# losing those settings, and still get Docker running in this session.
#
# It installs with --no-setup on purpose: nothing has pulled the database image yet, so
# the app's very first start - the one install.sh's own start-up check does - has to wait
# for that download. The other scenarios cover the setup; this one covers being patient.
set -uo pipefail
# shellcheck source=lib.sh
. /src/tests/install/inside/lib.sh

echo "== a WSL distribution without systemd and with a wsl.conf of its own =="
make_tester
printf '[user]\ndefault=tester\n\n[interop]\nenabled=true\n' > /etc/wsl.conf
ok "wsl.conf with the user's own settings"

echo; echo "== first run =="
as_tester "WSL_DISTRO_NAME=TestDistro bash /src/install.sh --yes --no-setup" >/tmp/run1.log 2>&1
rc=$?
show /tmp/run1.log
if [ "$rc" -eq 0 ]; then ok "install.sh exits 0"; else bad "install.sh exited $rc"; fi
check "Docker answers in this session even though systemd is off" docker info
check "wsl.conf now turns systemd on" grep -Eq '^systemd=true' /etc/wsl.conf
check "wsl.conf kept the user's own settings" bash -c "grep -q 'default=tester' /etc/wsl.conf && grep -q 'enabled=true' /etc/wsl.conf"
check "it says the distribution must restart (wsl --shutdown)" grep -q "wsl --shutdown" /tmp/run1.log
check "the app's first start brought the shared database up (the start-up check waited for it)" bash -c "[ -n \"\$(docker ps --filter name=odoo-manager-db --filter status=running -q)\" ]"
check "it did not fall back to a boot command that cannot work here" bash -c "! grep -q 'service docker start' /etc/wsl.conf"
app_serves "inside the pretend WSL"

echo; echo "== second run: wsl.conf is not touched again =="
before="$(cat /etc/wsl.conf)"
as_tester "WSL_DISTRO_NAME=TestDistro bash /src/install.sh --yes --no-setup" >/tmp/run2.log 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then ok "second run exits 0"; else bad "second run exited $rc"; fi
check "wsl.conf is unchanged" test "$before" = "$(cat /etc/wsl.conf)"
check "systemd=true appears exactly once" test "$(grep -c '^systemd=true' /etc/wsl.conf)" = 1

echo; echo "== a WSL1 distribution is refused before anything is installed =="
as_tester "WSL_DISTRO_NAME=TestDistro bash -c 'uname() { if [ \"\$1\" = -r ]; then echo 4.4.0-19041-Microsoft; else command uname \"\$@\"; fi; }; export -f uname; bash /src/install.sh --yes --no-check'" >/tmp/run3.log 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then ok "install.sh refuses (exit $rc)"; else bad "install.sh accepted WSL1"; fi
check "and explains how to move to WSL2" grep -q "wsl --set-version" /tmp/run3.log

finish
