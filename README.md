# Odoo Edu Manager

A local Flask web app for running and managing multiple Docker-based Odoo
instances side by side — different versions, different databases, optional
Enterprise/design-themes addons, database backups restored into a fresh
instance, and VS Code debugging.

## Requirements

- **Docker**, daemon running, and your user able to run `docker` without
  `sudo` (i.e. in the `docker` group: `sudo usermod -aG docker $USER`, then
  log back in). The app shells out to the `docker` CLI directly in a few
  places, so the CLI binary needs to be on `PATH` in addition to the Docker
  daemon being reachable — a plain `docker ps` should work before you start.
- **Python 3.10+** (developed and tested on 3.12.3).
- **git**. An SSH key registered with GitHub is needed *only* if you tick
  "Include Odoo Enterprise" (unticked by default) without a local Enterprise
  folder — that clones the private `git@github.com:odoo/enterprise.git`. Design themes
  need nothing extra: `odoo/design-themes` is public and is cloned over
  HTTPS anonymously. Community-only instances need no GitHub access at all.
- A free TCP port for the manager app itself (`5001` by default) plus one
  per Odoo instance you run (auto-assigned, starting from `8069`).

Nothing else needs to be running beforehand — the app creates its own
shared Postgres container and Docker network on first use.

## Installation

### The quick way

| Where | Do this |
|---|---|
| **Windows 10 / 11** | Download this repository as a ZIP (the green *Code* button, then *Download ZIP*), unzip it, and double-click **`windows\install.cmd`**. It switches WSL on if needed (that takes one restart), sets up its own small Ubuntu with Docker, installs the app and adds *Odoo Edu Manager* to the Start menu. Remove it later from the Start menu (*Uninstall Odoo Edu Manager*) or Settings → Apps. More in [windows/README.md](windows/README.md). |
| **Ubuntu, Debian, WSL** | Run **`./install.sh`** in the downloaded folder. It installs whatever is missing — Docker Engine (unless Docker already works), access to Docker for your user, `uv`, the app — and checks that the app starts. Run it again to update. |

Either way, start the app with `odoo-edu-manager` (on Windows: the Start menu
entry) and open `http://localhost:5001`.

The install also does the app's first-run setup, so nothing is left to wait for on
the first visit: it prepares the shared database (a Postgres container every instance
uses; the app would otherwise download its image on the very first start, before it
shows a single page), fetches the list of Odoo versions (what the *Update available
versions* button does) and starts the managed code-server at `http://localhost:8443`
(its password is on the app's Settings page). The first time that downloads about
500 MB in all. `./install.sh --no-code-server` leaves the code-server out and
`--no-setup` skips the whole step — the app does or offers the same later. The step is
also available by itself as `odoo-edu-manager --setup`.

### By hand

From a checkout:

```bash
git clone https://github.com/odoo-debugger-mgmt/odoo-edu-manager.git
cd odoo-edu-manager
python3 -m venv venv
venv/bin/pip install -e .
venv/bin/odoo-edu-manager
```

Or as an installed tool (`uv`, or `pipx` the same way):

```bash
uv tool install git+ssh://git@github.com/odoo-debugger-mgmt/odoo-edu-manager.git
odoo-edu-manager
```

Then open `http://localhost:5001`. Update an installed copy later with
`uv tool upgrade odoo-edu-manager`.

Runtime state lives in `~/.local/share/odoo-edu-manager` (override with
`ODOO_EDU_MANAGER_DATA`). Running from a git checkout keeps using that
checkout's `data/` directory instead.

### Platform notes

**Linux** — works as above, nothing extra.

**macOS** — set a base domain whose wildcard resolves to loopback:

```bash
ODOO_EDU_MANAGER_BASE_DOMAIN=lvh.me odoo-edu-manager
```

The manager itself stays at `http://localhost:5001`; the instance links
(Open, Connect as) use `<db>.lvh.me`. Each instance is opened on its own
hostname so the instances don't share one cookie jar (cookies ignore ports,
so separate ports would not do). Linux resolves every `*.localhost` name to
127.0.0.1; macOS only resolves `localhost` itself, so those hostnames fail
there. `lvh.me` and `127-0-0-1.nip.io` are public domains that resolve to
127.0.0.1 — convenient, but they are someone else's DNS, and instance names
appear in DNS queries, so pick deliberately.

code-server needs no extra flags: the app detects Docker Desktop and
publishes a port instead of using host networking, pointing the debugger
at `host.docker.internal` rather than `localhost`.

**Windows** — use the installer above: the app runs in **WSL2** (Linux inside
Windows) and `windows\install.cmd` sets that up. Native Windows is not
supported: the in-browser Shell/Terminal is built on `pty`/`termios`/`fcntl`,
which are POSIX-only, so the app cannot even import. Docker Desktop is not
needed — the installer gives the app its own Ubuntu with Docker Engine. If you
already use Docker Desktop with WSL integration, run `./install.sh` inside that
Ubuntu instead: it uses the Docker that is already there.

### Options

| Variable | Default | Purpose |
|---|---|---|
| `ODOO_EDU_MANAGER_DATA` | `~/.local/share/odoo-edu-manager` | Where clones, workspaces, logs and settings live |
| `ODOO_EDU_MANAGER_HOST` | `127.0.0.1` | Bind address |
| `ODOO_EDU_MANAGER_PORT` | `5001` | Port |
| `ODOO_EDU_MANAGER_DEBUG` | off | Werkzeug debugger — see warning below |
| `ODOO_EDU_MANAGER_BASE_DOMAIN` | `localhost` | Base domain for the per-instance hostnames |

> The app can create containers, drop databases and execute code, and has
> no authentication of its own. It binds loopback for that reason. Do not
> put it on `0.0.0.0` on a shared network, and especially not with
> `ODOO_EDU_MANAGER_DEBUG` on — the Werkzeug debugger offers a remote console.

The first time you create an instance, the app will `docker pull` the
matching `odoo:<version>` image if it isn't already local — this can take a
few minutes per version depending on your connection.

All runtime state (uploaded backups, cloned Enterprise/design-themes/core
source, activity logs, generated VS Code workspaces) lives in the data
directory, created automatically on demand. There's no separate database
for the manager app itself — everything about an instance's configuration
is stored as Docker labels on its container.

## Testing

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest
```

Runs the fast suite: pure-logic tests (regexes, path parsing, backup
manifest handling, the session-minting script, the git worktree helpers)
plus a few Flask routes that don't need a live Odoo instance. Finishes in
a couple of seconds, but still needs a *reachable* Docker daemon — some of the tests
go through the app's Docker client, which connects the first time it is used. (The app
itself no longer needs Docker to start: it shows a page that explains.)

```bash
venv/bin/pytest -m slow tests/test_instance_versions.py -v
```

The real end-to-end check: creates a fresh, no-backup, community-only
instance for each of Odoo 15.0 through 19.0, polls it until `/web/login`
actually responds (proving the container booted *and* Odoo finished
auto-initializing a blank database), then tears it down. This is excluded
from the default `pytest` run because it's genuinely slow and heavy —
expect several minutes per version the first time (image pulls), less once
they're cached locally — so run it deliberately, not as part of a normal
edit-test loop.

```bash
./tests/install/run.sh
./tests/install/run.sh lvh.me
./tests/install/run-install-sh.sh       # install.sh in clean Ubuntu containers
./tests/install/run-windows-scripts.sh  # windows/*.ps1 against a pretend Windows
```

Installs the working tree into a bare `ubuntu:24.04` container with
`uv tool install` and smoke-tests the result: console scripts, templates
and static files actually shipped in the wheel, the data directory, the
HTTP routes, the per-instance cookie hop under the given base domain, and
an MCP stdio handshake. This is also the WSL2 check — WSL2 is this same
userland. It cannot cover macOS (no container can); see
[tests/install/README.md](tests/install/README.md).

A real Windows PC is tested by following [windows/AI-RUNBOOK.md](windows/AI-RUNBOOK.md) —
written for an AI agent, readable by a person — with `windows\verify.cmd` as the
read-only health check (a PASS/FAIL line per check).

## Project layout

```
odoo_edu_manager/
  app.py             Flask routes, the "Connect as" cookie hop, WebSocket
                       terminal/shell bridges
  docker_manager.py  All Docker/Postgres/git logic — the actual instance
                       lifecycle
  settings.py        Persisted preferences (editor target, code-server
                       password)
  mcp_server.py      Agent access over MCP (read-only: list_instances)
  templates/         Jinja templates (one per page)
  static/            app.css
tests/               pytest suite (see Testing above)
pyproject.toml       Packaging: dependencies and the two console scripts
```

The data directory (see Installation) holds runtime state only — clones,
uploads, logs, generated workspaces, settings. It is gitignored and safe
to delete entirely between runs; everything in it is either a cache or
regenerated on demand.
