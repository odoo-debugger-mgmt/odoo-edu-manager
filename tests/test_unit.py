"""Fast tests: pure logic and Flask routing, no containers created or
started. Still requires a reachable Docker daemon, since docker_manager
connects to it at import time (client = docker.from_env()) — that's a
fixed precondition of this whole tool, not something worth mocking around.
"""
import io
import json
import re
import zipfile

import pytest

from odoo_edu_manager import app as app_module
from odoo_edu_manager import docker_manager as dm


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# --- app._parse_addons_paths -------------------------------------------------

def test_parse_addons_paths_strips_and_drops_blanks():
    raw = "  /a/b  \n\n/c/d\n   \n/e/f"
    assert app_module._parse_addons_paths(raw) == ["/a/b", "/c/d", "/e/f"]


def test_parse_addons_paths_empty():
    assert app_module._parse_addons_paths("") == []
    assert app_module._parse_addons_paths("   \n  \n") == []


# --- docker_manager._IDENT_RE ------------------------------------------------

@pytest.mark.parametrize("value", ["ertyad", "school_19", "test-db-1", "A1_b-2"])
def test_ident_re_accepts_safe_names(value):
    assert dm._IDENT_RE.match(value)


@pytest.mark.parametrize("value", [
    "ertyad; DROP TABLE ir_ui_view;", "with space", "quote'd", "semi;colon", "",
])
def test_ident_re_rejects_unsafe_names(value):
    assert not dm._IDENT_RE.match(value)


# --- docker_manager._version_sort_key ---------------------------------------

def test_version_sort_key_orders_numerically_not_lexicographically():
    versions = ["19.0", "9.0", "14.0", "8.0"]
    assert sorted(versions, key=dm._version_sort_key) == ["8.0", "9.0", "14.0", "19.0"]


def test_version_sort_key_puts_non_numeric_after_numeric():
    versions = ["19.0", "latest", "14.0"]
    assert sorted(versions, key=dm._version_sort_key) == ["14.0", "19.0", "latest"]


# --- docker_manager._CLEAN_VERSION_RE ---------------------------------------

@pytest.mark.parametrize("value", ["14.0", "19.0", "8.0"])
def test_clean_version_re_matches_major_dot_zero(value):
    assert dm._CLEAN_VERSION_RE.match(value)


@pytest.mark.parametrize("value", ["19.0-20260817", "19", "latest", "19.1"])
def test_clean_version_re_rejects_everything_else(value):
    assert not dm._CLEAN_VERSION_RE.match(value)


# --- app._extract_backup_db_name ---------------------------------------------

def _make_zip(manifest=None, manifest_bytes=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if manifest_bytes is not None:
            z.writestr("manifest.json", manifest_bytes)
        elif manifest is not None:
            z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("dump.sql", "-- sql dump --")
    buf.seek(0)
    return buf


def test_extract_backup_db_name_reads_manifest(tmp_path):
    zpath = tmp_path / "backup.zip"
    zpath.write_bytes(_make_zip({"db_name": "ertyad_prod"}).read())
    assert app_module._extract_backup_db_name(str(zpath)) == "ertyad_prod"


def test_extract_backup_db_name_rejects_unsafe_name(tmp_path):
    zpath = tmp_path / "backup.zip"
    zpath.write_bytes(_make_zip({"db_name": "not a safe name!"}).read())
    assert app_module._extract_backup_db_name(str(zpath)) is None


def test_extract_backup_db_name_missing_manifest(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("dump.sql", "-- sql dump --")
    zpath = tmp_path / "backup.zip"
    zpath.write_bytes(buf.getvalue())
    assert app_module._extract_backup_db_name(str(zpath)) is None


def test_extract_backup_db_name_not_a_zip(tmp_path):
    zpath = tmp_path / "backup.zip"
    zpath.write_bytes(b"this is not a zip file")
    assert app_module._extract_backup_db_name(str(zpath)) is None


# --- app host pattern ---------------------------------------------------------

def test_direct_host_re_matches_an_instances_own_host_and_nothing_wider():
    m = app_module._DIRECT_HOST_RE.match("ertyad.localhost:5001")
    assert m and m.group(1) == "ertyad"
    assert app_module._DIRECT_HOST_RE.match("ertyad.localhost")
    # One label only: the bare host, and a host with more labels in front, are not an instance's.
    assert not app_module._DIRECT_HOST_RE.match("localhost:5001")
    assert not app_module._DIRECT_HOST_RE.match("ertyad.frame.localhost:5001")


# --- the cookie hop connect-as uses --------------------------------------------

def test_the_bootstrap_hop_sets_the_session_cookie_on_the_instances_host_and_moves_on(client):
    resp = client.get("/_frame_bootstrap?sid=abc123&next=http%3A%2F%2Fertyad.localhost%3A8069%2Fweb",
                      headers={"Host": "ertyad.localhost:5001"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "http://ertyad.localhost:8069/web"
    assert resp.headers["Set-Cookie"] == "session_id=abc123; Path=/"


def test_the_bootstrap_hop_only_answers_on_an_instances_own_host(client):
    # On the manager's own host (or any other) it is just an unknown path: no cookie is ever set from there.
    for host in ("localhost:5001", "ertyad.frame.localhost:5001", "evil.example.com"):
        resp = client.get("/_frame_bootstrap?sid=abc123&next=/web", headers={"Host": host})
        assert resp.status_code == 404 and "Set-Cookie" not in resp.headers, host


def test_the_bootstrap_hop_without_a_session_id_sets_no_cookie(client):
    resp = client.get("/_frame_bootstrap?next=/web", headers={"Host": "ertyad.localhost:5001"})
    assert resp.status_code == 302 and "Set-Cookie" not in resp.headers


# --- Flask routes that don't need a running Odoo instance -------------------

def test_help_page_loads(client):
    resp = client.get("/help")
    assert resp.status_code == 200


def test_new_instance_form_loads(client):
    resp = client.get("/new")
    assert resp.status_code == 200


@pytest.fixture
def pulled_tags(monkeypatch):
    """Stand in for the images the daemon has, given as odoo tags — and only
    those: every other call still goes to the real daemon."""
    class Client:
        def __init__(self, real, images):
            self._real, self.images = real, images

        def __getattr__(self, name):
            return getattr(self._real, name)

    def fake(*tags):
        images = [type("Image", (), {"tags": [f"odoo:{t}"]})() for t in tags]
        stub = type("Images", (), {"list": staticmethod(lambda *a, **k: images)})()
        monkeypatch.setattr(dm, "client", Client(dm.client, stub))
    return fake


def test_pulled_versions_are_listed_in_numeric_order(pulled_tags):
    pulled_tags("9.0", "20.0", "latest", "8.0", "17.0", "10.0")
    assert dm.list_odoo_images() == ["8.0", "9.0", "10.0", "17.0", "20.0", "latest"]


def test_new_instance_form_starts_on_the_highest_pulled_version(client, pulled_tags):
    # as text 9.0 > 20.0, which made every new instance start on Odoo 9
    pulled_tags("9.0", "17.0", "20.0", "8.0", "latest")
    page = client.get("/new").get_data(as_text=True)
    assert 'name="version" required placeholder="e.g. 17.0"\n             value="20.0"' in page


def _instance(**overrides):
    """What dm.list_instances() hands the templates, for one instance."""
    return {"name": "x", "version": "17.0", "db_name": "x", "port": 8069, "status": "running",
            "restore": None, "enterprise": False, "enterprise_path": "", "enterprise_revision": "latest",
            "design_themes_revision": "latest", "odoo_core_revision": "latest",
            "addons_paths": [], "extra_args": "", **overrides}


def _enterprise_checkbox(page):
    return re.search(r'<input type="checkbox" name="include_enterprise"([^>]*)>', page).group(1)


def test_a_new_instance_is_community_unless_enterprise_is_ticked(client):
    # Enterprise is a private repository cloned over SSH, so starting the box
    # ticked made the first create fail for anyone without access to it
    page = client.get("/new").get_data(as_text=True)
    assert "checked" not in _enterprise_checkbox(page)


@pytest.mark.parametrize("enterprise", [True, False])
def test_editing_an_instance_shows_whether_it_has_enterprise(client, monkeypatch, enterprise):
    monkeypatch.setattr(dm, "list_instances", lambda: [_instance(enterprise=enterprise)])
    page = client.get("/instances/x/edit").get_data(as_text=True)
    assert ("checked" in _enterprise_checkbox(page)) is enterprise


def test_the_database_expiration_shortcut_is_not_part_of_this_app(client, monkeypatch):
    monkeypatch.setattr(dm, "list_instances", lambda: [_instance()])
    page = client.get("/").get_data(as_text=True)
    assert ">Remove<" in page and "expiration" not in page.lower()   # the menu is there, minus that item
    assert client.post("/instances/x/extend-expiration").status_code == 404
    assert not hasattr(dm, "extend_database_expiration")


def test_unknown_route_is_404(client):
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404


def test_the_views_and_upgrade_tooling_is_not_part_of_this_app(client):
    for path in ("/views", "/upgrade", "/upgrade/studio", "/api/instances/x/views", "/api/instances/x/disabled-views",
                 "/instances/x/render-view", "/settings/views", "/settings/category-hours"):
        assert client.get(path).status_code in (404, 405), path
    page = client.get("/").get_data(as_text=True)
    assert ">Views<" not in page and ">Upgrade<" not in page


# --- platform detection ------------------------------------------------------
#
# The macOS/Windows behaviour of this app comes down to a handful of
# branches keyed off one daemon fact: Docker Desktop runs the daemon in a
# VM, so `--network host` is the VM's network, not the user's machine.
# macOS cannot be containerised, so these branches are pinned here against
# a faked `client.info()` rather than a real Mac. See tests/install/ for
# the Linux/WSL2 side, which *is* containerisable.

_LINUX_INFO = {"OSType": "linux", "OperatingSystem": "Ubuntu 24.04"}
_DESKTOP_INFO = {"OSType": "linux", "OperatingSystem": "Docker Desktop"}


@pytest.fixture
def daemon_info(monkeypatch):
    """Fake what the Docker daemon reports about itself."""
    def _set(info):
        monkeypatch.setattr(dm.client, "info", lambda: info)
    return _set


def test_host_networking_supported_on_native_linux(daemon_info):
    daemon_info(_LINUX_INFO)
    assert dm.host_networking_supported() is True


def test_host_networking_unsupported_on_docker_desktop(daemon_info):
    daemon_info(_DESKTOP_INFO)
    assert dm.host_networking_supported() is False


def test_host_networking_unsupported_when_daemon_unreachable(monkeypatch):
    def boom():
        raise RuntimeError("daemon gone")
    monkeypatch.setattr(dm.client, "info", boom)
    assert dm.host_networking_supported() is False


def test_code_server_uses_host_network_on_native_linux(daemon_info):
    daemon_info(_LINUX_INFO)
    assert dm._code_server_network_kwargs() == {"network_mode": "host"}


def test_code_server_publishes_port_on_docker_desktop(daemon_info):
    """Host networking would strand the port inside Desktop's VM, so the
    container must publish it to loopback instead."""
    daemon_info(_DESKTOP_INFO)
    kwargs = dm._code_server_network_kwargs()
    assert "network_mode" not in kwargs
    assert kwargs["ports"] == {f"{dm.CODE_SERVER_PORT}/tcp": ("127.0.0.1", dm.CODE_SERVER_PORT)}


def test_debug_attach_host_is_localhost_on_native_linux(daemon_info):
    daemon_info(_LINUX_INFO)
    assert dm.debug_attach_host() == "localhost"


def test_debug_attach_host_is_docker_internal_on_docker_desktop(daemon_info):
    """Without host networking, localhost inside code-server is the
    code-server container — not the machine publishing the debug port."""
    daemon_info(_DESKTOP_INFO)
    assert dm.debug_attach_host() == "host.docker.internal"


# --- BASE_DOMAIN configurability ---------------------------------------------
#
# macOS resolves `localhost` but not `*.localhost`, so the per-instance
# hostnames need a different base domain there. BASE_DOMAIN is read
# at import time, so this reloads the module under a patched environment.

def _app_with_base_domain(monkeypatch, domain):
    import importlib
    monkeypatch.setenv("ODOO_EDU_MANAGER_BASE_DOMAIN", domain)
    return importlib.reload(app_module)


def test_base_domain_defaults_to_localhost(monkeypatch):
    import importlib
    monkeypatch.delenv("ODOO_EDU_MANAGER_BASE_DOMAIN", raising=False)
    mod = importlib.reload(app_module)
    try:
        assert mod.BASE_DOMAIN == "localhost"
        assert mod._DIRECT_HOST_RE.match("ertyad.localhost:5001")
    finally:
        importlib.reload(app_module)


def test_direct_host_re_follows_configured_base_domain(monkeypatch):
    mod = _app_with_base_domain(monkeypatch, "lvh.me")
    try:
        assert mod.BASE_DOMAIN == "lvh.me"
        m = mod._DIRECT_HOST_RE.match("ertyad.lvh.me:5001")
        assert m and m.group(1) == "ertyad"
        # The old default must stop matching, or hosts would cross over.
        assert not mod._DIRECT_HOST_RE.match("ertyad.localhost:5001")
    finally:
        import importlib
        monkeypatch.delenv("ODOO_EDU_MANAGER_BASE_DOMAIN", raising=False)
        importlib.reload(app_module)


def test_base_domain_dots_are_escaped_not_wildcards(monkeypatch):
    """re.escape, not raw interpolation: `lvh.me` must not match `lvhXme`."""
    mod = _app_with_base_domain(monkeypatch, "lvh.me")
    try:
        assert not mod._DIRECT_HOST_RE.match("mydb.lvhXme:5001")
    finally:
        import importlib
        monkeypatch.delenv("ODOO_EDU_MANAGER_BASE_DOMAIN", raising=False)
        importlib.reload(app_module)


def test_dashboard_instance_link_follows_base_domain(monkeypatch):
    """The per-instance link on the dashboard must be built from
    BASE_DOMAIN too — a hardcoded '.localhost' there would send a macOS
    user to a hostname that doesn't resolve, even with lvh.me configured."""
    import importlib
    mod = _app_with_base_domain(monkeypatch, "lvh.me")
    fake = [{
        "name": "ertyad", "container_name": "odoo-inst-ertyad", "status": "running",
        "port": 8069, "version": "19.0", "db_name": "ertyad",
        "enterprise": False, "enterprise_path": "", "design_themes": False,
        "addons_paths": [], "extra_args": "", "debug": False, "debug_port": None,
        "restore": None,
    }]
    monkeypatch.setattr(dm, "list_instances", lambda: fake)
    try:
        mod.app.config["TESTING"] = True
        body = mod.app.test_client().get("/").get_data(as_text=True)
        assert 'href="http://ertyad.lvh.me:8069"' in body
        assert ".localhost:8069" not in body
    finally:
        monkeypatch.delenv("ODOO_EDU_MANAGER_BASE_DOMAIN", raising=False)
        importlib.reload(app_module)


# --- docker_manager.connect_as (session minting script) ----------------------

def _fake_odoo_modules(monkeypatch, odoo20):
    """Install a fake `odoo.http` package shaped like Odoo 19 (root.session_store)
    or Odoo 20 (odoo.http.session.session_store()), with an in-memory store."""
    import sys
    import types

    class Store:
        def __init__(self):
            self.saved = []

        def new(self):
            return types.SimpleNamespace(sid="sid-" + ("20" if odoo20 else "19"))

        def save(self, session):
            self.saved.append(session)

    store = Store()
    odoo = types.ModuleType("odoo")
    http = types.ModuleType("odoo.http")
    if odoo20:
        session_module = types.ModuleType("odoo.http.session")
        session_module.session_store = lambda: store
        monkeypatch.setitem(sys.modules, "odoo.http.session", session_module)
    else:
        http.root = types.SimpleNamespace(session_store=store)
    monkeypatch.setitem(sys.modules, "odoo", odoo)
    monkeypatch.setitem(sys.modules, "odoo.http", http)
    return store


@pytest.mark.parametrize("odoo20", [False, True], ids=["odoo19", "odoo20"])
def test_connect_as_mints_session_on_odoo_19_and_20(monkeypatch, odoo20):
    import contextlib
    import subprocess
    import types

    store = _fake_odoo_modules(monkeypatch, odoo20)
    user = types.SimpleNamespace(
        id=2, login="admin", exists=lambda: True,
        _compute_session_token=lambda sid: "token", context_get=lambda: {"lang": "en_US"},
    )
    class Env:
        cr = types.SimpleNamespace(dbname="db1")

        def __getitem__(self, model):
            return types.SimpleNamespace(browse=lambda uid: user)

    env = Env()

    def fake_run(argv, input, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(input, {"env": env})
        return subprocess.CompletedProcess(argv, 0, stdout=out.getvalue(), stderr="")

    container = types.SimpleNamespace(id=f"c-{odoo20}", labels={"db_name": "db1"})
    monkeypatch.setattr(dm, "client", types.SimpleNamespace(containers=types.SimpleNamespace(get=lambda name: container)))
    monkeypatch.setattr(dm, "_odoo_shell_argv", lambda name, db: ["odoo", "shell"])
    monkeypatch.setattr(dm.subprocess, "run", fake_run)
    monkeypatch.setattr(dm, "_session_cache", {})

    assert dm.connect_as("inst", 2) == ("sid-20" if odoo20 else "sid-19")
    assert store.saved and store.saved[0].uid == 2
