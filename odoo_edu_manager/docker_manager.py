import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile

import docker
import requests

MANAGED_LABEL = "app"
MANAGED_VALUE = "odoo-manager"
NETWORK_NAME = "odoo-manager-net"
DB_CONTAINER_NAME = "odoo-manager-db"
DB_USER = "odoo"
DB_PASSWORD = "odoo"
# Odoo 20.0 hard-requires Postgres 16+ (MIN_PG_VERSION in its own release.py,
# confirmed by pulling odoo:20.0 and reading it directly) — below that,
# `_recompute_user_share()`'s own query fails outright at base-module install
# time, since it uses any_value(), a PG16 aggregate, not just a soft warning.
# 17 clears that with headroom for the next version's bump too. Moving an
# *existing* odoo-manager-db off an older image needs a real pg_dumpall +
# restore first — Postgres data directories aren't binary-compatible across
# major versions, so this constant alone only governs a *fresh* container
# (see ensure_shared_db); see CLAUDE.md for the migration this one took.
DB_IMAGE = "postgres:17"

def _resolve_data_dir():
    """Where enterprise clones, addons copies, workspaces, activity logs
    and settings live.

    Must not sit next to the code: once this is pip-installed that would
    be inside site-packages, and every upgrade would silently discard
    multi-gigabyte clones. Order is explicit override, then an existing
    checkout-local data/ (so anyone who ran this from a git clone keeps
    their data without a migration step), then the XDG location."""
    override = os.environ.get("ODOO_EDU_MANAGER_DATA")
    if override:
        return os.path.abspath(override)
    legacy = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    if os.path.isdir(legacy):
        return legacy
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(xdg, "odoo-edu-manager")


DATA_DIR = _resolve_data_dir()

ODOO_CORE_CACHE_DIR = os.path.join(DATA_DIR, "odoo-core")

ENTERPRISE_REPO_SSH = "git@github.com:odoo/enterprise.git"
ENTERPRISE_CACHE_DIR = os.path.join(DATA_DIR, "enterprise")

# HTTPS, not SSH: odoo/design-themes is a public repo, and GitHub refuses
# anonymous SSH even for public repos ("Permission denied (publickey)"),
# so cloning it over SSH would demand a registered key for nothing.
# Enterprise stays on SSH because it is private.
DESIGN_THEMES_REPO = "https://github.com/odoo/design-themes.git"
DESIGN_THEMES_CACHE_DIR = os.path.join(DATA_DIR, "design-themes")

# odoo:<version> Docker images are official but not rebuilt on every commit
# to that branch — confirmed directly (see ensure_odoo_core_addons_source)
# that an image's own bundled core addons can be missing a file the branch
# has right now, which a newer Enterprise module then assumes exists,
# crashing the whole registry with no way to fix it short of patching the
# database by hand. Also public, also HTTPS for the same reason as
# design-themes above.
ODOO_CORE_REPO = "https://github.com/odoo/odoo.git"
ODOO_CORE_ADDONS_CACHE_DIR = os.path.join(DATA_DIR, "odoo-core-addons")

# Versions this has actually been booted end-to-end on, not just cloned —
# see _run_container. NOT a lazy default: tested and *fixed* 16.0 (an
# Enterprise module wanting a base_setup file the image's core addons
# didn't have — see ensure_odoo_core_addons_source), then, checking
# rather than assuming it generalizes, tried 19.0 and hit a completely
# different, deeper mismatch (odoo/orm/registry.py doesn't even exist in
# 16.0 — the internal module layout moved between these versions, not
# just addons content) that the same fix does not resolve and this
# hasn't chased down. Odoo's packaging has clearly changed shape across
# versions in more than one way (see also _odoo_addons_init_py, which
# already has to special-case 16.0 vs 19.0 in the *opposite* direction
# for the one difference that IS understood) — enabling this for every
# version on the strength of one fix working would be extending a fix
# to versions it was never shown to work on, with real instances (10.0
# through 19.0+ are all in active use) it could break instead of help.
# Add a version here only after it's been verified the same way 16.0
# was: a real instance, a real module install that exercises it,
# booted clean.
CORE_ADDONS_REFRESH_VERIFIED_VERSIONS = {"16.0"}

ADDONS_COPY_CACHE_DIR = os.path.join(DATA_DIR, "addons-copies")
ADDONS_WORKTREE_CACHE_DIR = os.path.join(DATA_DIR, "addons-worktrees")

WORKSPACE_DIR = os.path.join(DATA_DIR, "workspaces")
ACTIVITY_LOG_DIR = os.path.join(DATA_DIR, "activity-logs")

AVAILABLE_VERSIONS_CACHE = os.path.join(DATA_DIR, "available_versions.json")
DOCKER_HUB_TAGS_URL = "https://hub.docker.com/v2/repositories/library/odoo/tags?page_size=100"
_CLEAN_VERSION_RE = re.compile(r"^\d+\.0$")

class _LazyDockerClient:
    """The Docker client, built on first use instead of at import.

    docker.from_env() contacts the daemon the moment it is built, so building it
    at import made the whole app die with a traceback whenever Docker was not
    running yet — the normal state just after a reboot, above all on WSL. Built
    on first use, a missing daemon is an ordinary error inside a request, which
    the app can explain; a failed attempt is not kept, so every later request
    tries again until Docker is up."""

    def __init__(self):
        self._real = None

    def _connect(self):
        if self._real is None:
            self._real = docker.from_env()
        return self._real

    def __getattr__(self, name):
        return getattr(self._connect(), name)


client = _LazyDockerClient()

# In-memory status tracker for long-running restore jobs, keyed by container name.
_restore_status = {}

# One append-only log file per instance, capturing everything the *manager*
# does on its behalf — image pulls, Enterprise/design-themes git clones,
# the actual restore steps (with real command output, not just the terse
# status string) — as opposed to the instance's own `docker logs`, which
# only shows what Odoo itself prints. Persisted to disk (unlike
# _restore_status) so it survives an app restart.
_activity_log_locks = {}


def _activity_log_path(name):
    return os.path.join(ACTIVITY_LOG_DIR, f"{name}.log")


def log_activity(name, message):
    os.makedirs(ACTIVITY_LOG_DIR, exist_ok=True)
    lock = _activity_log_locks.setdefault(name, threading.Lock())
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with lock:
        with open(_activity_log_path(name), "a") as f:
            f.write(line)


def get_activity_log(name):
    path = _activity_log_path(name)
    if not os.path.isfile(path):
        return ""
    with open(path) as f:
        return f.read()


def stream_activity_log(name):
    """Yield the existing log, then poll for new lines as they're appended —
    a plain file, potentially written by other threads (restore, pulls,
    clones), so this just tails it rather than assuming a live handle."""
    path = _activity_log_path(name)
    pos = 0
    if os.path.isfile(path):
        with open(path) as f:
            content = f.read()
            pos = f.tell()
        if content:
            yield content
    for _ in range(3600):  # ~30 min at 0.5s poll; the SSE client reconnects anyway
        time.sleep(0.5)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            f.seek(pos)
            new = f.read()
            pos = f.tell()
        if new:
            yield new


def ensure_network():
    try:
        client.networks.get(NETWORK_NAME)
    except docker.errors.NotFound:
        client.networks.create(NETWORK_NAME, driver="bridge")


def ensure_shared_db():
    ensure_network()
    try:
        c = client.containers.get(DB_CONTAINER_NAME)
        if c.status != "running":
            c.start()
        return c
    except docker.errors.NotFound:
        c = client.containers.run(
            DB_IMAGE,
            name=DB_CONTAINER_NAME,
            environment={
                "POSTGRES_USER": DB_USER,
                "POSTGRES_PASSWORD": DB_PASSWORD,
                "POSTGRES_DB": "postgres",
            },
            network=NETWORK_NAME,
            detach=True,
            labels={MANAGED_LABEL: MANAGED_VALUE, "role": "db"},
        )
        return c


CODE_SERVER_CONTAINER_NAME = "odoo-manager-code-server"
CODE_SERVER_IMAGE = "linuxserver/code-server:latest"
CODE_SERVER_PORT = 8443
CODE_SERVER_CONFIG_VOLUME = "odoo-manager-code-server-config"

# Without these the debugpy 'attach' config in every generated workspace
# fails with "Configured debug type 'debugpy' is not supported", which
# reads like the config is wrong rather than an extension being absent.
# Both are on Open VSX, so no sideloading is involved.
CODE_SERVER_EXTENSIONS = ("ms-python.python", "ms-python.debugpy")
CODE_SERVER_FALLBACK_EXTENSIONS_DIR = "/config/extensions"


def host_networking_supported():
    """Whether `--network host` actually shares the host's network stack.

    True on native Linux docker. On Docker Desktop (macOS, Windows) the
    daemon runs inside a VM, so host networking means *the VM's* network,
    not the Mac's — a container could not reach a debugpy port published
    on the host that way. Detected from the daemon rather than the client
    platform, since the client may be a Linux CLI talking to Desktop."""
    try:
        info = client.info()
    except Exception:
        return False
    return info.get("OSType") == "linux" and "desktop" not in (info.get("OperatingSystem") or "").lower()


def _code_server_network_kwargs():
    """Host networking where it's real, published ports otherwise.

    The debug story differs accordingly: with host networking the
    generated 'attach' config's localhost:<port> resolves as it does for
    a desktop editor. On Docker Desktop it can't, so the workspace
    generator points the attach at host.docker.internal instead — see
    debug_attach_host()."""
    if host_networking_supported():
        return {"network_mode": "host"}
    return {"ports": {f"{CODE_SERVER_PORT}/tcp": ("127.0.0.1", CODE_SERVER_PORT)}}


def debug_attach_host():
    """Hostname a debugger running inside code-server should attach to.

    'localhost' only reaches the instance's published debug port when
    code-server shares the host's network stack; behind Docker Desktop
    the container needs the special host.docker.internal name instead."""
    return "localhost" if host_networking_supported() else "host.docker.internal"


def _code_server_extensions_dir(container):
    """The extensions directory the running server actually reads.

    Read back off the server process rather than assumed: this image
    starts code-server with an explicit --extensions-dir, which is *not*
    the CLI's default. Installing without matching it silently puts
    extensions where the server never looks — they show up in
    --list-extensions and still don't load."""
    script = r'''
for p in /proc/[0-9]*/cmdline; do
  if tr "\0" "\n" < "$p" 2>/dev/null | grep -qx -- "--bind-addr"; then
    tr "\0" "\n" < "$p" | grep -A1 -x -- "--extensions-dir" | tail -1
    exit 0
  fi
done
'''
    try:
        out = container.exec_run(["sh", "-c", script]).output.decode(errors="replace").strip()
    except Exception:
        out = ""
    return out if out.startswith("/") else CODE_SERVER_FALLBACK_EXTENSIONS_DIR


def install_code_server_extensions(container=None):
    """Install the Python + debugpy extensions into the running server's
    own extensions directory. Idempotent — already-installed extensions
    are reported and skipped. Returns the per-extension output lines."""
    c = container or client.containers.get(CODE_SERVER_CONTAINER_NAME)
    ext_dir = _code_server_extensions_dir(c)
    results = []
    for ext in CODE_SERVER_EXTENSIONS:
        res = c.exec_run(
            ["/app/code-server/bin/code-server", "--extensions-dir", ext_dir, "--install-extension", ext],
            user=str(os.getuid()),
        )
        tail = res.output.decode(errors="replace").strip().splitlines()
        results.append(tail[-1] if tail else f"{ext}: no output")
    return results


def code_server_status():
    """State of the managed code-server container: absent / exited /
    running, plus the URL it serves on when it's up."""
    try:
        c = client.containers.get(CODE_SERVER_CONTAINER_NAME)
    except docker.errors.NotFound:
        return {"state": "absent", "url": None}
    return {
        "state": c.status,
        "url": f"http://localhost:{CODE_SERVER_PORT}/" if c.status == "running" else None,
    }


def ensure_code_server(password):
    """Create (or just start) the shared code-server container — the same
    shape as ensure_shared_db, since it's the same kind of thing: one
    long-lived support container the manager owns.

    Two deliberate choices, both about making the workspaces this app
    already generates work unmodified:

    * DATA_DIR is mounted at its *own absolute path*. Every folder in a
      generated .code-workspace lives under it, so mounting it identically
      means the workspace file resolves inside the container exactly as it
      does on the host — no path rewriting, and no rewriting of the
      debugger's pathMappings either.
    * Host networking, so the debugpy 'attach' config (which points at
      localhost:<mapped debug port>) resolves the same way it does for a
      desktop VS Code. On a bridge network 'localhost' would be the
      code-server container itself and every attach would fail.

    Host networking does mean code-server is reachable on every interface,
    hence the mandatory password — without one this is an unauthenticated
    shell as the host user."""
    if not password:
        raise ValueError("code-server needs a password (host networking exposes it beyond localhost)")
    try:
        c = client.containers.get(CODE_SERVER_CONTAINER_NAME)
        if c.status != "running":
            c.start()
        return c
    except docker.errors.NotFound:
        pass

    if CODE_SERVER_IMAGE not in [t for img in client.images.list() for t in img.tags]:
        client.images.pull(CODE_SERVER_IMAGE)

    container = client.containers.run(
        CODE_SERVER_IMAGE,
        name=CODE_SERVER_CONTAINER_NAME,
        environment={
            # The host user is uid 10000 here, not the 1000 these images
            # usually assume; without PUID/PGID the bind mount below is
            # unwritable from inside.
            "PUID": str(os.getuid()),
            "PGID": str(os.getgid()),
            "PASSWORD": password,
            "DEFAULT_WORKSPACE": WORKSPACE_DIR,
        },
        volumes={
            DATA_DIR: {"bind": DATA_DIR, "mode": "rw"},
            CODE_SERVER_CONFIG_VOLUME: {"bind": "/config", "mode": "rw"},
        },
        **_code_server_network_kwargs(),
        restart_policy={"Name": "unless-stopped"},
        detach=True,
        labels={MANAGED_LABEL: MANAGED_VALUE, "role": "code-server"},
    )

    # Install on first creation so a fresh container is usable immediately
    # rather than greeting you with "debug type 'debugpy' is not
    # supported" the first time you try to attach. The server needs a
    # moment to come up before it will accept the CLI; failing here isn't
    # fatal, since Settings has a button to retry.
    for _ in range(30):
        if _code_server_http_ok():
            break
        time.sleep(2)
    try:
        install_code_server_extensions(container)
    except Exception as e:
        log_activity("code-server", f"extension install failed (retry from Settings): {e}")
    return container


def _code_server_http_ok():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            return s.connect_ex(("127.0.0.1", CODE_SERVER_PORT)) == 0
    except OSError:
        return False


def stop_code_server():
    try:
        client.containers.get(CODE_SERVER_CONTAINER_NAME).stop()
    except docker.errors.NotFound:
        pass


def list_odoo_images():
    versions = set()
    for image in client.images.list():
        for tag in image.tags:
            if tag.startswith("odoo:"):
                versions.add(tag.split(":", 1)[1])
    # Numerically, not as text: callers take the last one as "the highest",
    # and as text 9.0 sorts after 20.0.
    return sorted(versions, key=_version_sort_key)


def _version_sort_key(v):
    m = re.match(r"^(\d+)\.0$", v)
    return (0, int(m.group(1))) if m else (1, v)


def fetch_available_versions():
    """Query Docker Hub for every tag of the official odoo image, keep the
    clean major.0 releases (skipping noisy dated micro-build tags like
    '19.0-20260817' and bare major-only tags like '19'), and cache the
    result to disk. This is what the "Update available versions" button
    calls — Docker Hub is the source of truth, not something to hit on
    every page load."""
    versions = set()
    url = DOCKER_HUB_TAGS_URL
    while url:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for result in data.get("results", []):
            name = result.get("name", "")
            if name == "latest" or _CLEAN_VERSION_RE.match(name):
                versions.add(name)
        url = data.get("next")

    versions = sorted(versions, key=_version_sort_key)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AVAILABLE_VERSIONS_CACHE, "w") as f:
        json.dump({"versions": versions, "fetched_at": time.time()}, f)
    return versions


def list_available_versions():
    """Cached list from the last fetch_available_versions() call, plus
    whatever's already been pulled locally (in case it fell out of the
    cached set, e.g. an old dated tag someone typed in manually)."""
    versions = set(list_odoo_images())
    if os.path.isfile(AVAILABLE_VERSIONS_CACHE):
        try:
            with open(AVAILABLE_VERSIONS_CACHE) as f:
                data = json.load(f)
            versions.update(data.get("versions", []))
        except (json.JSONDecodeError, OSError):
            pass
    return sorted(versions, key=_version_sort_key)


def available_versions_fetched_at():
    if os.path.isfile(AVAILABLE_VERSIONS_CACHE):
        try:
            with open(AVAILABLE_VERSIONS_CACHE) as f:
                return json.load(f).get("fetched_at")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _http_port(c):
    """The host port mapped to the container's web UI (8069/tcp) specifically —
    not just 'whichever port happens to come first', since a debug-enabled
    container also exposes the debugpy port (5678/tcp), which Docker's own
    API lists *before* 8069/tcp, not after.

    NetworkSettings.Ports only reflects the *live* binding, which goes empty
    the moment a container stops — HostConfig.PortBindings is the static,
    always-present configuration, so it's the reliable source for a
    stopped instance (and identical to NetworkSettings.Ports for a running
    one)."""
    bindings = (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).get("8069/tcp")
    if not bindings:
        bindings = (c.attrs.get("HostConfig", {}).get("PortBindings") or {}).get("8069/tcp")
    return bindings[0]["HostPort"] if bindings else None


def port_holder(port):
    """Instance name currently binding `port` on the host, if any — for
    turning docker's "port is already allocated" into a message that says
    *who* has it. Only running containers actually hold a port; a stopped
    one merely has it reserved in its config (see next_free_port)."""
    port = int(port)
    for c in client.containers.list():
        for bindings in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).values():
            for b in bindings or []:
                try:
                    if int(b["HostPort"]) == port:
                        return c.labels.get("instance_name", c.name)
                except (KeyError, ValueError, TypeError):
                    pass
    return None


def next_free_port(start=8069):
    """Lowest port from `start` that no container has reserved and nothing
    is currently listening on.

    Reads HostConfig.PortBindings, not NetworkSettings.Ports. The latter
    is the *runtime* mapping and is `{}` for a stopped container, so
    reading it made every stopped instance's ports look free and handed
    them straight to the next instance created — the collision only
    surfacing later, when both were started, as "Bind for 0.0.0.0:8073
    failed: port is already allocated". PortBindings is the configured
    binding and survives a stop, which is what "reserved" has to mean
    here: instances are long-lived and spend plenty of time stopped."""
    used = set()
    for c in client.containers.list(all=True):
        sources = [
            (c.attrs.get("HostConfig", {}) or {}).get("PortBindings") or {},
            (c.attrs.get("NetworkSettings", {}) or {}).get("Ports") or {},
        ]
        for mapping in sources:
            for bindings in mapping.values():
                for b in bindings or []:
                    try:
                        used.add(int(b["HostPort"]))
                    except (KeyError, ValueError, TypeError):
                        pass
    port = start
    while port in used or _port_in_use(port):
        port += 1
    return port


def _container_name(instance_name):
    return f"odoo-inst-{instance_name}"


def _ensure_git_source(repo_url, version, cache_dir, label):
    """Clone `repo_url` at the given version branch into a per-version
    cache dir, reusing it on later calls instead of re-cloning."""
    dest = os.path.join(cache_dir, version)
    if os.path.isdir(os.path.join(dest, ".git")):
        return dest

    os.makedirs(cache_dir, exist_ok=True)
    try:
        subprocess.run(
            [
                "git", "clone",
                "--branch", version,
                "--single-branch",
                "--depth", "1",
                repo_url,
                dest,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git clone of {label} ({version}) failed: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git clone of {label} ({version}) timed out")

    return dest


_REVISION_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _ensure_git_source_at_revision(repo_url, cache_dir, label, revision):
    """Like _ensure_git_source, but pinned to an exact commit rather than
    tracking a branch's moving tip — cached separately (by revision, not
    version) so pinning one instance to a specific commit doesn't touch
    the shared "latest" clone every other instance of that version is
    using. `git clone --branch` only accepts a branch/tag name, not an
    arbitrary SHA, so this is a different sequence: init + fetch that
    one commit + checkout — confirmed directly that GitHub supports a
    shallow fetch of an arbitrary commit, not just a ref tip, so this
    stays exactly as shallow as the branch-tracking case."""
    if not _REVISION_SHA_RE.match(revision):
        raise ValueError(f"not a commit sha: {revision!r}")
    dest = os.path.join(cache_dir, f"pinned-{revision}")
    if os.path.isdir(os.path.join(dest, ".git")):
        return dest

    os.makedirs(cache_dir, exist_ok=True)
    tmp_dest = dest + ".fetching"
    if os.path.isdir(tmp_dest):
        shutil.rmtree(tmp_dest)
    try:
        subprocess.run(["git", "init", "-q", tmp_dest], check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(
            ["git", "-C", tmp_dest, "remote", "add", "origin", repo_url],
            check=True, capture_output=True, text=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", tmp_dest, "fetch", "--depth", "1", "origin", revision],
            check=True, capture_output=True, text=True, timeout=600,
        )
        subprocess.run(
            ["git", "-C", tmp_dest, "checkout", "-q", "FETCH_HEAD"],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        shutil.rmtree(tmp_dest, ignore_errors=True)
        raise RuntimeError(f"failed to fetch {label} at {revision}: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dest, ignore_errors=True)
        raise RuntimeError(f"fetching {label} at {revision} timed out")

    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.move(tmp_dest, dest)
    return dest


def _refresh_git_source(cache_dir, version, label):
    """Re-syncs an existing "latest"-tracking clone (cache_dir/<version>) to
    the branch's current tip, in place — the manual counterpart to
    _ensure_git_source's caching, which otherwise reuses whatever was
    cloned the first time forever. Shared across every instance whose
    revision is set to "latest" for this version, same as the clone
    itself already is — refreshing updates it for all of them, not just
    whichever instance's button was clicked. A no-op if nothing is
    cloned yet; the next ensure_*_source call clones fresh instead."""
    dest = os.path.join(cache_dir, version)
    if not os.path.isdir(os.path.join(dest, ".git")):
        return
    try:
        subprocess.run(
            ["git", "-C", dest, "fetch", "--depth", "1", "origin", version],
            check=True, capture_output=True, text=True, timeout=600,
        )
        subprocess.run(
            ["git", "-C", dest, "reset", "-q", "--hard", "FETCH_HEAD"],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"failed to refresh {label} ({version}): {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"refreshing {label} ({version}) timed out")


def _git_source_revision(path):
    """The commit currently checked out at `path`, or None if there's
    nothing cloned there yet — for displaying what an instance is
    actually running against, not just what it's configured to track."""
    if not path or not os.path.isdir(os.path.join(path, ".git")):
        return None
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_enterprise_source(version, revision="latest"):
    if revision and revision != "latest":
        return _ensure_git_source_at_revision(ENTERPRISE_REPO_SSH, ENTERPRISE_CACHE_DIR, "enterprise", revision)
    return _ensure_git_source(ENTERPRISE_REPO_SSH, version, ENTERPRISE_CACHE_DIR, "enterprise")


def ensure_design_themes_source(version, revision="latest"):
    if revision and revision != "latest":
        return _ensure_git_source_at_revision(DESIGN_THEMES_REPO, DESIGN_THEMES_CACHE_DIR, "design-themes", revision)
    return _ensure_git_source(DESIGN_THEMES_REPO, version, DESIGN_THEMES_CACHE_DIR, "design-themes")


# (repo label, container label prefix, repo url, cache dir) for every
# git-sourced repo the "Repository revisions" UI (Settings-style, but
# per instance — see get_instance_revisions/refresh_instance_revision)
# can show and refresh. odoo_core is deliberately not just "always
# included" — see CORE_ADDONS_REFRESH_VERIFIED_VERSIONS.
_REVISION_REPOS = {
    "odoo_core": ("odoo", ODOO_CORE_REPO, ODOO_CORE_ADDONS_CACHE_DIR),
    "enterprise": ("enterprise", "https://github.com/odoo/enterprise", ENTERPRISE_CACHE_DIR),
    "design_themes": ("themes", DESIGN_THEMES_REPO, DESIGN_THEMES_CACHE_DIR),
}


def get_instance_revisions(name):
    """Current commit + pin status for each git-sourced repo this instance
    actually uses, for the Edit page's "Repository revisions" display —
    only odoo-core if this version is one _run_container actually refreshes
    it for, only enterprise if included and not a local folder (nothing
    for this app to track there; that's the user's own checkout), design
    themes always, since every instance gets it. Each row's current_sha is
    read live off whatever's on disk right now, not cached anywhere, so it
    can't drift from reality the way a stored value could."""
    c = client.containers.get(_container_name(name))
    version = c.labels.get("odoo_version", "14.0")
    revisions = {
        "odoo_core": c.labels.get("odoo_core_revision", "latest"),
        "enterprise": c.labels.get("enterprise_revision", "latest"),
        "design_themes": c.labels.get("design_themes_revision", "latest"),
    }

    rows = []
    for repo, (label, url, cache_dir) in _REVISION_REPOS.items():
        if repo == "odoo_core" and version not in CORE_ADDONS_REFRESH_VERIFIED_VERSIONS:
            continue
        if repo == "enterprise" and (c.labels.get("enterprise") != "1" or c.labels.get("enterprise_path")):
            continue
        revision = revisions[repo]
        pinned = revision != "latest"
        path = os.path.join(cache_dir, f"pinned-{revision}" if pinned else version)
        rows.append({
            "repo": repo,
            "label": label,
            "url": url,
            "revision": revision,
            "pinned": pinned,
            "current_sha": _git_source_revision(path),
        })
    return rows


def refresh_instance_revision(name, repo):
    """Re-syncs one repo's "latest" clone — shared across every instance of
    this version tracking latest, same as the clone itself already is — to
    its branch's current tip. Refuses on a pinned repo rather than
    silently no-op-ing: pinning is a deliberate "stay exactly here"
    choice, and a refresh button that quietly moved it anyway would
    defeat the entire point of pinning in the first place."""
    if repo not in _REVISION_REPOS:
        raise ValueError(f"unknown repo: {repo!r}")
    c = client.containers.get(_container_name(name))
    version = c.labels.get("odoo_version", "14.0")
    label, _url, cache_dir = _REVISION_REPOS[repo]
    revision_label = {"odoo_core": "odoo_core_revision", "enterprise": "enterprise_revision", "design_themes": "design_themes_revision"}[repo]
    if c.labels.get(revision_label, "latest") != "latest":
        raise ValueError("this repo is pinned to a specific revision — switch it back to Latest to refresh")
    _refresh_git_source(cache_dir, version, label)


_odoo_addons_init_py_cache = {}


def _odoo_addons_init_py(version):
    """Whether *this specific version's* image has an odoo/addons/__init__.py
    — and its exact content if so — for ensure_odoo_core_addons_source to
    replicate. Not a constant: confirmed directly that this is NOT one
    fixed answer across versions. 16.0 has the file (making odoo.addons a
    regular package, since its own framework code expects __path__ to be
    a plain list — get_manifest()'s `odoo.addons.__path__ + [root_path]`
    only works that way); a plain git checkout of 16.0's addons/ has no
    such file, since that's added by Odoo's own install step, which
    running straight from a checkout skips. But 19.0 has *removed* the
    file entirely — its own module.py does
    `odoo.addons.__path__._path_finder = ...`, which needs __path__ to be
    a real _NamespacePath, i.e. needs there to be NO __init__.py. Writing
    this file unconditionally fixed 16.0 and broke 19.0 in the exact same
    way, just the opposite direction (AttributeError instead of
    TypeError) — so this has to ask each version's own image what it
    actually does, not assume either way. Cached per version — spins up
    a throwaway container."""
    if version in _odoo_addons_init_py_cache:
        return _odoo_addons_init_py_cache[version]

    image = f"odoo:{version}"
    if image not in [t for img in client.images.list() for t in img.tags]:
        client.images.pull("odoo", tag=version)

    init_file = f"{_core_package_path(version)}/addons/__init__.py"
    try:
        output = client.containers.run(
            image, ["sh", "-c", f"cat '{init_file}' 2>/dev/null || true"], remove=True
        ).decode()
    except docker.errors.ContainerError:
        output = ""

    content = output if output.strip() else None
    _odoo_addons_init_py_cache[version] = content
    return content


def ensure_odoo_core_addons_source(version, revision="latest"):
    """A fresh clone of odoo/odoo's own <version> branch, used for its
    addons/ subdirectory specifically — deliberately NOT the odoo:<version>
    Docker image's own bundled copy. Confirmed directly on a real 16.0
    instance (Enterprise's `documents` module installed): the image's core
    addons were missing addons/base_setup/models/kpi_provider.py entirely
    — present in this same branch today, just not in whatever point in
    time that image was last built — which crashed the *entire* registry
    the moment any module referencing kpi.provider loaded, with no way to
    fix it short of editing the database directly. Enterprise/design-themes
    already dodge this by being freshly cloned rather than image-bundled;
    core addons is the one piece that wasn't.

    Deliberately NOT a wholesale replacement of the odoo package: checked
    the image's actually-installed pip versions against what a fresh
    16.0 checkout's requirements.txt wants, and they've drifted apart
    enough to be a real risk (Werkzeug 1.0.1 in the image vs 2.0.2
    wanted) — swapping the *framework* (which imports those libraries
    directly) could trade one breakage for a worse, harder-to-diagnose
    one. Addons code doesn't pull in its own third-party dependencies
    beyond what the same-version framework already provides, so it's
    the safe part to refresh this way; the framework stays exactly what
    the image ships. See ensure_odoo_core_source for that half, and
    generate_vscode_workspace for why both still need separate,
    correctly-mapped folders rather than one replacing the other.

    _run_container bind-mounts this clone's addons/ directly onto the
    image's own core-addons path rather than adding it as another
    --addons-path entry — confirmed directly that the image's
    /etc/odoo/odoo.conf addons_path wins over anything appended via
    --addons-path for a module name present in both, regardless of
    argument order, so a plain extra entry silently lost to the stale
    copy for exactly the modules this exists to fix.

    Returns the repo root, not addons/ directly: odoo/odoo actually
    splits its addons across *two* directories, and callers need both
    merged. The repo-root addons/ is the general pool (base_setup, sale,
    mail, ...), but `base` itself — and odoo's own test_* modules —
    live inside the odoo/ package at odoo/addons/ instead, merged into
    one addons/ only at install time. Confirmed the hard way: mounting
    just the repo-root addons/ over the image's installed one is missing
    `base` entirely, and `base` is a dependency of everything, so the
    whole server fails to boot ("ModuleNotFoundError: No module named
    'odoo.addons.base'") rather than just the one module this was meant
    to fix.

    Copied into addons/ below (skipping anything already there), not
    symlinked: _run_container bind-mounts addons/ alone into the
    container, not the whole repo, so a symlink pointing back up to
    ../odoo/addons/base would target a host path with nothing mounted
    at it from the container's side — a dangling link once inside,
    which is exactly as broken as not having base at all. ~90MB, a
    small fraction of the clone this is already part of, and it only
    runs while something's actually missing, so an already-merged repo
    doesn't pay this cost again."""
    if revision and revision != "latest":
        dest = _ensure_git_source_at_revision(ODOO_CORE_REPO, ODOO_CORE_ADDONS_CACHE_DIR, "odoo-core-addons", revision)
    else:
        dest = _ensure_git_source(ODOO_CORE_REPO, version, ODOO_CORE_ADDONS_CACHE_DIR, "odoo-core-addons")
    embedded = os.path.join(dest, "odoo", "addons")
    pool = os.path.join(dest, "addons")
    if os.path.isdir(embedded):
        for entry in os.listdir(embedded):
            source = os.path.join(embedded, entry)
            target = os.path.join(pool, entry)
            # odoo/addons/ is itself a package (its own __init__.py) —
            # only the module directories (base, test_*) belong in the
            # pool; skip anything that isn't one.
            if os.path.isdir(source) and not os.path.exists(target):
                shutil.copytree(source, target, symlinks=True)
    # A plain git checkout of addons/ has whatever __init__.py (if any)
    # happens to be committed there directly — not necessarily what that
    # version's own image ends up with once installed, since some
    # versions' setup.py adds or removes this file at install time (see
    # _odoo_addons_init_py — this is genuinely not the same answer across
    # versions, and guessing wrong breaks the version guessed wrong for
    # in the exact same way, just in whichever direction was assumed).
    # Matching the real image file-for-file rather than asserting a
    # policy here is what stays correct as that changes across versions
    # without this needing to know why.
    init_path = os.path.join(pool, "__init__.py")
    real_init = _odoo_addons_init_py(version)
    has_init = os.path.isfile(init_path)
    if real_init is not None and not has_init:
        with open(init_path, "w") as f:
            f.write(real_init)
    elif real_init is None and has_init:
        os.remove(init_path)
    return dest


_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def is_git_repo(path):
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def list_git_branches(path):
    """Local branches and remote-tracking branches for a repo already on
    disk (the addons-folder browser's own working copy) — shown together
    and left undeduplicated (a local branch and its own origin/<branch>
    can genuinely point at different commits, so collapsing them would
    hide that). Used to populate the branch picker; ensure_addons_copy
    below is what actually resolves a chosen name to a real checkout."""
    # Filters on the *full* refname, not the short one: refs/remotes/
    # origin/HEAD (the symbolic "default branch" pointer, not a branch)
    # shortens to plain "origin", so filtering shortened names for a
    # trailing "/HEAD" silently lets it through as a branch called
    # "origin" — which then fails to check out.
    result = subprocess.run(
        ["git", "-C", path, "for-each-ref", "--format=%(refname)\t%(refname:short)", "refs/heads", "refs/remotes"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return {"branches": [], "current": None}
    branches = set()
    for line in result.stdout.splitlines():
        full, _, short = line.strip().partition("\t")
        if short and not full.endswith("/HEAD"):
            branches.add(short)
    branches = sorted(branches)
    current = subprocess.run(
        ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    return {"branches": branches, "current": current or None}


def ensure_addons_copy(source_path, branch):
    """Copy a local addons repo at a specific branch into an isolated,
    cached location, so an instance can pin a branch of someone's working
    copy without that working copy's own checked-out branch (or further
    edits to it) affecting an already-created instance. Cloning from a
    local path (rather than a URL) has git hardlink the object store when
    on the same filesystem, so this is fast/cheap even for a large repo —
    no need for the --depth/--single-branch shortcuts _ensure_git_source
    uses for the (network) enterprise/design-themes clones. Cached by
    (source path, branch): calling this again for the same pair reuses
    the existing copy instead of re-cloning."""
    source_path = os.path.abspath(source_path)
    if not os.path.isdir(source_path):
        raise ValueError(f"not a directory: {source_path}")
    if not is_git_repo(source_path):
        raise ValueError(f"not a git repository: {source_path}")
    if not _SAFE_BRANCH_RE.match(branch) or ".." in branch.split("/"):
        raise ValueError(f"invalid branch name: {branch!r}")

    repo_name = os.path.basename(source_path.rstrip("/")) or "repo"
    source_hash = hashlib.sha1(source_path.encode()).hexdigest()[:10]
    dest = os.path.join(ADDONS_COPY_CACHE_DIR, f"{repo_name}-{source_hash}", branch)
    if os.path.isdir(os.path.join(dest, ".git")):
        return dest

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp_dest = dest.rstrip("/") + ".copying"
    if os.path.isdir(tmp_dest):
        shutil.rmtree(tmp_dest)
    try:
        subprocess.run(
            ["git", "clone", source_path, tmp_dest],
            check=True, capture_output=True, text=True, timeout=600,
        )
        subprocess.run(
            ["git", "-C", tmp_dest, "checkout", branch],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        shutil.rmtree(tmp_dest, ignore_errors=True)
        raise RuntimeError(f"failed to copy {source_path} at branch {branch}: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dest, ignore_errors=True)
        raise RuntimeError(f"copying {source_path} at branch {branch} timed out")

    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.move(tmp_dest, dest)
    return dest


def _git_remote_names(path):
    result = subprocess.run(["git", "-C", path, "remote"], capture_output=True, text=True, timeout=15)
    return [l.strip() for l in result.stdout.splitlines() if l.strip()] if result.returncode == 0 else []


def _local_branch_exists(source_path, name):
    return subprocess.run(
        ["git", "-C", source_path, "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True, text=True, timeout=15,
    ).returncode == 0


def _worktree_branch_plan(source_path, branch):
    """Decide how `branch` resolves inside `source_path`: an existing local
    branch to check out as-is, or a remote-tracking ref (e.g. "origin/19.0-foo",
    exactly as list_git_branches surfaces it) with no local branch yet, which
    needs one created so a later `git push` has somewhere to go. Looked up
    directly against source_path's own refs, not guessed from where a "/"
    sits in the name, since a local branch can itself legally contain one."""
    if _local_branch_exists(source_path, branch):
        return {"kind": "local", "checkout": branch, "new_branch": None}

    remote = subprocess.run(
        ["git", "-C", source_path, "show-ref", "--verify", "--quiet", f"refs/remotes/{branch}"],
        capture_output=True, text=True, timeout=15,
    )
    if remote.returncode != 0:
        raise ValueError(f"no such local or remote-tracking branch in {source_path}: {branch!r}")

    for name in sorted(_git_remote_names(source_path), key=len, reverse=True):
        prefix = name + "/"
        if branch.startswith(prefix):
            local_name = branch[len(prefix):]
            break
    else:
        raise ValueError(f"could not determine a local branch name for remote branch {branch!r}")

    if _local_branch_exists(source_path, local_name):
        raise ValueError(
            f"branch {local_name!r} (derived from {branch!r} by dropping the remote prefix) "
            f"already exists locally in {source_path} — pick a different branch, or rename/"
            f"remove the existing {local_name!r} first"
        )
    return {"kind": "remote", "checkout": branch, "new_branch": local_name}


def ensure_addons_worktree(source_path, branch, new_branch=None):
    """Create (or reuse) a real `git worktree` from a local addons repo, so
    an instance can mount a *live*, independently checked out branch —
    edited and committed/pushed on the host exactly like any normal git
    checkout — rather than ensure_addons_copy's frozen, isolated clone.
    Unlike that copy, this does NOT insulate the instance from the source
    repo: a worktree shares the same object store and refs as source_path
    (that's the point) — which also means git's own "a branch can only be
    checked out in one place at a time" rule applies across them, for
    whichever branch actually ends up checked out — see below.

    Two modes:
      - `new_branch=None` (check out `branch` directly): `branch` may be a
        local branch or a remote-tracking branch (list_git_branches's
        merged, undeduplicated list can hand back either — see
        _worktree_branch_plan for how each resolves; the remote case
        creates a new local branch, tracking it, so `git push` behaves
        normally). Refuses outright if `branch` is already checked out in
        source_path itself or another of its worktrees: very plausibly the
        first thing tried here, since a real custom-addons checkout is
        typically parked permanently on one branch.
      - `new_branch=<name>` (fork a new branch instead): creates `new_branch`
        starting at `branch`'s tip and checks *that* out in the worktree —
        `branch` is only ever read from here, never occupied, so this can
        never hit the already-checked-out conflict above, including when
        `branch` is the source repo's own current branch (the common case).
        Refuses if `new_branch` already exists locally — never silently
        reuses or renames it.

    Cached by (source path, whichever branch ends up checked out): calling
    again for the same pair reuses the existing worktree. The cache-hit
    check can't be `isdir(.../.git)` the way ensure_addons_copy's is — a
    linked worktree's ".git" is a file (a gitdir: pointer back into
    source_path/.git/worktrees/), not a dir."""
    source_path = os.path.abspath(source_path)
    if not os.path.isdir(source_path):
        raise ValueError(f"not a directory: {source_path}")
    if not is_git_repo(source_path):
        raise ValueError(f"not a git repository: {source_path}")
    if not _SAFE_BRANCH_RE.match(branch) or ".." in branch.split("/"):
        raise ValueError(f"invalid branch name: {branch!r}")

    create_as = None
    if new_branch:
        if not _SAFE_BRANCH_RE.match(new_branch) or ".." in new_branch.split("/"):
            raise ValueError(f"invalid new branch name: {new_branch!r}")
        if _local_branch_exists(source_path, new_branch):
            raise ValueError(f"branch {new_branch!r} already exists in {source_path} — pick a different name")
        create_as, starting_point = new_branch, branch
    else:
        plan = _worktree_branch_plan(source_path, branch)
        create_as, starting_point = plan["new_branch"], plan["checkout"]

    checked_out_name = create_as or starting_point
    repo_name = os.path.basename(source_path.rstrip("/")) or "repo"
    source_hash = hashlib.sha1(source_path.encode()).hexdigest()[:10]
    dest = os.path.join(ADDONS_WORKTREE_CACHE_DIR, f"{repo_name}-{source_hash}", checked_out_name)
    if os.path.exists(os.path.join(dest, ".git")) and is_git_repo(dest):
        return dest

    if create_as is None:
        # Only reachable checking out an existing *local* branch directly
        # with no new_branch override — the one case that can collide with
        # wherever that branch is already checked out.
        listing = subprocess.run(
            ["git", "-C", source_path, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        conflict_path, cur_path = None, None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                cur_path = line[len("worktree "):]
            elif line == f"branch refs/heads/{starting_point}":
                conflict_path = cur_path
        if conflict_path:
            same_repo = os.path.abspath(conflict_path) == source_path
            where = "its own working tree" if same_repo else f"another worktree at {conflict_path}"
            raise RuntimeError(
                f"branch {starting_point!r} is already checked out in {where} — git only allows "
                f"a branch to be checked out in one place at a time. Switch it to a different "
                f"branch, pick a different branch here, or create a new branch from it instead."
            )
        add_args = ["git", "-C", source_path, "worktree", "add", dest, starting_point]
    else:
        add_args = ["git", "-C", source_path, "worktree", "add", "-b", create_as, dest, starting_point]

    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    try:
        subprocess.run(add_args, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as e:
        subprocess.run(["git", "-C", source_path, "worktree", "remove", "--force", dest],
                        capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "-C", source_path, "worktree", "prune"],
                        capture_output=True, text=True, timeout=30)
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"failed to create worktree for {source_path} at branch {branch}: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        subprocess.run(["git", "-C", source_path, "worktree", "prune"],
                        capture_output=True, text=True, timeout=30)
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"creating worktree for {source_path} at branch {branch} timed out")

    return dest


_CORE_PACKAGE_PATH_PROBE = "import odoo,sys;sys.stdout.write(odoo.__path__[0])"
_core_package_path_cache = {}


def _core_package_path(version):
    """Where the `odoo` Python package actually lives inside this
    version's image — used both for --addons-path and for extracting
    core source for VS Code. Not a constant: confirmed directly that
    odoo:10.0 has no python3 binary at all and installs to
    /usr/lib/python2.7/dist-packages/odoo instead of the python3 path
    every other version uses — a single hardcoded path silently broke
    both of those for any version still on Python 2 (10.0 and older;
    11.0/12.0 were a mixed transition, so even a version-number cutoff
    would be one edge case away from wrong). Asking the image itself
    is the only way that's right for every version with no table to
    maintain. Cached per version — this spins up a throwaway
    container, worth doing once rather than on every instance
    start/restart."""
    if version in _core_package_path_cache:
        return _core_package_path_cache[version]

    image = f"odoo:{version}"
    if image not in [t for img in client.images.list() for t in img.tags]:
        client.images.pull("odoo", tag=version)

    script = (
        f"python3 -c '{_CORE_PACKAGE_PATH_PROBE}' 2>/dev/null || "
        f"python -c '{_CORE_PACKAGE_PATH_PROBE}' 2>/dev/null"
    )
    try:
        output = client.containers.run(image, ["sh", "-c", script], remove=True).decode().strip()
    except docker.errors.ContainerError as e:
        raise RuntimeError(f"could not locate the odoo package inside image {image}: {e}")
    if not output.startswith("/"):
        raise RuntimeError(f"could not locate the odoo package inside image {image} (tried python3 and python)")

    _core_package_path_cache[version] = output
    return output


_supports_http_interface_cache = {}


def _supports_http_interface(version):
    """Whether this version's `odoo` CLI understands `--http-interface` at
    all. Not a version-number cutoff: confirmed directly (docker run, not
    just reading config.py) that odoo:10.0 hard-errors on it
    ("no such option") while odoo:14.0 and up accept it — and the whole
    point of asking the image is not having to also verify every version in
    between actually pulled here. Grepping config.py's own option
    definition is cheap and exact, no process to actually start; cached per
    version, same reasoning as _core_package_path. See _run_container: the
    flag itself is needed because Odoo's own default for it flipped from
    0.0.0.0 (<20.0) to 127.0.0.1 (20.0+), confirmed the same way — but
    versions too old to accept it at all just keep relying on their own
    default, same as before this existed."""
    if version in _supports_http_interface_cache:
        return _supports_http_interface_cache[version]

    image = f"odoo:{version}"
    if image not in [t for img in client.images.list() for t in img.tags]:
        client.images.pull("odoo", tag=version)

    script = (
        "grep -q -- '\"--http-interface\"' "
        "/usr/lib/python3/dist-packages/odoo/tools/config.py "
        "/usr/lib/python2.7/dist-packages/openerp/tools/config.py 2>/dev/null"
    )
    try:
        client.containers.run(image, ["sh", "-c", script], remove=True)
        supported = True
    except docker.errors.ContainerError:
        supported = False

    _supports_http_interface_cache[version] = supported
    return supported


def ensure_odoo_core_source(version):
    """Copy the core Odoo *framework* (api.py, models.py, http.py, tools/,
    etc.) out of the already-pulled odoo:<version> image itself (via a
    throwaway, never-started container) rather than cloning from GitHub —
    this is exactly what's actually running, no network/SSH needed, and
    it's cached on the host so VS Code (and the debugger) can reference
    it. For a version in CORE_ADDONS_REFRESH_VERIFIED_VERSIONS, framework
    only, deliberately: addons/ is excluded below, since
    ensure_odoo_core_addons_source's fresh clone is bind-mounted directly
    over that exact path inside the running container now (see
    _run_container) — this image-extracted copy would otherwise sit right
    next to it showing stale, unused content under the same directory
    name. For any other version, addons/ stays — the image's own is
    still what's actually running there, so that's what still needs to
    be browsable/debuggable."""
    dest = os.path.join(ODOO_CORE_CACHE_DIR, version)
    refresh_addons = version in CORE_ADDONS_REFRESH_VERIFIED_VERSIONS
    # Not just "dest is a non-empty directory": the old `docker cp`-based
    # version of this function could fail partway through (see below) and
    # left whatever it had already written on disk with no cleanup, which
    # that check couldn't tell apart from a genuinely complete extraction
    # — silently serving an incomplete core folder to every instance of
    # that version from then on, forever, since a failed run never got a
    # chance to retry. The marker file only ever gets written after a
    # fully successful extraction, so a directory left over from an old
    # partial failure (no marker) is correctly treated as not cached.
    marker = dest + ".complete"
    if os.path.isdir(dest) and os.path.isfile(marker):
        # A cache from before addons/ started being excluded (above) would
        # still have it sitting here — clean it up on the read path too,
        # not just for a fresh extraction, so an existing cache doesn't
        # need a manual wipe to pick up the change.
        if refresh_addons:
            shutil.rmtree(os.path.join(dest, "addons"), ignore_errors=True)
        return dest

    image = f"odoo:{version}"
    core_package_path = _core_package_path(version)

    os.makedirs(ODOO_CORE_CACHE_DIR, exist_ok=True)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    if os.path.isfile(marker):
        os.remove(marker)

    # Not `docker cp`: some addons (point_of_sale's fonts, at least) ship as
    # symlinks into shared system packages outside /usr/lib/.../odoo
    # entirely (e.g. into /usr/share/fonts/...) — harmless read-only assets
    # we already fully trust, since this is the very image we run as a
    # live instance, but `docker cp` refuses to copy any symlink whose
    # target resolves outside the requested source path at all, full stop,
    # and fails the *entire* copy over one such file. Streaming the tar
    # ourselves and extracting with tarfile's most permissive filter skips
    # that check for this already-trusted source; a resulting dangling
    # symlink for one font file is harmless for browsing/debugging source.
    extract_tmp = dest + ".extracting"
    tmp = client.containers.create(image)
    try:
        stream, _ = tmp.get_archive(core_package_path)
        tar_bytes = io.BytesIO()
        for chunk in stream:
            tar_bytes.write(chunk)
        tar_bytes.seek(0)

        if os.path.isdir(extract_tmp):
            shutil.rmtree(extract_tmp)
        os.makedirs(extract_tmp)
        with tarfile.open(fileobj=tar_bytes) as tar:
            tar.extractall(path=extract_tmp, filter="fully_trusted")

        # get_archive() names entries after the source's basename
        # ("odoo/..."), matching how `docker cp` used to place them
        # directly at `dest` itself (dest/api.py, not dest/odoo/api.py).
        shutil.move(os.path.join(extract_tmp, os.path.basename(core_package_path)), dest)
        if refresh_addons:
            # This copy's own bundled addons/ is exactly the stale copy
            # ensure_odoo_core_addons_source's fresh clone now exists to
            # replace for *running* purposes (see _run_container) —
            # leaving it here would show VS Code two different "addons"
            # folders with different content and no way to tell which
            # one is live. Drop it; only the framework half of this
            # extraction is still what's actually running.
            shutil.rmtree(os.path.join(dest, "addons"), ignore_errors=True)
        with open(marker, "w"):
            pass
    except Exception as e:
        raise RuntimeError(f"failed to extract odoo core from image {image}: {e}")
    finally:
        tmp.remove(force=True)
        shutil.rmtree(extract_tmp, ignore_errors=True)

    return dest


def generate_vscode_workspace(name):
    """Build a multi-root .code-workspace for this instance: the core Odoo
    framework and core addons (the latter a fresh git clone, not the
    image's own bundled copy — see ensure_odoo_core_addons_source),
    Enterprise/design-themes if included, and every custom addons folder
    — plus, if debug mode is on, a debugpy 'attach' launch config with
    path mappings from each local folder to where it actually lives
    inside the container."""
    c = client.containers.get(_container_name(name))
    version = c.labels.get("odoo_version", "14.0")
    include_enterprise = c.labels.get("enterprise") == "1"
    include_themes = c.labels.get("design_themes") == "1"
    addons_paths = [p for p in c.labels.get("addons_paths", "").split("\n") if p]
    enable_debug = c.labels.get("debug") == "1"
    debug_port = c.labels.get("debug_port") or None
    enterprise_revision = c.labels.get("enterprise_revision", "latest")
    design_themes_revision = c.labels.get("design_themes_revision", "latest")
    odoo_core_revision = c.labels.get("odoo_core_revision", "latest")

    folders = []
    path_mappings = []

    # Two separate folders/mappings, not one: the framework (still exactly
    # what the image ships) and the core addons (a fresh clone, bind-mounted
    # directly over the image's own addons path — see _run_container) are
    # different content now, and the framework extraction below excludes
    # its own addons/ accordingly, so they no longer overlap on disk either.
    #
    # The addons mapping is listed *before* the framework one on purpose:
    # its remoteRoot (.../odoo/addons) is a subdirectory of the framework
    # mapping's (.../odoo), so a debugger matching mappings in order needs
    # the more specific one checked first — otherwise a breakpoint in an
    # addons file would resolve against the framework's local folder
    # instead, land on a path that was never extracted there, and silently
    # never bind.
    #
    # Only for a version _run_container actually refreshes the addons
    # for (CORE_ADDONS_REFRESH_VERIFIED_VERSIONS) — otherwise there is no
    # separate fresh copy to speak of, the image's own addons are what's
    # really running, and ensure_odoo_core_source below keeps its own
    # bundled addons/ in that case for exactly this reason.
    if version in CORE_ADDONS_REFRESH_VERIFIED_VERSIONS:
        core_addons_path = ensure_odoo_core_addons_source(version, odoo_core_revision)
        folders.append({"name": f"odoo-core-addons-{version}", "path": f"{core_addons_path}/addons"})
        path_mappings.append({"localRoot": f"{core_addons_path}/addons", "remoteRoot": f"{_core_package_path(version)}/addons"})

    core_path = ensure_odoo_core_source(version)
    folders.append({"name": f"odoo-core-{version}", "path": core_path})
    path_mappings.append({"localRoot": core_path, "remoteRoot": _core_package_path(version)})

    if include_enterprise:
        enterprise_path = ensure_enterprise_source(version, enterprise_revision)
        folders.append({"name": "enterprise", "path": enterprise_path})
        path_mappings.append({"localRoot": enterprise_path, "remoteRoot": "/mnt/enterprise-addons"})

    if include_themes:
        themes_path = ensure_design_themes_source(version, design_themes_revision)
        folders.append({"name": "design-themes", "path": themes_path})
        path_mappings.append({"localRoot": themes_path, "remoteRoot": "/mnt/design-themes-addons"})

    for i, p in enumerate(addons_paths):
        folders.append({"name": os.path.basename(p.rstrip("/")) or p, "path": p})
        path_mappings.append({"localRoot": p, "remoteRoot": f"/mnt/extra-addons-{i}"})

    workspace = {"folders": folders, "settings": {}}

    if enable_debug and debug_port:
        workspace["launch"] = {
            "version": "0.2.0",
            "configurations": [{
                "name": f"Attach: {name} (Odoo, debugpy)",
                # "debugpy", not the older "python": debugging moved out of
                # ms-python.python into ms-python.debugpy, and current
                # versions of neither register a "python" debug type any
                # more — a config using it fails with "Configured debug
                # type 'python' is not supported". Checked against the
                # installed extensions' package.json.
                "type": "debugpy",
                "request": "attach",
                # localhost on native Linux; host.docker.internal when
                # code-server runs behind Docker Desktop, where the
                # container has no route to the host via localhost.
                "connect": {"host": debug_attach_host(), "port": int(debug_port)},
                "pathMappings": path_mappings,
                "justMyCode": False,
            }],
        }

    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    workspace_path = os.path.join(WORKSPACE_DIR, f"{name}.code-workspace")
    with open(workspace_path, "w") as f:
        json.dump(workspace, f, indent=2)
    return workspace_path


def list_instances():
    instances = []
    for c in client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}={MANAGED_VALUE}"}):
        if c.labels.get("role") != "odoo":
            continue
        port = _http_port(c)
        name = c.labels.get("instance_name", c.name)
        instances.append({
            "name": name,
            "container_name": c.name,
            "status": c.status,
            "port": port,
            "version": c.labels.get("odoo_version", "?"),
            "db_name": c.labels.get("db_name", name),
            "enterprise": c.labels.get("enterprise") == "1",
            "enterprise_path": c.labels.get("enterprise_path", ""),
            "enterprise_revision": c.labels.get("enterprise_revision", "latest"),
            "design_themes": c.labels.get("design_themes") == "1",
            "design_themes_revision": c.labels.get("design_themes_revision", "latest"),
            "odoo_core_revision": c.labels.get("odoo_core_revision", "latest"),
            "addons_paths": [p for p in c.labels.get("addons_paths", "").split("\n") if p],
            "extra_args": c.labels.get("extra_args", ""),
            "debug": c.labels.get("debug") == "1",
            "debug_port": c.labels.get("debug_port") or None,
            "restore": _restore_status.get(c.name),
        })
    instances.sort(key=lambda i: i["name"])
    return instances


def _data_volume_name(instance_name):
    return f"{_container_name(instance_name)}-data"


DEBUGPY_PORT = 5678


def _run_container(name, version, addons_paths, port, db_name, include_enterprise, extra_args,
                    enable_debug=False, debug_port=None, pin_database=True,
                    intended_debug=None, intended_debug_port=None, enterprise_path="",
                    enterprise_revision="latest", design_themes_revision="latest",
                    odoo_core_revision="latest"):
    """Build and start the container for an instance. Shared by create_instance
    (fresh instance) and update_instance (recreate with changed settings).
    addons_paths is a list of host directories, each mounted separately.

    enterprise_path, when set, is a local Odoo Enterprise checkout to mount
    instead of cloning odoo/enterprise over SSH — the SSH clone needs
    GitHub access to a private repo, which not everyone has. Branch
    selection is handled before it gets here: the folder browser copies
    the chosen branch out via ensure_addons_copy and hands back the path
    of that copy, exactly as it does for custom addons folders.

    pin_database=False skips --database=<db_name>. That flag is what lets
    visiting an instance skip straight to /web/login instead of the database
    selector — but it also makes Odoo auto-create and initialize a blank
    database under that exact name shortly after boot (confirmed directly:
    an idle container with nothing else touching it still ends up with a
    fully initialized blank db within ~30s). That's fine for a normal fresh
    instance, but when a backup is about to be restored into that same name,
    it's a race — Odoo's own auto-init frequently wins, so the restore then
    fails with "database already exists" against a blank database. So the
    caller restoring a backup passes pin_database=False for the initial
    boot, then re-runs this (via update_instance) with pin_database=True
    once the restore has actually succeeded."""
    if f"odoo:{version}" not in [t for img in client.images.list() for t in img.tags]:
        log_activity(name, f"pulling docker image odoo:{version} (not cached locally)...")
        client.images.pull("odoo", tag=version)
        log_activity(name, f"pulled odoo:{version}")
    else:
        log_activity(name, f"using already-pulled image odoo:{version}")

    container_name = _container_name(name)
    addons_paths = addons_paths or []

    volumes = {
        _data_volume_name(name): {"bind": "/var/lib/odoo", "mode": "rw"},
    }

    command = [f"--database={db_name}"] if pin_database else []
    # Explicit, not left to Odoo's own default: confirmed directly (reading
    # config.py out of both images) that the default flipped from 0.0.0.0 in
    # 19.0 to 127.0.0.1 in 20.0 — a deliberate change, 19.0 already warns
    # about it ("will change to 127.0.0.1 in 20.0"). Binding to loopback
    # inside the container makes it unreachable through the Docker port
    # mapping every instance here relies on. Only for versions that actually
    # understand the flag (see _supports_http_interface) — confirmed 10.0
    # hard-errors on an option it doesn't recognize, so this can't be
    # unconditional; a version too old to support it keeps relying on its
    # own default, same as always.
    if _supports_http_interface(version):
        command.append("--http-interface=0.0.0.0")
    extra_parts = []
    warnings = []

    if include_enterprise:
        if enterprise_path:
            # An explicit local checkout, for anyone without SSH access to
            # odoo/enterprise on GitHub. Mounted read-only: unlike the
            # managed clone under data/, this is very likely the user's own
            # working copy, and an instance has no business writing to it.
            if not os.path.isdir(enterprise_path):
                raise ValueError(f"enterprise folder does not exist: {enterprise_path}")
            log_activity(name, f"enterprise addons: using local folder {enterprise_path}")
            volumes[enterprise_path] = {"bind": "/mnt/enterprise-addons", "mode": "ro"}
        else:
            pinned = enterprise_revision and enterprise_revision != "latest"
            rev_label = f"pinned to {enterprise_revision[:12]}" if pinned else version
            check_dir = os.path.join(ENTERPRISE_CACHE_DIR, f"pinned-{enterprise_revision}" if pinned else version)
            cached = os.path.isdir(os.path.join(check_dir, ".git"))
            log_activity(name, f"enterprise addons ({rev_label}): " + ("using cached clone" if cached else "cloning via SSH..."))
            volumes[ensure_enterprise_source(version, enterprise_revision)] = {"bind": "/mnt/enterprise-addons", "mode": "rw"}
            if not cached:
                log_activity(name, f"enterprise addons ({rev_label}): clone complete")
        extra_parts.append("/mnt/enterprise-addons")

    design_themes_included = False
    try:
        dt_pinned = design_themes_revision and design_themes_revision != "latest"
        dt_rev_label = f"pinned to {design_themes_revision[:12]}" if dt_pinned else version
        dt_check_dir = os.path.join(DESIGN_THEMES_CACHE_DIR, f"pinned-{design_themes_revision}" if dt_pinned else version)
        cached = os.path.isdir(os.path.join(dt_check_dir, ".git"))
        log_activity(name, f"design-themes ({dt_rev_label}): " + ("using cached clone" if cached else "cloning..."))
        design_themes_path = ensure_design_themes_source(version, design_themes_revision)
        volumes[design_themes_path] = {"bind": "/mnt/design-themes-addons", "mode": "rw"}
        extra_parts.append("/mnt/design-themes-addons")
        design_themes_included = True
        if not cached:
            log_activity(name, f"design-themes ({dt_rev_label}): clone complete")
    except RuntimeError as e:
        warnings.append(str(e))
        log_activity(name, f"design-themes ({version}): skipped — {e}")

    for i, path in enumerate(addons_paths):
        mount_point = f"/mnt/extra-addons-{i}"
        volumes[path] = {"bind": mount_point, "mode": "rw"}
        extra_parts.append(mount_point)

    # Only for versions actually verified end-to-end (see
    # CORE_ADDONS_REFRESH_VERIFIED_VERSIONS) — not gated on extra_parts,
    # since a stale image affects every instance of that version, not
    # just ones with Enterprise/custom addons mounted in, but gated on
    # version because it demonstrably does not generalize to every one.
    #
    # Bind-mounted directly *onto* the image's own core-addons path, not
    # added as an extra --addons-path entry — confirmed directly that the
    # latter doesn't work the way it looks like it should: the image's
    # /etc/odoo/odoo.conf already has an addons_path, and --addons-path on
    # the command line is *appended* to that, not prepended or substituted,
    # so a fresh clone listed first in --addons-path still lost to the
    # stale one for any module name present in both (base_setup, in the
    # bug this exists for) — Odoo resolved it from the image copy
    # regardless of argument order. Shadowing the image's own path with
    # this mount means there's no ordering to fight: it's the only
    # base_setup Odoo can find, because it's the same path Python always
    # looked in, just with different content underneath now.
    if version in CORE_ADDONS_REFRESH_VERIFIED_VERSIONS:
        core_pinned = odoo_core_revision and odoo_core_revision != "latest"
        core_rev_label = f"pinned to {odoo_core_revision[:12]}" if core_pinned else version
        core_check_dir = os.path.join(ODOO_CORE_ADDONS_CACHE_DIR, f"pinned-{odoo_core_revision}" if core_pinned else version)
        core_addons_cached = os.path.isdir(os.path.join(core_check_dir, ".git"))
        log_activity(name, f"odoo core addons ({core_rev_label}): " + ("using cached clone" if core_addons_cached else "cloning..."))
        core_addons_src = ensure_odoo_core_addons_source(version, odoo_core_revision)
        if not core_addons_cached:
            log_activity(name, f"odoo core addons ({core_rev_label}): clone complete")
        volumes[f"{core_addons_src}/addons"] = {"bind": f"{_core_package_path(version)}/addons", "mode": "ro"}

    if extra_parts:
        addons_path_full = ",".join(extra_parts + [f"{_core_package_path(version)}/addons"])
        command.append(f"--addons-path={addons_path_full}")

    extra_args = (extra_args or "").strip()
    if extra_args:
        command.extend(shlex.split(extra_args))

    if not command:
        # entrypoint.sh's case-match needs at least one arg (it pattern-matches
        # $1 against "odoo"/"--"/"-*" to decide whether to inject DB args and
        # wait for postgres); an empty command breaks that entirely.
        command = ["odoo"]

    ports = {"8069/tcp": port}

    if enable_debug:
        # entrypoint.sh only auto-injects --db_host/--db_user/--db_password
        # when it recognizes $1 (odoo/--...); running under debugpy via `sh -c`
        # doesn't match those cases, so it falls through to a plain `exec "$@"`
        # and we have to pass the DB args ourselves (from the same env vars).
        odoo_args = " ".join(shlex.quote(c) for c in command)
        odoo_invocation = (
            f"/usr/bin/odoo {odoo_args} --db_host=\"$HOST\" --db_port=5432 "
            f"--db_user=\"$USER\" --db_password=\"$PASSWORD\""
        )
        # Newer images (Python 3.12+, e.g. Odoo 19.0) enforce PEP 668 and
        # refuse a plain `pip install`, even with --user — needs
        # --break-system-packages. Older images (e.g. 14.0's Python
        # 3.7/pip 18.1) predate that flag and would error on it if passed
        # unconditionally, so: try plain first, only add the flag if that
        # fails. No log redirect — install output goes straight to stdout
        # (visible in the instance's own Logs page) instead of being hidden
        # in a file inside the container. If debugpy still can't be
        # installed for some other reason, fall back to running Odoo
        # without it rather than crash-looping the whole instance forever.
        script = (
            "echo '--- installing debugpy ---'; "
            "pip3 install --user -q debugpy "
            "|| pip3 install --user -q --break-system-packages debugpy; "
            "if python3 -c 'import debugpy' 2>/dev/null; then "
            f"exec python3 -m debugpy --listen 0.0.0.0:{DEBUGPY_PORT} {odoo_invocation}; "
            "else "
            "echo 'WARNING: debugpy could not be installed — starting Odoo without the debugger attached' >&2; "
            f"exec {odoo_invocation}; "
            "fi"
        )
        command = ["sh", "-c", script]
        ports[f"{DEBUGPY_PORT}/tcp"] = debug_port

    container = client.containers.run(
        f"odoo:{version}",
        name=container_name,
        command=command,
        ports=ports,
        volumes=volumes,
        network=NETWORK_NAME,
        environment={
            "HOST": DB_CONTAINER_NAME,
            "USER": DB_USER,
            "PASSWORD": DB_PASSWORD,
        },
        detach=True,
        labels={
            MANAGED_LABEL: MANAGED_VALUE,
            "role": "odoo",
            "instance_name": name,
            "odoo_version": version,
            "db_name": db_name,
            "enterprise": "1" if include_enterprise else "0",
            "enterprise_path": enterprise_path or "",
            "enterprise_revision": enterprise_revision or "latest",
            "design_themes": "1" if design_themes_included else "0",
            "design_themes_revision": design_themes_revision or "latest",
            "odoo_core_revision": odoo_core_revision or "latest",
            "addons_paths": "\n".join(addons_paths),
            "addons_path_full": addons_path_full,
            "extra_args": extra_args,
            "debug": "1" if enable_debug else "0",
            "debug_port": str(debug_port) if enable_debug else "",
            "pin_database": "1" if pin_database else "0",
            # What debug settings should be applied once a deferred restore
            # (see docstring above) actually succeeds — independent of the
            # current `debug` label, which reflects this specific boot.
            "intended_debug": "1" if (intended_debug if intended_debug is not None else enable_debug) else "0",
            "intended_debug_port": str(intended_debug_port if intended_debug is not None else debug_port) if (intended_debug if intended_debug is not None else enable_debug) else "",
        },
    )
    log_activity(name, f"container {container_name} started (command: {' '.join(command)})")
    return container, warnings


def create_instance(name, version, addons_paths, port, backup_path=None,
                     db_name=None, include_enterprise=False, extra_args="",
                     enable_debug=False, debug_port=None, enterprise_path="",
                     enterprise_revision="latest", design_themes_revision="latest",
                     odoo_core_revision="latest"):
    log_activity(name, f"creating instance: version={version} port={port} "
                        f"enterprise={include_enterprise}{' (local: ' + enterprise_path + ')' if enterprise_path else ''} "
                        f"debug={enable_debug} "
                        f"backup={os.path.basename(backup_path) if backup_path else None}")
    ensure_shared_db()
    if not db_name:
        db_name = name

    # A backup is about to be restored into db_name — see _run_container's
    # docstring for why the first boot must NOT pin --database, and (since
    # the whole point of that pin is what makes the restore-vs-auto-init
    # race possible) must also skip debug mode, since debugpy wrapping the
    # process is what turns a slow multipart upload into an outright
    # deadlock. Both get applied for real once the restore succeeds.
    defer = bool(backup_path)
    container, warnings = _run_container(
        name, version, addons_paths, port, db_name, include_enterprise, extra_args,
        enable_debug=False if defer else enable_debug,
        debug_port=debug_port,
        pin_database=not defer,
        intended_debug=enable_debug,
        intended_debug_port=debug_port,
        enterprise_path=enterprise_path,
        enterprise_revision=enterprise_revision,
        design_themes_revision=design_themes_revision,
        odoo_core_revision=odoo_core_revision,
    )
    container_name = _container_name(name)
    if backup_path:
        log_activity(name, "restore queued — will start once the container is up")
        _restore_status[container_name] = "waiting"
        t = threading.Thread(
            target=_restore_and_finalize,
            args=(name, container_name, db_name, backup_path),
            daemon=True,
        )
        t.start()

    return container, warnings


def _restore_and_finalize(name, container_name, db_name, backup_path):
    _restore_backup(container_name, db_name, backup_path)
    if _restore_status.get(container_name, "") == "done":
        try:
            _finalize_after_restore(name)
        except Exception as e:
            _restore_status[container_name] = f"done, but failed to apply final settings: {e}"


def _finalize_after_restore(name):
    """Recreate the container with its originally-intended --database pin
    and debug settings now that the restore has actually landed real data —
    both were deliberately deferred to avoid racing the restore itself."""
    container_name = _container_name(name)
    try:
        c = client.containers.get(container_name)
    except docker.errors.NotFound:
        return  # instance was removed while the restore was running
    if c.labels.get("pin_database") == "1":
        return  # already finalized — e.g. this restore came from a manual retry

    log_activity(name, "restore succeeded — recreating container with --database pin"
                        + (" and debug mode" if c.labels.get("intended_debug") == "1" else "") + " applied for real")
    port = int(_http_port(c))

    intended_debug = c.labels.get("intended_debug") == "1"
    intended_debug_port = c.labels.get("intended_debug_port") or None

    update_instance(
        name,
        version=c.labels.get("odoo_version", "14.0"),
        addons_paths=[p for p in c.labels.get("addons_paths", "").split("\n") if p],
        port=port,
        db_name=c.labels.get("db_name", name),
        include_enterprise=c.labels.get("enterprise") == "1",
        enterprise_path=c.labels.get("enterprise_path", ""),
        extra_args=c.labels.get("extra_args", ""),
        enable_debug=intended_debug,
        debug_port=int(intended_debug_port) if intended_debug_port else None,
        enterprise_revision=c.labels.get("enterprise_revision", "latest"),
        design_themes_revision=c.labels.get("design_themes_revision", "latest"),
        odoo_core_revision=c.labels.get("odoo_core_revision", "latest"),
    )


def update_instance(name, version, addons_paths, port, db_name, include_enterprise, extra_args,
                     enable_debug=False, debug_port=None, enterprise_path="",
                     enterprise_revision="latest", design_themes_revision="latest",
                     odoo_core_revision="latest"):
    """Recreate an existing instance's container with new settings. The named
    data volume (filestore) and the shared database survive the recreate."""
    log_activity(name, f"recreating instance: version={version} port={port} "
                        f"enterprise={include_enterprise}{' (local: ' + enterprise_path + ')' if enterprise_path else ''} "
                        f"debug={enable_debug}")
    container_name = _container_name(name)
    try:
        old = client.containers.get(container_name)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    return _run_container(name, version, addons_paths, port, db_name, include_enterprise, extra_args,
                           enable_debug=enable_debug, debug_port=debug_port,
                           enterprise_path=enterprise_path,
                           enterprise_revision=enterprise_revision,
                           design_themes_revision=design_themes_revision,
                           odoo_core_revision=odoo_core_revision)


_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _drop_database(db_name):
    """Drop db_name in the shared Postgres, terminating any open connections first."""
    if not _IDENT_RE.match(db_name):
        raise ValueError(f"refusing to drop database with unexpected name: {db_name!r}")
    db_container = client.containers.get(DB_CONTAINER_NAME)
    db_container.exec_run([
        "psql", "-U", DB_USER, "-d", "postgres", "-c",
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}';",
    ])
    db_container.exec_run([
        "psql", "-U", DB_USER, "-d", "postgres", "-c",
        f'DROP DATABASE IF EXISTS "{db_name}";',
    ])


def list_databases():
    """Every database in the shared Postgres (excluding the admin 'postgres'
    db and templates), with size, and which instance (if any) currently uses
    it as its default db — a database can outlive its instance (e.g. after
    Remove without 'also delete data', or a stray restore-target)."""
    db_container = client.containers.get(DB_CONTAINER_NAME)
    result = db_container.exec_run([
        "psql", "-U", DB_USER, "-d", "postgres", "-t", "-A", "-F", "\t", "-c",
        "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database "
        "WHERE datistemplate = false AND datname != 'postgres' ORDER BY datname;",
    ])
    in_use = {i["db_name"]: i["name"] for i in list_instances()}

    databases = []
    for line in result.output.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        dbname, size = parts
        databases.append({
            "name": dbname,
            "size": size,
            "instance": in_use.get(dbname),
        })
    return databases


def delete_database(db_name):
    _drop_database(db_name)


def retry_restore(name, backup_path):
    """Drop the instance's current database (e.g. a blank one Odoo auto-created
    while the original restore was still in flight) and restore backup_path
    into it again."""
    container_name = _container_name(name)
    db_name = client.containers.get(container_name).labels.get("db_name", name)

    log_activity(name, f"retry-restore requested: dropping database {db_name!r} and re-running restore")
    _drop_database(db_name)

    _restore_status[container_name] = "waiting"
    t = threading.Thread(
        target=_restore_and_finalize,
        args=(name, container_name, db_name, backup_path),
        daemon=True,
    )
    t.start()


def _restore_backup(container_name, db_name, backup_path):
    """Restore a backup zip the same way Odoo's own restore_db() does
    internally (create an empty database with matching encoding/collation,
    load dump.sql, move any filestore/ files into place) — but done directly
    via psql/docker cp instead of an HTTP upload through Odoo's own dev
    server, which large backups could deadlock or get dropped mid-transfer.
    Doing it ourselves also means real, granular status per step instead of
    one opaque all-or-nothing HTTP call. Every step's actual command output
    goes to the activity log, not just the terse dashboard status string."""
    name = container_name.removeprefix("odoo-inst-")
    tmpdir = tempfile.mkdtemp(prefix="odoo-restore-")
    log_activity(name, f"restore starting: {os.path.basename(backup_path)} -> database {db_name!r}")
    try:
        _restore_status[container_name] = "extracting backup"
        try:
            with zipfile.ZipFile(backup_path) as z:
                names = z.namelist()
                if "dump.sql" not in names:
                    msg = "error: backup has no dump.sql (not a supported Odoo backup format)"
                    _restore_status[container_name] = msg
                    log_activity(name, msg + f" — zip contains: {names[:20]}")
                    return
                z.extract("dump.sql", tmpdir)
                filestore_members = [n for n in names if n.startswith("filestore/") and not n.endswith("/")]
                log_activity(name, f"extracted dump.sql ({os.path.getsize(os.path.join(tmpdir, 'dump.sql'))} bytes) "
                                    f"and {len(filestore_members)} filestore file(s) from {len(names)} zip entries")
                if filestore_members:
                    z.extractall(tmpdir, filestore_members)
        except zipfile.BadZipFile:
            _restore_status[container_name] = "error: not a valid zip file"
            log_activity(name, "error: not a valid zip file")
            return

        db_container = client.containers.get(DB_CONTAINER_NAME)

        _restore_status[container_name] = "creating database"
        create_sql = f"CREATE DATABASE \"{db_name}\" ENCODING 'unicode' LC_COLLATE 'C' TEMPLATE template0;"
        log_activity(name, f"creating database: {create_sql}")
        r = db_container.exec_run(["psql", "-U", DB_USER, "-d", "postgres", "-c", create_sql])
        log_activity(name, f"psql (create database) exit={r.exit_code}: {r.output.decode(errors='replace').strip()}")
        if r.exit_code != 0:
            _restore_status[container_name] = f"error: could not create database — {r.output.decode(errors='replace').strip()[:300]}"
            return

        _restore_status[container_name] = "loading database dump (this can take a few minutes for large backups)"
        remote_dump = f"/tmp/restore-{db_name}.sql"
        log_activity(name, f"copying dump.sql into {DB_CONTAINER_NAME}:{remote_dump}")
        subprocess.run(
            ["docker", "cp", os.path.join(tmpdir, "dump.sql"), f"{DB_CONTAINER_NAME}:{remote_dump}"],
            check=True, capture_output=True, text=True,
        )
        log_activity(name, f"running: psql --dbname {db_name} -f {remote_dump}")
        r = db_container.exec_run(["psql", "-U", DB_USER, "--dbname", db_name, "-q", "-f", remote_dump])
        db_container.exec_run(["rm", "-f", remote_dump])
        log_activity(name, f"psql (load dump) exit={r.exit_code}" + (f": {r.output.decode(errors='replace').strip()}" if r.exit_code != 0 else " (ok)"))
        if r.exit_code != 0:
            _restore_status[container_name] = f"error: loading dump failed — {r.output.decode(errors='replace').strip()[-300:]}"
            log_activity(name, f"dropping database {db_name!r} after failed load")
            _drop_database(db_name)
            return

        if filestore_members:
            n_files = len(filestore_members)
            _restore_status[container_name] = f"copying filestore ({n_files} files)"
            log_activity(name, f"copying {n_files} filestore file(s) into {container_name}:/var/lib/odoo/filestore/{db_name}")
            # docker cp only auto-creates the final path component, not its
            # parent — /var/lib/odoo/filestore itself may not exist yet on a
            # freshly created instance, so it needs to exist before copying
            # .../filestore/<db_name> into it.
            client.containers.get(container_name).exec_run(["mkdir", "-p", "/var/lib/odoo/filestore"])
            subprocess.run(
                ["docker", "cp", os.path.join(tmpdir, "filestore"), f"{container_name}:/var/lib/odoo/filestore/{db_name}"],
                check=True, capture_output=True, text=True,
            )
            log_activity(name, "filestore copy complete")
        else:
            log_activity(name, "no filestore/ entries in this backup — nothing to copy")

        _restore_status[container_name] = "done"
        log_activity(name, "restore done")
    except subprocess.CalledProcessError as e:
        _restore_status[container_name] = f"error: {e.stderr or e}"
        log_activity(name, f"error: command {e.cmd} failed (exit {e.returncode}): {e.stderr}")
    except Exception as e:
        _restore_status[container_name] = f"error: {e}"
        log_activity(name, f"error: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def start_instance(name):
    """Start an instance, repairing its network attachment first.

    A container that happened to be stopped when odoo-manager-net was
    removed and recreated (a docker daemon restart, a `network prune`)
    silently loses its endpoint: docker only refuses to remove a network
    that has *running* endpoints, so stopped ones are dropped without
    complaint and nothing surfaces until next boot. Odoo then dies on its
    first database connection with

        could not translate host name "odoo-manager-db" to address

    which reads like a database fault and isn't one — the container just
    has no network, so no DNS. Reconnecting is idempotent and costs
    nothing, so this checks on every start rather than waiting for the
    failure. ensure_shared_db() also covers the neighbouring case where
    the postgres container itself isn't up yet."""
    ensure_shared_db()
    c = client.containers.get(_container_name(name))
    if NETWORK_NAME not in (c.attrs.get("NetworkSettings", {}).get("Networks") or {}):
        log_activity(name, f"container was not attached to {NETWORK_NAME}; reattaching before start")
        client.networks.get(NETWORK_NAME).connect(c)
    c.start()


def stop_instance(name):
    client.containers.get(_container_name(name)).stop()


def remove_instance(name, remove_volumes=False):
    c = client.containers.get(_container_name(name))
    db_name = c.labels.get("db_name", name)
    c.remove(force=True, v=remove_volumes)
    _restore_status.pop(_container_name(name), None)
    if remove_volumes:
        try:
            client.volumes.get(_data_volume_name(name)).remove()
        except docker.errors.NotFound:
            pass
        # "also delete data" should mean *all* of it — the filestore
        # (the volume above) and the database itself, not just one half.
        # Previously this left the database behind in the shared Postgres
        # container indefinitely, orphaned once its instance was gone.
        log_activity(name, f"remove with delete-data: dropping database {db_name!r}")
        _drop_database(db_name)


def get_logs(name, tail=200):
    c = client.containers.get(_container_name(name))
    return c.logs(tail=tail).decode(errors="replace")


def get_container_info(name):
    """Docker-level lifecycle info for the instance's container (state, exit
    code, restart count, timestamps) — not part of the stdout/stderr log stream."""
    c = client.containers.get(_container_name(name))
    c.reload()
    state = c.attrs.get("State", {})
    return {
        "status": state.get("Status"),
        "running": state.get("Running"),
        "exit_code": state.get("ExitCode"),
        "error": state.get("Error") or None,
        "oom_killed": state.get("OOMKilled"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "restart_count": c.attrs.get("RestartCount"),
        "image": c.attrs.get("Config", {}).get("Image"),
        "created": c.attrs.get("Created"),
    }


def instance_ready(name):
    """Whether this instance is not just running but actually serving
    HTTP yet. A container can sit at Docker status 'running' for a while
    before Odoo finishes booting — the dashboard's Connect buttons need
    that distinction, since clicking one during the gap gets a
    connection-refused or a half-initialized page rather than a login
    screen. Any HTTP response at all counts as ready; the point is
    telling 'nothing is listening yet' apart from 'something is', not
    asserting a particular status code.

    A restore still in flight is the same problem one level deeper: Odoo's
    web server comes up and answers /web/login (its own login/db-selector
    page, not the target database's presence) well before the backup
    behind it has finished loading, so the plain HTTP check above would
    call it ready while the dump is still being psql'd in or the filestore
    is still being copied. Treat any non-terminal restore status the same
    as "not ready yet" — done and error are both terminal, since nothing
    further is going to change on its own past that point."""
    container_name = _container_name(name)
    restore = _restore_status.get(container_name)
    if restore and restore != "done" and not restore.startswith("error"):
        return False
    c = client.containers.get(container_name)
    if c.status != "running":
        return False
    port = _http_port(c)
    if not port:
        return False
    try:
        requests.get(f"http://127.0.0.1:{port}/web/login", timeout=1.5)
        return True
    except requests.exceptions.RequestException:
        return False


_TECHNICAL_LOGINS = ("__system__", "default", "public", "portaltemplate")


def list_users(name):
    """Active, human-usable users on this instance's database, for the
    'Connect as' feature. Reads directly from Postgres — read-only."""
    c = client.containers.get(_container_name(name))
    db_name = c.labels.get("db_name", name)
    if not _IDENT_RE.match(db_name):
        raise ValueError(f"unexpected db name: {db_name!r}")

    placeholders = ",".join(f"'{login}'" for login in _TECHNICAL_LOGINS)
    query = (
        "SELECT u.id, u.login, p.name FROM res_users u "
        "JOIN res_partner p ON p.id = u.partner_id "
        f"WHERE u.active = true AND u.login NOT IN ({placeholders}) "
        "ORDER BY p.name;"
    )
    db_container = client.containers.get(DB_CONTAINER_NAME)
    result = db_container.exec_run(
        ["psql", "-U", DB_USER, "-d", db_name, "-t", "-A", "-F", "\t", "-c", query]
    )
    users = []
    for line in result.output.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        uid, login, uname = parts
        users.append({"id": int(uid), "login": login, "name": uname})
    return users


def find_admin_user(name):
    """The first active user in the 'Settings/Administration' group
    (base.group_system) — the real full-admin group, rather than assuming
    the admin's login is literally 'admin' (often changed on real databases)."""
    c = client.containers.get(_container_name(name))
    db_name = c.labels.get("db_name", name)
    if not _IDENT_RE.match(db_name):
        raise ValueError(f"unexpected db name: {db_name!r}")

    query = (
        "SELECT u.id, u.login, p.name FROM res_users u "
        "JOIN res_partner p ON p.id = u.partner_id "
        "JOIN res_groups_users_rel gu ON gu.uid = u.id "
        "JOIN res_groups g ON g.id = gu.gid "
        "JOIN ir_model_data d ON d.res_id = g.id AND d.model = 'res.groups' "
        "WHERE d.module = 'base' AND d.name = 'group_system' AND u.active = true "
        "ORDER BY u.id LIMIT 1;"
    )
    db_container = client.containers.get(DB_CONTAINER_NAME)
    result = db_container.exec_run(
        ["psql", "-U", DB_USER, "-d", db_name, "-t", "-A", "-F", "\t", "-c", query]
    )
    line = result.output.decode(errors="replace").strip()
    if not line:
        return None
    uid, login, uname = line.split("\t")
    return {"id": int(uid), "login": login, "name": uname}


def _odoo_shell_argv(name, db_name):
    """argv for a non-interactive `odoo shell -d db_name` on this
    instance's container, with the same --addons-path it actually runs
    with (read back from the label _run_container stores it under, same
    as shell_command below). Without this, a bare `odoo shell` falls back
    to whatever addons_path happens to be baked into the image's own
    odoo.conf — not the instance's real one — so any model that only
    exists in a custom or Enterprise addon (i.e. most of what's actually
    interesting to poke at here) is invisible to the script and raises
    "Invalid model name" / KeyError, even though the real running
    webclient resolves it fine. Found the hard way: a script failing
    on a real custom model while the exact same model worked fine
    through the browser. Takes the instance name rather than an
    already-fetched container object since callers fetch (or don't keep
    around) that container handle in a couple of different ways — cheaper
    to just look it up again here than to make every caller consistent."""
    c = client.containers.get(_container_name(name))
    argv = [
        "docker", "exec", "-i", c.name, "odoo", "shell",
        "-d", db_name, "--shell-interface=python",
        f"--db_host={DB_CONTAINER_NAME}", "--db_port=5432",
        f"--db_user={DB_USER}", f"--db_password={DB_PASSWORD}",
    ]
    addons_path_full = c.labels.get("addons_path_full")
    if addons_path_full:
        argv.append(f"--addons-path={addons_path_full}")
    return argv


def shell_command(name):
    """argv for an interactive `odoo shell` on this instance, with the same
    DB connection args used elsewhere (see connect_as) since entrypoint.sh's
    auto-injection doesn't apply to a plain `docker exec` — and the same
    --addons-path the instance actually runs with (read back from the label
    _run_container stores it under), so Enterprise/design-themes/custom
    addons are importable in the shell too, not just the image's default."""
    c = client.containers.get(_container_name(name))
    db_name = c.labels.get("db_name", name)
    cmd = [
        "docker", "exec", "-it", _container_name(name),
        "odoo", "shell", "-d", db_name, "--shell-interface=python",
        f"--db_host={DB_CONTAINER_NAME}", "--db_port=5432",
        f"--db_user={DB_USER}", f"--db_password={DB_PASSWORD}",
    ]
    addons_path_full = c.labels.get("addons_path_full")
    if addons_path_full:
        cmd.append(f"--addons-path={addons_path_full}")
    return cmd


def terminal_command(name):
    """argv for a plain interactive shell inside the instance's container —
    OS-level access (files, processes, ad-hoc commands), not the Odoo
    Python REPL that shell_command() gives you."""
    return ["docker", "exec", "-it", _container_name(name), "bash"]


_session_cache = {}  # (container_id, uid) -> (sid, minted_at)
_SESSION_CACHE_TTL = 600  # seconds


def connect_as(name, uid):
    """Mint a valid Odoo session authenticated as the given user id, using
    Odoo's own shell (running inside the instance's own container, so it's
    guaranteed compatible with that exact Odoo/werkzeug version) rather than
    reimplementing its session format externally. Returns the session id to
    set as a cookie.

    Minting one this way costs real seconds (spinning up a full odoo-shell
    process), and "Connect as admin" is clicked over and over for the same
    admin uid, so recent sids are cached for a few minutes,
    keyed by (container id, uid) rather than (instance name, uid): if the
    instance gets recreated, its container id changes too, so a stale
    session for a container that no longer exists can never be handed
    back. A cache miss (including a first call, or an entry past its TTL)
    just falls through to minting fresh, same as if there were no cache."""
    c = client.containers.get(_container_name(name))
    db_name = c.labels.get("db_name", name)
    if not _IDENT_RE.match(db_name):
        raise ValueError(f"unexpected db name: {db_name!r}")

    uid = int(uid)
    cache_key = (c.id, uid)
    cached = _session_cache.get(cache_key)
    if cached and time.time() - cached[1] < _SESSION_CACHE_TTL:
        return cached[0]

    script = f"""
try:
    from odoo.http import root
    store = root.session_store
except ImportError:  # Odoo 20 moved the session store into odoo.http.session
    from odoo.http.session import session_store
    store = session_store()
user = env['res.users'].browse({uid})
if not user.exists():
    print("ERROR:no such user")
else:
    session = store.new()
    session.db = env.cr.dbname
    session.uid = user.id
    session.login = user.login
    session.session_token = user._compute_session_token(session.sid)
    session.context = dict(user.context_get() or {{}})
    store.save(session)
    print("SID_RESULT:" + session.sid)
"""
    result = subprocess.run(
        _odoo_shell_argv(name, db_name),
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
    )
    for line in result.stdout.splitlines():
        if line.startswith("SID_RESULT:"):
            sid = line.split(":", 1)[1].strip()
            _session_cache[cache_key] = (sid, time.time())
            return sid
        if line.startswith("ERROR:"):
            raise RuntimeError(line[len("ERROR:"):])
    raise RuntimeError("failed to create session: " + (result.stderr or result.stdout)[-2000:])


def stream_logs(name, tail=200):
    """Yield decoded log lines as they arrive (existing tail, then live)."""
    c = client.containers.get(_container_name(name))
    stream = c.logs(stream=True, follow=True, tail=tail)
    try:
        for chunk in stream:
            yield chunk.decode(errors="replace")
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
