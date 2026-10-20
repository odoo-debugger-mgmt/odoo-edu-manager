#!/usr/bin/env bash
# Odoo Edu Manager - installer for Ubuntu and Debian, including WSL2.
#
# Does every step that is otherwise manual: the base packages, Docker Engine
# (unless Docker already works here), Docker access for your user, `uv`, the app
# itself, the app's first-run setup (the shared database, the list of Odoo versions
# and the managed code-server), and a check that the app starts. It is safe to run again: it only
# does what is still missing and reinstalls the app from this folder, so running
# it again is also how you update.
#
#   ./install.sh [options]
#
#   -y, --yes          never ask; where Docker is missing, install Docker Engine here
#       --engine       install Docker Engine here even if a docker command is already there
#       --no-docker    skip everything about Docker (you provide it yourself)
#       --no-setup     skip the first-run setup (the database, the list of Odoo versions, the code-server)
#       --no-code-server  do the first-run setup but leave out the code-server (a ~350 MB download)
#       --no-check     do not start the app once at the end to check that it works
#       --source DIR   install the app from DIR instead of from this folder
#   -h, --help         show this text
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$HERE"
ASSUME_YES=0
FORCE_ENGINE=0
SKIP_DOCKER=0
RUN_CHECK=1
RUN_SETUP=1
WITH_CODE_SERVER=1
CODE_SERVER_UP=0
GROUP_ADDED=0
RESTART_WSL=0
DOCKER_NOTE=""

if [ -t 1 ]; then BOLD=$'\033[1m'; PLAIN=$'\033[0m'; else BOLD=""; PLAIN=""; fi
say()  { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$PLAIN"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)    ASSUME_YES=1 ;;
    --engine)    FORCE_ENGINE=1 ;;
    --no-docker) SKIP_DOCKER=1 ;;
    --no-setup)  RUN_SETUP=0 ;;
    --no-code-server) WITH_CODE_SERVER=0 ;;
    --no-check)  RUN_CHECK=0 ;;
    --source)    [ $# -ge 2 ] || die "--source needs a folder"; SOURCE="$(cd "$2" && pwd)"; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

is_wsl()      { [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; }
has_systemd() { [ -d /run/systemd/system ]; }
confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  [ -t 0 ] || return 1
  local answer
  read -r -p "    $1 [y/N] " answer
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# Runs a command with its output hidden, and shows that output only if it fails.
quietly() {
  local log
  log="$(mktemp)"
  if "$@" >"$log" 2>&1; then
    rm -f "$log"
  else
    cat "$log" >&2
    rm -f "$log"
    return 1
  fi
}

apt_get() { quietly sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get "$@"; }

preflight() {
  [ "$(uname -s)" = "Linux" ] || die "This installer is for Linux (Ubuntu or Debian, including WSL2). On Windows, run windows\\install.cmd instead."
  command -v apt-get >/dev/null 2>&1 || die "This installer supports Ubuntu and Debian (it needs apt-get). For other systems see the README."
  [ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not as root - it uses sudo for the steps that need it."
  if is_wsl && uname -r | grep -q 'Microsoft$'; then
    die "This is a WSL1 distribution, and Docker cannot run in WSL1. In Windows, run: wsl --set-version ${WSL_DISTRO_NAME:-<this distribution>} 2   and then run this again."
  fi
  [ -f "$SOURCE/pyproject.toml" ] || die "There is no pyproject.toml in $SOURCE - run this from the folder you downloaded, or pass --source."
  command -v sudo >/dev/null 2>&1 || die "sudo is not installed. As root, run: apt-get install -y sudo   and add your user to the sudo group."
  if ! sudo -n true 2>/dev/null; then
    [ -t 0 ] || die "sudo needs a password and there is no terminal to ask for it. Run this in a terminal, or give your user passwordless sudo."
    info "sudo will ask for your password once."
    sudo -v || die "sudo did not accept the password."
  fi
}

install_base_packages() {
  say "Installing base packages (git, curl, python, certificates)"
  apt_get update -qq
  apt_get install -y -qq --no-install-recommends ca-certificates curl git python3
}

# --- Docker ------------------------------------------------------------------

docker_ok()       { docker info >/dev/null 2>&1; }
docker_ok_root()  { sudo docker info >/dev/null 2>&1; }
docker_ok_group() { command -v sg >/dev/null 2>&1 && sg docker -c 'docker info' >/dev/null 2>&1; }

# Three ways, in order of preference: systemd; an init script (older packages ship
# one, Ubuntu 24.04's docker.io does not); or running the daemon ourselves.
start_docker() {
  if has_systemd; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo systemctl start docker >/dev/null 2>&1 || true
  elif [ -x /etc/init.d/docker ]; then
    sudo service docker start >/dev/null 2>&1 || true
  else
    info "No systemd here, so starting the Docker daemon directly (log: /var/log/dockerd.log)."
    sudo sh -c 'nohup dockerd >>/var/log/dockerd.log 2>&1 &'
  fi
}

wait_for_docker() {
  local _
  for _ in $(seq 1 40); do
    docker_ok_root && return 0
    sleep 1
  done
  return 1
}

ensure_docker_group() {
  local me
  me="$(id -un)"
  getent group docker >/dev/null 2>&1 || sudo groupadd docker
  if ! id -nG "$me" | tr ' ' '\n' | grep -qx docker; then
    if ! getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$me"; then
      sudo usermod -aG docker "$me"
    fi
    GROUP_ADDED=1   # recorded now, but this shell only gets it with the next login
  fi
}

# Without systemd nothing starts Docker when a WSL distribution starts. Switching
# systemd on (WSL's own, documented setting) fixes that for good, because Docker's
# package ships a systemd unit; it takes effect when the distribution restarts.
wsl_enable_systemd() {
  is_wsl || return 0
  has_systemd && return 0
  local conf=/etc/wsl.conf
  if [ -f "$conf" ] && grep -Eq '^[[:space:]]*systemd[[:space:]]*=' "$conf"; then
    info "/etc/wsl.conf already sets systemd - left as it is."
    return 0
  fi
  if [ -f "$conf" ] && grep -q '^\[boot\]' "$conf"; then
    sudo sed -i '/^\[boot\]/a systemd=true' "$conf"
  else
    printf '\n[boot]\nsystemd=true\n' | sudo tee -a "$conf" >/dev/null
  fi
  RESTART_WSL=1
  info "Turned systemd on in /etc/wsl.conf so that Docker starts with this distribution from now on."
}

ensure_docker() {
  say "Checking Docker"
  if [ "$SKIP_DOCKER" -eq 1 ]; then
    info "skipped (--no-docker)"
    return 0
  fi

  if [ "$FORCE_ENGINE" -eq 0 ] && docker_ok; then
    info "Docker is working here ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'version unknown')) - using it."
    DOCKER_NOTE="the Docker that was already here"
    return 0
  fi

  if command -v dockerd >/dev/null 2>&1; then
    info "Docker Engine is installed here but not answering - starting it."
  elif command -v docker >/dev/null 2>&1 && [ "$FORCE_ENGINE" -eq 0 ]; then
    # A docker command with no engine behind it: Docker Desktop's WSL integration,
    # with Docker Desktop not running or the integration switched off.
    warn "There is a docker command here but nothing answers. If you use Docker Desktop: start it, switch on"
    warn "Settings > Resources > WSL integration for this distribution, then run this installer again."
    warn "To install Docker Engine inside this Linux instead, run: ./install.sh --engine"
    DOCKER_NOTE="Docker Desktop is not answering yet"
    return 0
  else
    if is_wsl && command -v docker.exe >/dev/null 2>&1; then
      info "Docker Desktop is installed on Windows but is not connected to this Linux."
      if ! confirm "Install a separate Docker Engine inside this Linux?"; then
        warn "Skipped. Switch on Docker Desktop's WSL integration for this distribution, or run: ./install.sh --yes"
        DOCKER_NOTE="Docker Desktop is not connected to this distribution"
        return 0
      fi
    fi
    say "Installing Docker Engine"
    apt_get install -y -qq docker.io
  fi

  start_docker
  ensure_docker_group
  wait_for_docker || die "Docker is installed but is not answering. See: sudo journalctl -u docker   or   /var/log/dockerd.log"
  if docker_ok || docker_ok_group; then
    info "Docker Engine is running and your user can use it."
  else
    warn "Docker is running, but your user cannot use it yet - that takes effect with your next login."
  fi
  DOCKER_NOTE="Docker Engine, installed here"
  wsl_enable_systemd
}

# --- uv and the app ----------------------------------------------------------

ensure_uv() {
  say "Checking uv (the Python tool installer)"
  export PATH="$HOME/.local/bin:$PATH"
  if command -v uv >/dev/null 2>&1; then
    info "uv $(uv --version | awk '{print $2}') is already installed."
    return 0
  fi
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "Could not install uv (it needs internet access to astral.sh)."
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv was installed but is not at $HOME/.local/bin/uv."
}

install_app() {
  say "Installing Odoo Edu Manager"
  local tmp
  tmp="$(mktemp -d)"
  # Built from a copy: building writes into the folder it builds, and this one
  # may be read-only or on a slow Windows drive (/mnt/c).
  ( cd "$SOURCE" && tar --exclude=./.git --exclude=./venv --exclude=./data --exclude=./build \
      --exclude=./dist --exclude='*.egg-info' --exclude=__pycache__ -cf - . ) | tar -C "$tmp" -xf -
  if ! quietly uv tool install --force "$tmp"; then
    rm -rf "$tmp"
    die "Installing the app failed - see the messages above."
  fi
  rm -rf "$tmp"
  info "Installed odoo-edu-manager and odoo-edu-manager-mcp into ~/.local/bin."
  uv tool update-shell >/dev/null 2>&1 || true
}

# --- first-run setup ---------------------------------------------------------

# What the app does once for a new install: prepare the shared database (the app does that
# itself before it serves its first page, so doing it here keeps the first start quick),
# fetch the list of Odoo versions ("Update available versions") and start the managed
# code-server. None is essential and the app does or offers each later, so a failure here
# only warns.
run_first_setup() {
  [ "$RUN_SETUP" -eq 1 ] || return 0
  say "First-run setup: the shared database, the list of Odoo versions and the code-server"
  local app="$HOME/.local/bin/odoo-edu-manager" cmd status=0
  local args=(--setup)
  if [ "$WITH_CODE_SERVER" -eq 0 ] || [ "$SKIP_DOCKER" -eq 1 ]; then
    args+=(--skip-code-server)
    info "The first time this downloads about 170 MB (the database), so it can take a minute or two."
  else
    info "The first time this downloads about 500 MB (the database and the code-server), so it can take a few minutes."
  fi
  [ -x "$app" ] || die "$app is missing after the install."
  cmd="$(printf '%q ' "$app" "${args[@]}")"
  # A user who was only just added to the docker group does not have it in this shell yet.
  if [ "$SKIP_DOCKER" -eq 0 ] && ! docker_ok && docker_ok_group; then
    sg docker -c "$cmd" || status=$?
  else
    "$app" "${args[@]}" || status=$?
  fi
  if [ "$status" -eq 0 ]; then
    [ "${args[*]}" = "--setup" ] && CODE_SERVER_UP=1
  else
    warn "Some of the first-run setup was skipped (see above). The app's own pages can do it later."
  fi
  return 0
}

# --- a last check ------------------------------------------------------------

free_port() {
  local p
  for p in $(seq 5090 5199); do
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null || { echo "$p"; return 0; }
  done
  echo 5099
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" || true; }

check_app() {
  [ "$RUN_CHECK" -eq 1 ] || return 0
  say "Checking that the app starts"
  local port data pid code app
  port="$(free_port)"
  data="$(mktemp -d)"
  app="$HOME/.local/bin/odoo-edu-manager"
  [ -x "$app" ] || die "$app is missing after the install."

  # A user who was only just added to the docker group does not have it in this shell yet.
  if [ "$SKIP_DOCKER" -eq 0 ] && ! docker_ok && docker_ok_group; then
    setsid sg docker -c "exec env ODOO_EDU_MANAGER_PORT=$port ODOO_EDU_MANAGER_DATA=$data '$app'" >"$data/app.log" 2>&1 &
  else
    setsid env ODOO_EDU_MANAGER_PORT="$port" ODOO_EDU_MANAGER_DATA="$data" "$app" >"$data/app.log" 2>&1 &
  fi
  pid=$!

  # The app prepares the shared database before it serves its first page, and on a machine
  # that never did (--no-setup, or a setup that was cut short) that means waiting for Docker
  # to download Postgres. So: patient, but not blind - give up at once if the app is gone.
  code=000
  for i in $(seq 1 180); do
    code="$(http_code "http://127.0.0.1:$port/help")"
    [ "$code" = "200" ] && break
    kill -0 "$pid" 2>/dev/null || break
    [ "$i" -eq 30 ] && info "Still starting - the first start downloads the database image, which can take a few minutes."
    sleep 1
  done
  if [ "$code" != "200" ]; then
    kill -- "-$pid" 2>/dev/null || true
    warn "The app did not answer. Its log:"
    tail -n 25 "$data/app.log" >&2 || true
    rm -rf "$data"
    die "The app did not start."
  fi
  info "The app starts and serves its pages."

  code="$(http_code "http://127.0.0.1:$port/")"
  if [ "$code" = "200" ]; then
    info "The dashboard loads: the app can talk to Docker."
  elif [ "$SKIP_DOCKER" -eq 1 ]; then
    info "The dashboard needs Docker, which you chose to provide yourself."
  else
    warn "The dashboard says Docker is not reachable yet - the page explains what to do. ${DOCKER_NOTE:+($DOCKER_NOTE)}"
  fi
  kill -- "-$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  rm -rf "$data"
}

summary() {
  printf '\n%sInstalled.%s\n\n' "$BOLD" "$PLAIN"
  info "Start it:     odoo-edu-manager"
  info "Then open:    http://localhost:5001"
  if is_wsl; then
    info "              (in your normal Windows browser - WSL forwards localhost)"
  fi
  if [ "$CODE_SERVER_UP" -eq 1 ]; then
    info "code-server:  http://localhost:8443  (its password is on the app's Settings page)"
  fi
  if [ "$GROUP_ADDED" -eq 1 ] || [ "$RESTART_WSL" -eq 1 ]; then
    printf '\n'
    if [ "$GROUP_ADDED" -eq 1 ]; then
      info "Your user was just given access to Docker. That takes effect with a NEW login."
    fi
    if is_wsl; then
      info "Close this terminal, run 'wsl --shutdown' in Windows, then open it again."
    else
      info "Log out and back in (or open a new terminal session) before starting the app."
    fi
  fi
  if ! command -v odoo-edu-manager >/dev/null 2>&1; then
    printf '\n'
    info "If 'odoo-edu-manager' is not found, open a new terminal: uv added ~/.local/bin to your PATH."
  fi
  if [ -n "$DOCKER_NOTE" ]; then
    printf '\n'
    info "Docker: $DOCKER_NOTE."
  fi
  printf '\n'
}

preflight
install_base_packages
ensure_docker
ensure_uv
install_app
run_first_setup
check_app
summary
