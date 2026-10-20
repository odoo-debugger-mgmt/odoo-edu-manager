# shellcheck shell=bash
# Shared by the scripts that run inside the test containers.
pass=0
fail=0
ok()    { echo "  ok    $1"; pass=$((pass + 1)); }
bad()   { echo "  FAIL  $1"; fail=$((fail + 1)); }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
as_tester() { su - tester -c "$1"; }
show()  { sed 's/^/    | /' "$1"; }
finish() { echo; echo "  $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }

# What a fresh WSL distribution already has: sudo, curl, certificates and a user with sudo.
make_tester() {
  apt-get update -qq >/dev/null
  apt-get install -y -qq --no-install-recommends sudo curl ca-certificates >/dev/null
  useradd --create-home --shell /bin/bash tester
  echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester
  chmod 0440 /etc/sudoers.d/tester
}

# The app answers on its pages. $1 = what to call it.
app_serves() {
  local port=5088 code
  # a new login, so the user has whatever group gives access to Docker
  as_tester "ODOO_EDU_MANAGER_PORT=$port ODOO_EDU_MANAGER_DATA=\$(mktemp -d) setsid /home/tester/.local/bin/odoo-edu-manager >/tmp/app.log 2>&1 &"
  for _ in $(seq 1 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/help")" = 200 ] && break
    sleep 1
  done
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/help")" != 200 ]; then
    echo "    | the app did not answer on port $port. Its log:"
    sed 's/^/    |   /' /tmp/app.log 2>/dev/null || true
  fi
  for path in / /new /settings /databases /help; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port$path")"
    if [ "$code" = 200 ]; then ok "$1: GET $path -> 200"; else bad "$1: GET $path -> $code"; fi
  done
  pkill -u tester -f odoo-edu-manager || true
}
