#!/bin/bash
# Runs inside the container. Installs the project the way the README tells
# a user to, then checks the things that can actually break on a machine
# that isn't the developer's: missing package data, missing entry points,
# a data directory that resolves somewhere unwritable, POSIX-only imports,
# and the per-instance hostnames that "Connect as" logs in through.
set -uo pipefail

pass=0; fail=0
ok()   { echo "  ok    $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

BASE_DOMAIN="${ODOO_EDU_MANAGER_BASE_DOMAIN:-localhost}"
PORT=5099

echo "== docker daemon reachable =="
docker version --format '{{.Server.Version}}' || { echo "  FAIL  no daemon (mount /var/run/docker.sock)"; exit 1; }
ok "docker CLI talks to the daemon"

echo
echo "== install from source, as documented =="
# The working tree, not HEAD — this is meant to catch packaging mistakes
# before they are committed. Copied to a writable path first: the build
# backend writes egg-info into the source tree, and /src is read-only.
# data/ is excluded because it holds multi-GB Odoo clones.
mkdir -p /tmp/src
tar -C /src -cf - \
    --exclude=./data --exclude=./venv --exclude=./build --exclude=./dist \
    --exclude='*.egg-info' --exclude=__pycache__ --exclude=./.git . | tar -C /tmp/src -xf -
uv tool install --force /tmp/src || { echo "  FAIL  uv tool install"; exit 1; }
ok "uv tool install succeeded"

echo
echo "== console scripts on PATH =="
check "odoo-edu-manager exists"     "command -v odoo-edu-manager"
check "odoo-edu-manager-mcp exists" "command -v odoo-edu-manager-mcp"

echo
echo "== package data shipped in the wheel =="
SITE=$(uv tool run --from odoo-edu-manager python -c 'import odoo_edu_manager,os;print(os.path.dirname(odoo_edu_manager.__file__))' 2>/dev/null \
       || find /root/.local/share/uv/tools -name 'odoo_edu_manager' -maxdepth 6 -type d | head -1)
echo "  package at: $SITE"
check "templates/ present"       "test -f '$SITE/templates/base.html'"
check "static/app.css present"   "test -f '$SITE/static/app.css'"
TPL_COUNT=$(ls "$SITE"/templates/*.html 2>/dev/null | wc -l)
if [ "$TPL_COUNT" -ge 12 ]; then ok "$TPL_COUNT templates shipped"; else bad "only $TPL_COUNT templates shipped"; fi

echo
echo "== data directory resolves off the checkout =="
DATA=$(odoo-edu-manager-data-dir 2>/dev/null || \
  "$(dirname "$(readlink -f "$(command -v odoo-edu-manager)")")/python" -c \
  'from odoo_edu_manager import docker_manager as dm; print(dm.DATA_DIR)' 2>/dev/null)
echo "  DATA_DIR: $DATA"
case "$DATA" in
  /root/.local/share/odoo-edu-manager) ok "DATA_DIR is the XDG location" ;;
  *)                               bad "DATA_DIR unexpected: $DATA" ;;
esac

echo
echo "== app boots and serves =="
ODOO_EDU_MANAGER_PORT=$PORT ODOO_EDU_MANAGER_BASE_DOMAIN="$BASE_DOMAIN" odoo-edu-manager >/tmp/app.log 2>&1 &
APP_PID=$!
for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 0.5
done

code() { curl -s -o /dev/null -w '%{http_code}' -H "Host: $2" "http://127.0.0.1:$PORT$1"; }
for route in / /new /settings /databases /help; do
  c=$(code "$route" "127.0.0.1:$PORT")
  [ "$c" = "200" ] && ok "GET $route -> 200" || bad "GET $route -> $c"
done

echo
echo "== per-instance hostnames (base domain: $BASE_DOMAIN) =="
# "Connect as" hands the session to the browser through a one-shot hop served on the instance's own host: it
# must set the cookie there, and only there (the manager's own host must never set one).
HDRS=$(curl -s -D - -o /dev/null -H "Host: demo.$BASE_DOMAIN:$PORT" "http://127.0.0.1:$PORT/_frame_bootstrap?sid=abc123&next=/web")
if printf '%s' "$HDRS" | grep -q "^HTTP/.* 302" && printf '%s' "$HDRS" | grep -qi "^set-cookie: session_id=abc123; Path=/"; then
  ok "demo.$BASE_DOMAIN sets the session cookie and redirects"
else
  bad "the cookie hop on demo.$BASE_DOMAIN did not behave: $(printf '%s' "$HDRS" | head -3 | tr '\r\n' '  ')"
fi
c=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: 127.0.0.1:$PORT" "http://127.0.0.1:$PORT/_frame_bootstrap?sid=abc123&next=/web")
[ "$c" = "404" ] && ok "the manager's own host does not serve the hop" || bad "the manager's own host answered the hop with $c"

kill $APP_PID 2>/dev/null; wait $APP_PID 2>/dev/null

echo
echo "== MCP server speaks stdio =="
# The trailing sleep matters: a stdio server shuts down on EOF, and
# closing the pipe immediately races the request it was just sent.
MCP_OUT=$({ printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 4; } \
  | timeout 30 odoo-edu-manager-mcp 2>/dev/null)
TOOLS=$(printf '%s' "$MCP_OUT" | grep -o '"inputSchema"' | wc -l)
if [ "$TOOLS" -ge 1 ]; then ok "MCP server listed $TOOLS tools over stdio"; else bad "MCP tools/list returned $TOOLS tools"; fi

echo
echo "======================================"
echo "  $pass passed, $fail failed"
echo "======================================"
[ "$fail" -eq 0 ]
