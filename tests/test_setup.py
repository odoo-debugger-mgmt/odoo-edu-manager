"""The one-time first-run steps (`odoo-edu-manager --setup`): prepare the shared database,
fetch the list of Odoo versions and start the managed code-server. The installers run it,
so it must be safe to repeat, must not stop at the first failure, and must never start
the web app.

The database comes first on purpose: the web app prepares it itself before it serves a
single page, so on a machine that has never done so its first start waits for Docker to
download Postgres. The install is the place to have that already behind it."""
import pytest

from odoo_edu_manager import app as app_module
from odoo_edu_manager import docker_manager as dm
from odoo_edu_manager import settings


@pytest.fixture
def world(monkeypatch):
    """Pretend Docker Hub and Docker: record what was asked and let each test make them fail."""
    state = {"calls": [], "db_error": None, "versions_error": None, "code_server_error": None, "served": []}

    def fetch_available_versions():
        state["calls"].append("versions")
        if state["versions_error"]:
            raise RuntimeError(state["versions_error"])
        return ["16.0", "17.0", "18.0"]

    def ensure_code_server(password):
        state["calls"].append(("code-server", password))
        if state["code_server_error"]:
            raise RuntimeError(state["code_server_error"])

    monkeypatch.setattr(dm, "fetch_available_versions", fetch_available_versions)
    monkeypatch.setattr(dm, "ensure_code_server", ensure_code_server)
    monkeypatch.setattr(settings, "code_server_password", lambda: "generated-password")

    def ensure_shared_db():
        state["calls"].append("shared-db")
        if state["db_error"]:
            raise RuntimeError(state["db_error"])

    monkeypatch.setattr(dm, "ensure_shared_db", ensure_shared_db)
    monkeypatch.setattr(app_module.app, "run", lambda **kw: state["served"].append(kw))
    return state


def run_setup(**kwargs):
    lines = []
    ok = app_module.first_run_setup(log=lines.append, **kwargs)
    return ok, "\n".join(lines)


def test_setup_prepares_the_database_fetches_the_versions_and_starts_the_code_server(world):
    ok, out = run_setup()
    assert ok
    # the database first: it is what the web app's first start would otherwise wait for
    assert world["calls"] == ["shared-db", "versions", ("code-server", "generated-password")]
    assert "got 3 versions" in out and "http://localhost:8443/" in out and "Settings page" in out


def test_setup_can_leave_the_code_server_out(world):
    ok, out = run_setup(code_server=False)
    assert ok and world["calls"] == ["shared-db", "versions"] and "code-server" not in out


def test_a_failed_database_does_not_stop_the_other_steps(world):
    world["db_error"] = "Cannot connect to the Docker daemon"
    ok, out = run_setup()
    assert not ok
    assert world["calls"] == ["shared-db", "versions", ("code-server", "generated-password")]
    assert "Cannot connect to the Docker daemon" in out and "tries again every time it starts" in out


def test_a_failed_version_fetch_does_not_stop_the_code_server(world):
    world["versions_error"] = "no route to hub.docker.com"
    ok, out = run_setup()
    assert not ok
    assert world["calls"] == ["shared-db", "versions", ("code-server", "generated-password")]
    assert "no route to hub.docker.com" in out and "Update available versions" in out


def test_a_failed_code_server_is_reported_and_the_other_steps_still_count(world):
    world["code_server_error"] = "Docker is not running"
    ok, out = run_setup()
    assert not ok and world["calls"][:2] == ["shared-db", "versions"]
    assert "Docker is not running" in out and "Start button" in out


def test_setup_can_be_repeated(world):
    assert run_setup()[0] and run_setup()[0]       # nothing in it is a one-shot: both steps are idempotent


@pytest.mark.parametrize("fails, code", [(False, 0), (True, 3)])
def test_the_setup_option_runs_the_steps_and_exits_without_serving(world, fails, code):
    world["versions_error"] = "offline" if fails else None
    with pytest.raises(SystemExit) as exit_info:
        app_module.main(["--setup"])
    assert exit_info.value.code == code
    assert world["served"] == []                   # the web app was not started
    assert world["calls"].count("shared-db") == 1  # the database was prepared once, by the setup itself


def test_help_prints_the_options_and_starts_nothing(world, capsys):
    # found on a real Windows PC: before arguments were parsed, `odoo-edu-manager --help`
    # went straight to starting a second server
    with pytest.raises(SystemExit) as exit_info:
        app_module.main(["--help"])
    assert exit_info.value.code == 0
    assert "--setup" in capsys.readouterr().out
    assert world["served"] == [] and world["calls"] == []


def test_the_skip_option_leaves_the_code_server_out(world):
    with pytest.raises(SystemExit):
        app_module.main(["--setup", "--skip-code-server"])
    assert world["calls"] == ["shared-db", "versions"]


def test_with_no_option_the_web_app_starts_as_before(world, monkeypatch):
    monkeypatch.setenv("ODOO_EDU_MANAGER_PORT", "5077")
    app_module.main([])
    assert world["calls"] == ["shared-db"]
    assert world["served"] == [{"host": "127.0.0.1", "port": 5077, "debug": False, "threaded": True}]
