import argparse
import datetime
import fcntl
import glob
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import zipfile
from urllib.parse import quote

import requests
from docker.errors import DockerException
from flask import Flask, render_template, request, redirect, url_for, flash, Response, stream_with_context
from flask_sock import Sock

from . import docker_manager as dm
from . import settings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Under DATA_DIR, not next to the code — same reason as DATA_DIR
# itself: installed as a package, BASE_DIR is site-packages.
UPLOAD_DIR = os.path.join(dm.DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "dev-only-not-secret"
sock = Sock(app)


# Base domain the connect-as hosts hang off. "localhost" is right on Linux,
# where the resolver maps every *.localhost name to 127.0.0.1 — but macOS and
# Windows only resolve "localhost" itself, not subdomains of it, so there
# "Connect as" cannot reach an instance on its own host.
#
# Ports can't substitute for subdomains here: cookies ignore the port, so
# distinct origins are the only thing giving each instance its own cookie
# jar. The portable answer is a domain whose wildcard resolves to 127.0.0.1,
# e.g.
#   ODOO_EDU_MANAGER_BASE_DOMAIN=lvh.me
# The tradeoff is a dependency on somebody else's DNS, and instance names
# appearing in DNS queries — which is why it's opt-in rather than the default.
BASE_DOMAIN = os.environ.get("ODOO_EDU_MANAGER_BASE_DOMAIN", "localhost")
# '<db_name>.localhost' — used by connect-as/connect-as-admin to mint the
# session cookie on a host unique to *this instance* before bouncing straight
# to its real "<db_name>.localhost:<port>" address (the browser talks to the
# real instance directly from then on). Needed because a plain "localhost"
# cookie is shared by every instance at once (cookies aren't port-specific) —
# with more than one instance running, connecting to one can silently
# overwrite another's session cookie in that single shared slot and bounce
# the browser back to its login page.
_DIRECT_HOST_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\." + re.escape(BASE_DOMAIN) + r"(?::\d+)?$")


class CookieBootstrapMiddleware:
    """Serves the one-shot '/_frame_bootstrap' hop that "Connect as" uses to
    hand a freshly minted Odoo session to the browser on the instance's own
    '<db_name>.localhost' host, and passes every other request on to the app
    untouched. (The path keeps the name of the iframe flow it began in.)"""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        host = environ.get("HTTP_HOST", "")
        path = environ.get("PATH_INFO", "")
        if path == "/_frame_bootstrap" and _DIRECT_HOST_RE.match(host):
            return self._bootstrap(environ, start_response)
        return self.wsgi_app(environ, start_response)

    def _bootstrap(self, environ, start_response):
        """One-shot hop that mints the session cookie *from the target
        host's own origin*: a Set-Cookie from a response served on the
        manager app's own host can't land on '<db_name>.localhost', because
        without a Domain= attribute a cookie only matches the exact host that
        set it, and these are genuinely different hostnames from "localhost"
        outright — unlike same-host-different-port, which cookies don't
        distinguish. So the caller redirects here instead, with the session
        id as a one-time query param; this response — genuinely served from
        the target host — sets the cookie for real and bounces onward (`next`
        may be a relative path or a full absolute URL, e.g. to hand off to
        the instance's real port directly)."""
        from urllib.parse import parse_qs
        qs = parse_qs(environ.get("QUERY_STRING", ""))
        sid = (qs.get("sid") or [None])[0]
        next_path = (qs.get("next") or ["/web"])[0]
        headers = [("Location", next_path)]
        if sid:
            headers.append(("Set-Cookie", f"session_id={sid}; Path=/"))
        start_response("302 FOUND", headers)
        return [b""]


app.wsgi_app = CookieBootstrapMiddleware(app.wsgi_app)


def _request_port():
    return request.host.split(":", 1)[1] if ":" in request.host else "80"


@app.context_processor
def _inject_helpers():
    def instance_url(instance):
        """Direct link to an instance, on its own '<db>.<BASE_DOMAIN>'
        host so each instance keeps a separate cookie jar (ports alone
        don't do that). Built here rather than in the template so the
        base domain is never hardcoded."""
        return f"http://{instance['db_name']}.{BASE_DOMAIN}:{instance['port']}"
    return {"instance_url": instance_url}


def _docker_unreachable(exc):
    """True when `exc` says the Docker daemon could not be reached at all — not
    running, not installed, or no permission on its socket — as opposed to
    Docker answering with an error about one container or image. docker-py
    reports the first as a DockerException (building the client) or a requests
    ConnectionError (a call later), with the socket error as the cause."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (requests.exceptions.ConnectionError, ConnectionError, FileNotFoundError, PermissionError)):
            # the version list comes from Docker Hub, not the daemon
            return "hub.docker.com" not in str(getattr(getattr(exc, "request", None), "url", "") or "")
        exc = exc.__cause__ or exc.__context__
    return False


@app.errorhandler(DockerException)
@app.errorhandler(requests.exceptions.ConnectionError)
def _docker_error(exc):
    """Docker not answering is the most common thing to go wrong on a fresh
    install (not started yet after a reboot, or no permission on its socket),
    and a traceback explains none of it — say what to do instead. Any other
    Docker error is somebody else's to handle and is raised as before."""
    if not _docker_unreachable(exc):
        raise exc
    reason = str(exc)
    if sys.platform == "linux":
        platform = "wsl" if settings.is_wsl() else "linux"
    else:
        platform = "desktop"
    return render_template(
        "docker_unavailable.html",
        reason=reason,
        permission="permission denied" in reason.lower(),
        platform=platform,
    ), 503


# Windows paths a person copies from Explorer, for use inside WSL: C:\Users\me\x
# lives at /mnt/c/Users/me/x there, and \\wsl$\<distro>\home\me\x is /home/me/x.
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):(?:[\\/](.*))?$")
_WSL_UNC_RE = re.compile(r"^\\\\(?:wsl\$|wsl\.localhost)\\[^\\]+(?:\\(.*))?$", re.IGNORECASE)


def _host_path(raw):
    """A folder as typed, as this machine's path. Inside WSL a path pasted from
    Windows Explorer is translated to where WSL sees that folder; anywhere else,
    and for every path that is not a Windows one, it is returned as typed."""
    path = (raw or "").strip()
    if not settings.is_wsl():
        return path
    unc = _WSL_UNC_RE.match(path)
    if unc:
        return "/" + (unc.group(1) or "").replace("\\", "/")
    drive = _WIN_DRIVE_RE.match(path)
    if drive:
        rest = (drive.group(2) or "").replace("\\", "/").strip("/")
        return f"/mnt/{drive.group(1).lower()}" + (f"/{rest}" if rest else "")
    return path


def _parse_addons_paths(raw):
    return [_host_path(line) for line in raw.splitlines() if line.strip()]


def _parse_revision(raw):
    """A revision form field: blank means "latest" (track the branch),
    anything else must look like a commit sha — checked here, at the form
    boundary, rather than left for docker_manager to reject deep inside a
    git fetch, where the error would be much less obviously "you typed
    this wrong."""
    raw = (raw or "").strip()
    if not raw:
        return "latest"
    if not dm._REVISION_SHA_RE.match(raw):
        raise ValueError(f"not a commit sha (7-40 hex characters): {raw!r}")
    return raw


def _extract_backup_db_name(zip_path):
    """Standard Odoo backups (from the Database Manager's own Backup button)
    include a manifest.json with the original database name. Custom/scripted
    exports (e.g. a plain pg_dump zipped up by hand) usually don't — in that
    case there's nothing to extract, so just return None."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            with z.open("manifest.json") as f:
                manifest = json.load(f)
        name = manifest.get("db_name")
        name = name.strip() if isinstance(name, str) else None
        # Several other features (Retry restore, Connect as, the Databases
        # page) assume db names look like this — if the manifest's name
        # doesn't, it's safer to fall back than to create one that breaks them.
        return name if name and re.match(r"^[A-Za-z0-9_-]+$", name) else None
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError, OSError):
        return None


@app.route("/api/browse-dirs")
def browse_dirs():
    path = os.path.abspath(request.args.get("path") or os.path.expanduser("~"))
    if not os.path.isdir(path):
        return {"error": f"not a directory: {path}"}, 400
    try:
        dirs = sorted(
            e for e in os.listdir(path)
            if not e.startswith(".") and os.path.isdir(os.path.join(path, e))
        )
    except PermissionError:
        dirs = []
    parent = os.path.dirname(path) if path != os.path.dirname(path) else None
    return {"path": path, "parent": parent, "dirs": dirs, "is_git_repo": dm.is_git_repo(path)}


@app.route("/api/git-branches")
def git_branches():
    path = os.path.abspath(request.args.get("path") or "")
    if not os.path.isdir(path):
        return {"error": f"not a directory: {path}"}, 400
    return dm.list_git_branches(path)


@app.route("/api/copy-addons-repo", methods=["POST"])
def copy_addons_repo():
    path = request.form.get("path", "").strip()
    branch = request.form.get("branch", "").strip()
    if not (path and branch):
        return {"error": "missing path/branch"}, 400
    try:
        return {"path": dm.ensure_addons_copy(path, branch)}
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/api/worktree-addons-repo", methods=["POST"])
def worktree_addons_repo():
    path = request.form.get("path", "").strip()
    branch = request.form.get("branch", "").strip()
    new_branch = request.form.get("new_branch", "").strip() or None
    if not (path and branch):
        return {"error": "missing path/branch"}, 400
    try:
        return {"path": dm.ensure_addons_worktree(path, branch, new_branch=new_branch)}
    except Exception as e:
        return {"error": str(e)}, 400


def _find_uploaded_backup(name):
    matches = sorted(glob.glob(os.path.join(UPLOAD_DIR, f"{name}-*")), key=os.path.getmtime)
    return matches[-1] if matches else None


def _delete_uploaded_backups(name):
    for path in glob.glob(os.path.join(UPLOAD_DIR, f"{name}-*")):
        try:
            os.remove(path)
        except OSError:
            pass


@app.route("/")
def index():
    instances = dm.list_instances()
    return render_template("index.html", instances=instances)


@app.route("/new", methods=["GET"])
def new_instance_form():
    versions = dm.list_available_versions()
    pulled = dm.list_odoo_images()
    port = dm.next_free_port()
    # Prefer the highest pulled numeric version as the default — "latest" is
    # a moving target, so even if it's already pulled, it shouldn't silently
    # win over a concrete version like 14.0 just by sorting last.
    numeric_pulled = [v for v in pulled if v != "latest"]
    numeric_available = [v for v in versions if v != "latest"]
    default_version = (
        numeric_pulled[-1] if numeric_pulled
        else pulled[-1] if pulled
        else numeric_available[-1] if numeric_available
        else versions[-1] if versions
        else "14.0"
    )
    fetched_at = dm.available_versions_fetched_at()
    fetched_at_str = (
        datetime.datetime.fromtimestamp(fetched_at).strftime("%Y-%m-%d %H:%M")
        if fetched_at else None
    )
    return render_template(
        "new.html", versions=versions, pulled=set(pulled), suggested_port=port,
        default_version=default_version, versions_fetched_at=fetched_at_str,
        core_revision_versions=sorted(dm.CORE_ADDONS_REFRESH_VERIFIED_VERSIONS),
    )


@app.route("/versions/update", methods=["POST"])
def update_available_versions():
    try:
        versions = dm.fetch_available_versions()
        flash(f"Fetched {len(versions)} available versions from Docker Hub.", "success")
    except Exception as e:
        flash(f"Failed to fetch available versions: {e}", "error")
    return redirect(request.referrer or url_for("new_instance_form"))


@app.route("/instances", methods=["POST"])
def create_instance():
    name = request.form["name"].strip()
    version = request.form["version"].strip()
    addons_paths = _parse_addons_paths(request.form.get("addons_paths", ""))
    port = int(request.form["port"])
    db_name = request.form.get("db_name", "").strip() or None
    include_enterprise = request.form.get("include_enterprise") == "on"
    enterprise_path = _host_path(request.form.get("enterprise_path", ""))
    extra_args = request.form.get("extra_args", "").strip()
    # No per-instance toggle: every instance gets a debugger, same as
    # design themes already get cloned for every instance unconditionally.
    enable_debug = True
    debug_port = dm.next_free_port(start=port + 1)

    try:
        enterprise_revision = _parse_revision(request.form.get("enterprise_revision", ""))
        design_themes_revision = _parse_revision(request.form.get("design_themes_revision", ""))
        odoo_core_revision = _parse_revision(request.form.get("odoo_core_revision", ""))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("new_instance_form"))

    missing = [p for p in addons_paths if not os.path.isdir(p)]
    if missing:
        flash("Addons path(s) do not exist: " + ", ".join(missing), "error")
        return redirect(url_for("new_instance_form"))
    if enterprise_path and not os.path.isdir(enterprise_path):
        flash(f"Enterprise folder does not exist: {enterprise_path}", "error")
        return redirect(url_for("new_instance_form"))

    backup_path = None
    f = request.files.get("backup_zip")
    if f and f.filename:
        backup_path = os.path.join(UPLOAD_DIR, f"{name}-{f.filename}")
        f.save(backup_path)
        if not db_name:
            extracted = _extract_backup_db_name(backup_path)
            if extracted:
                db_name = extracted
                flash(f"Using database name \"{db_name}\" from the backup's manifest.json.", "info")

    try:
        _, warnings = dm.create_instance(
            name=name,
            version=version,
            addons_paths=addons_paths,
            port=port,
            backup_path=backup_path,
            db_name=db_name,
            include_enterprise=include_enterprise,
            enterprise_path=enterprise_path,
            extra_args=extra_args,
            enable_debug=enable_debug,
            debug_port=debug_port,
            enterprise_revision=enterprise_revision,
            design_themes_revision=design_themes_revision,
            odoo_core_revision=odoo_core_revision,
        )
        for w in warnings:
            flash(w, "warning")
    except ValueError as e:
        flash(f"Could not parse extra CLI arguments: {e}", "error")
        return redirect(url_for("new_instance_form"))
    except Exception as e:
        flash(f"Failed to create instance: {e}", "error")
        return redirect(url_for("new_instance_form"))

    return redirect(url_for("index"))


@app.route("/instances/<name>/edit", methods=["GET"])
def edit_instance_form(name):
    instance = next((i for i in dm.list_instances() if i["name"] == name), None)
    if instance is None:
        flash(f"No such instance: {name}", "error")
        return redirect(url_for("index"))
    versions = dm.list_available_versions()
    pulled = set(dm.list_odoo_images())
    try:
        revisions = dm.get_instance_revisions(name)
    except Exception:
        revisions = []
    return render_template(
        "edit.html", instance=instance, versions=versions, pulled=pulled,
        revisions=revisions, core_revision_versions=sorted(dm.CORE_ADDONS_REFRESH_VERIFIED_VERSIONS),
    )


@app.route("/instances/<name>/edit", methods=["POST"])
def edit_instance(name):
    version = request.form["version"].strip()
    addons_paths = _parse_addons_paths(request.form.get("addons_paths", ""))
    port = int(request.form["port"])
    db_name = request.form.get("db_name", "").strip() or name
    include_enterprise = request.form.get("include_enterprise") == "on"
    enterprise_path = _host_path(request.form.get("enterprise_path", ""))
    extra_args = request.form.get("extra_args", "").strip()
    # Same as create_instance: no per-instance toggle any more.
    enable_debug = True
    debug_port = dm.next_free_port(start=port + 1)

    try:
        enterprise_revision = _parse_revision(request.form.get("enterprise_revision", ""))
        design_themes_revision = _parse_revision(request.form.get("design_themes_revision", ""))
        odoo_core_revision = _parse_revision(request.form.get("odoo_core_revision", ""))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("edit_instance_form", name=name))

    missing = [p for p in addons_paths if not os.path.isdir(p)]
    if missing:
        flash("Addons path(s) do not exist: " + ", ".join(missing), "error")
        return redirect(url_for("edit_instance_form", name=name))
    if enterprise_path and not os.path.isdir(enterprise_path):
        flash(f"Enterprise folder does not exist: {enterprise_path}", "error")
        return redirect(url_for("edit_instance_form", name=name))

    try:
        _, warnings = dm.update_instance(
            name=name,
            version=version,
            addons_paths=addons_paths,
            port=port,
            db_name=db_name,
            include_enterprise=include_enterprise,
            enterprise_path=enterprise_path,
            extra_args=extra_args,
            enable_debug=enable_debug,
            debug_port=debug_port,
            enterprise_revision=enterprise_revision,
            design_themes_revision=design_themes_revision,
            odoo_core_revision=odoo_core_revision,
        )
        for w in warnings:
            flash(w, "warning")
    except ValueError as e:
        flash(f"Could not parse extra CLI arguments: {e}", "error")
        return redirect(url_for("edit_instance_form", name=name))
    except Exception as e:
        flash(f"Failed to update instance: {e}", "error")
        return redirect(url_for("edit_instance_form", name=name))

    return redirect(url_for("index"))


def _friendly_docker_error(e):
    """Turn a raw docker APIError into something a user can act on.

    Without this, a routine port clash renders Werkzeug's full traceback
    page — accurate, but it buries the one sentence that matters and
    looks like the app crashed rather than a container failing to boot."""
    msg = str(e)
    m = re.search(r"Bind for [\d.]+:(\d+) failed: port is already allocated", msg)
    if m:
        port = m.group(1)
        holder = dm.port_holder(port)
        whose = f" by instance '{holder}'" if holder else ""
        return (
            f"Port {port} is already in use{whose}. Two instances were assigned the same port, "
            f"so they can't run at the same time — change this one's port with Edit, or stop "
            f"{holder or 'the other instance'} first."
        )
    if "could not translate host name" in msg or "name resolution" in msg:
        return f"{msg}\nThe container has no network attachment — starting it again should reattach it."
    return msg


@app.route("/instances/<name>/start", methods=["POST"])
def start_instance(name):
    try:
        dm.start_instance(name)
    except Exception as e:
        flash(f"Failed to start {name}: {_friendly_docker_error(e)}", "error")
    return redirect(url_for("index"))


@app.route("/instances/<name>/stop", methods=["POST"])
def stop_instance(name):
    try:
        dm.stop_instance(name)
    except Exception as e:
        flash(f"Failed to stop {name}: {_friendly_docker_error(e)}", "error")
    return redirect(url_for("index"))


@app.route("/instances/<name>/retry-restore", methods=["POST"])
def retry_restore(name):
    backup_path = None
    f = request.files.get("backup_zip")
    if f and f.filename:
        backup_path = os.path.join(UPLOAD_DIR, f"{name}-{f.filename}")
        f.save(backup_path)
    else:
        backup_path = _find_uploaded_backup(name)

    if not backup_path:
        flash(f"No previously uploaded backup found for {name} — attach one to retry.", "error")
        return redirect(url_for("index"))

    try:
        dm.retry_restore(name, backup_path)
        flash(f"Retrying restore for {name} — watch the Restore column for progress.", "info")
    except Exception as e:
        flash(f"Failed to retry restore: {e}", "error")

    return redirect(url_for("index"))


@app.route("/instances/<name>/refresh-revision", methods=["POST"])
def refresh_instance_revision(name):
    repo = request.form.get("repo", "")
    try:
        dm.refresh_instance_revision(name, repo)
        flash(f"{name}: refreshed {repo.replace('_', ' ')} to its latest commit. "
              "Shared with every other instance of this version tracking Latest — "
              "an already-running container won't pick it up until it's restarted.", "success")
    except ValueError as e:
        flash(f"{name}: {e}", "error")
    except Exception as e:
        flash(f"{name}: failed to refresh {repo}: {e}", "error")
    return redirect(url_for("edit_instance_form", name=name))


@app.route("/instances/<name>/vscode")
def open_vscode(name):
    target = request.args.get("target", "vscode")
    if target not in ("vscode", "code-server"):
        flash(f"Unknown editor target: {target}", "error")
        return redirect(url_for("index"))

    # Redirecting to a code-server that isn't up just lands on a browser
    # connection error with no hint as to why, so say it here instead.
    if target == "code-server" and dm.code_server_status()["state"] != "running":
        flash("code-server isn't running — start it below, then try again.", "error")
        return redirect(url_for("settings_page"))

    try:
        workspace_path = dm.generate_vscode_workspace(name)
        url = settings.workspace_url(workspace_path, target)
    except Exception as e:
        flash(f"Failed to build workspace for {name}: {e}", "error")
        return redirect(url_for("index"))
    return redirect(url)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        template = request.form.get("code_server_template", "").strip() or settings.DEFAULT_CODE_SERVER_TEMPLATE
        if "{workspace}" not in template:
            flash("The code-server URL must contain {workspace}.", "error")
            return redirect(url_for("settings_page"))
        settings.save({"code_server_template": template})
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))
    return render_template(
        "settings.html",
        settings=settings.load(),
        vscode_example=settings.workspace_url("/path/to/instance.code-workspace", "vscode"),
        wsl_distro=settings.wsl_distro(),
        code_server_example=settings.workspace_url("/path/to/instance.code-workspace", "code-server"),
        code_server=dm.code_server_status(),
    )


@app.route("/settings/code-server/start", methods=["POST"])
def start_code_server():
    try:
        dm.ensure_code_server(settings.code_server_password())
        flash(f"code-server starting on http://localhost:{dm.CODE_SERVER_PORT}/ — give it a few seconds.", "success")
    except Exception as e:
        flash(f"Failed to start code-server: {e}", "error")
    return redirect(url_for("settings_page"))


@app.route("/settings/code-server/extensions", methods=["POST"])
def install_code_server_extensions():
    try:
        for line in dm.install_code_server_extensions():
            flash(line, "info")
        flash("Reload the code-server browser tab for the extension host to pick these up.", "info")
    except Exception as e:
        flash(f"Failed to install extensions: {e}", "error")
    return redirect(url_for("settings_page"))


@app.route("/settings/code-server/stop", methods=["POST"])
def stop_code_server():
    try:
        dm.stop_code_server()
        flash("code-server stopped.", "success")
    except Exception as e:
        flash(f"Failed to stop code-server: {e}", "error")
    return redirect(url_for("settings_page"))


@app.route("/instances/<name>/remove", methods=["POST"])
def remove_instance(name):
    remove_volumes = request.form.get("remove_volumes") == "on"
    dm.remove_instance(name, remove_volumes=remove_volumes)
    _delete_uploaded_backups(name)
    return redirect(url_for("index"))


@app.route("/databases")
def databases():
    return render_template("databases.html", databases=dm.list_databases())


@app.route("/databases/<name>/delete", methods=["POST"])
def delete_database(name):
    try:
        dm.delete_database(name)
        flash(f"Deleted database {name}.", "success")
    except Exception as e:
        flash(f"Failed to delete database {name}: {e}", "error")
    return redirect(url_for("databases"))


@app.route("/instances/<name>/users")
def instance_users(name):
    try:
        users = dm.list_users(name)
    except Exception as e:
        flash(f"Could not list users for {name}: {e}", "error")
        return redirect(url_for("index"))
    return render_template("users.html", name=name, users=users)


def _connect_as_response(name, uid, error_redirect, path="/web"):
    instance = next((i for i in dm.list_instances() if i["name"] == name), None)
    if instance is None or not instance["port"]:
        flash(f"No such running instance: {name}", "error")
        return redirect(url_for("index"))

    try:
        sid = dm.connect_as(name, uid)
    except Exception as e:
        flash(f"Failed to connect as user {uid}: {e}", "error")
        return redirect(error_redirect)

    # Mint the cookie on "<db_name>.localhost" — a host unique to *this*
    # instance — via the bootstrap hop in CookieBootstrapMiddleware._bootstrap,
    # rather than bare
    # "localhost", which every running instance shares: cookies aren't
    # port-specific, so with more than one instance running, connecting to
    # one could silently overwrite another's session cookie in that single
    # shared slot and bounce the browser back to a login page instead —
    # this is a real bug that shipped, not a hypothetical.
    #
    # This isn't the earlier-rejected "Domain=localhost" approach (that
    # tried to *share* one cookie across hosts by declaring a domain
    # attribute, and real browsers didn't honor it) — this instead mints a
    # separate, genuinely host-only cookie on each instance's own host, no
    # Domain= attribute involved at all, then hands off to that same
    # host's real port directly (no ongoing proxying after this one hop).
    db_name = instance["db_name"]
    target = quote(f"http://{db_name}.{BASE_DOMAIN}:{instance['port']}{path}", safe="")
    return redirect(f"http://{db_name}.{BASE_DOMAIN}:{_request_port()}/_frame_bootstrap?sid={sid}&next={target}")


@app.route("/instances/<name>/connect-as/<int:uid>")
def connect_as(name, uid):
    return _connect_as_response(name, uid, url_for("instance_users", name=name))


@app.route("/instances/<name>/connect-as-admin")
def connect_as_admin(name):
    admin = dm.find_admin_user(name)
    if admin is None:
        flash(f"No active admin user found on {name}.", "error")
        return redirect(url_for("index"))
    # The dashboard's Open field lets you type any path+query here (e.g.
    # ?debug=1 for Odoo's own developer mode, unrelated to this app's own
    # debugpy feature) — defaults to plain /web, and falls back to it for
    # anything that isn't a same-origin relative path, so this can't be
    # turned into an open redirect to somewhere else entirely.
    path = request.args.get("path", "").strip() or "/web"
    if not path.startswith("/") or path.startswith("//"):
        path = "/web"
    return _connect_as_response(name, admin["id"], url_for("index"), path=path)


@app.route("/help")
def help_page():
    return render_template("help.html", core_revision_versions=sorted(dm.CORE_ADDONS_REFRESH_VERIFIED_VERSIONS))


@app.route("/instances/<name>/logs")
def instance_logs(name):
    return render_template("logs.html", name=name)


@app.route("/instances/<name>/activity-log")
def instance_activity_log(name):
    return dm.get_activity_log(name), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/instances/<name>/activity-log/stream")
def instance_activity_log_stream(name):
    def gen():
        for chunk in dm.stream_activity_log(name):
            yield f"data: {json.dumps(chunk)}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _pty_bridge_ws(ws, argv):
    """Bridge a WebSocket to a real PTY running argv — shared by the Odoo
    shell and the plain container terminal, which only differ in command."""
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        argv,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave_fd)

    stop = threading.Event()

    def pump_output():
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.5)
            except OSError:
                break
            if not ready:
                continue
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            try:
                ws.send(data.decode(errors="replace"))
            except Exception:
                break
        stop.set()

    threading.Thread(target=pump_output, daemon=True).start()

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            if isinstance(msg, str) and msg.startswith("\x01RESIZE:"):
                try:
                    cols, rows = msg[len("\x01RESIZE:"):].split(",")
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", int(rows), int(cols), 0, 0))
                except (ValueError, OSError):
                    pass
                continue
            data = msg.encode() if isinstance(msg, str) else msg
            try:
                os.write(master_fd, data)
            except OSError:
                break
    finally:
        stop.set()
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


@app.route("/instances/<name>/shell")
def instance_shell(name):
    return render_template("shell.html", name=name)


@sock.route("/instances/<name>/shell/ws")
def instance_shell_ws(ws, name):
    try:
        argv = dm.shell_command(name)
    except Exception:
        ws.close()
        return
    _pty_bridge_ws(ws, argv)


@app.route("/instances/<name>/terminal")
def instance_terminal(name):
    return render_template("terminal.html", name=name)


@sock.route("/instances/<name>/terminal/ws")
def instance_terminal_ws(ws, name):
    try:
        argv = dm.terminal_command(name)
    except Exception:
        ws.close()
        return
    _pty_bridge_ws(ws, argv)


@app.route("/instances/<name>/docker-info")
def instance_docker_info(name):
    try:
        return dm.get_container_info(name)
    except Exception as e:
        return {"error": str(e)}, 404


@app.route("/instances/<name>/ready")
def instance_ready(name):
    """Polled by the dashboard/Users page to enable the Connect buttons
    only once there's an actual login screen to connect to — see
    dm.instance_ready. A lookup failure (removed mid-poll, say) just
    means "not ready", not an error worth surfacing to the poller."""
    try:
        return {"ready": dm.instance_ready(name)}
    except Exception:
        return {"ready": False}


@app.route("/instances/<name>/logs/stream")
def instance_logs_stream(name):
    def gen():
        for line in dm.stream_logs(name):
            yield f"data: {json.dumps(line)}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def first_run_setup(code_server=True, log=print):
    """The steps a new install does once, so that the first visit has nothing left
    to wait for:

    * prepare the shared database, the Postgres container every instance uses. The web
      app does this itself before it starts serving, so on a machine that has never run
      it the first start sits there, with no page at all, while Docker downloads the
      Postgres image (a download of about 170 MB);
    * fetch the list of Odoo versions (what the "Update available versions" button does);
    * start the managed code-server (the Settings page's Start button, which the first
      time downloads about 350 MB).

    None is essential and the app does or offers each of them later, so a failure is
    reported and the other steps still run. Returns True when everything went through."""
    ok = True
    log("Preparing the shared database (the first time this downloads the Postgres image) ...")
    try:
        dm.ensure_shared_db()
        log("  running.")
    except Exception as e:
        ok = False
        log(f"  could not prepare it ({e}). The app tries again every time it starts.")
    log("Fetching the list of Odoo versions from Docker Hub ...")
    try:
        versions = dm.fetch_available_versions()
        log(f"  got {len(versions)} versions.")
    except Exception as e:
        ok = False
        log(f"  could not fetch it ({e}). The New instance page has an \"Update available versions\" button for later.")
    if code_server:
        log("Starting code-server (the first time this downloads about 350 MB) ...")
        try:
            dm.ensure_code_server(settings.code_server_password())
            log(f"  running at http://localhost:{dm.CODE_SERVER_PORT}/ - its password is on the Settings page.")
        except Exception as e:
            ok = False
            log(f"  could not start it ({e}). The Settings page has a Start button for later.")
    return ok


def main(argv=None):
    """Console-script entry point.

    Binds loopback by default, and debug off. Previously this served on
    0.0.0.0 with the Werkzeug debugger enabled and no authentication of
    any kind — while being able to create containers, drop databases and
    execute code. On any shared network that was remote code execution
    for anyone who could reach the port. Both are still overridable for
    anyone who genuinely wants them, but they have to be asked for.

    `--setup` does the one-time first-run steps (see first_run_setup) and
    exits instead of starting the web app: exit code 0 when everything went
    through, 3 when something was skipped."""
    parser = argparse.ArgumentParser(prog="odoo-edu-manager", description="Odoo Edu Manager: run and manage Docker-based Odoo instances.")
    parser.add_argument("--setup", action="store_true",
                        help="do the one-time first-run steps (prepare the shared database, fetch the list of Odoo versions, start the managed code-server) and exit")
    parser.add_argument("--skip-code-server", action="store_true", help="with --setup: do not start the code-server")
    args = parser.parse_args(argv)
    if args.setup:
        sys.exit(0 if first_run_setup(code_server=not args.skip_code_server) else 3)

    host = os.environ.get("ODOO_EDU_MANAGER_HOST", "127.0.0.1")
    port = int(os.environ.get("ODOO_EDU_MANAGER_PORT", "5001"))
    debug = os.environ.get("ODOO_EDU_MANAGER_DEBUG", "").lower() in ("1", "true", "yes")
    dm.ensure_shared_db()
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
