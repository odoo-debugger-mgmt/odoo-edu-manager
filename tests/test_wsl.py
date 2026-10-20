"""What changes when Docker is not there yet, and when the app runs inside WSL:
the page that explains a missing Docker, the lazy Docker client behind it, the
VS Code link and the Windows paths people paste into the folder fields."""
import docker
import pytest
import requests
from docker.errors import DockerException, NotFound

from odoo_edu_manager import app as app_module
from odoo_edu_manager import docker_manager as dm
from odoo_edu_manager import settings


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# --- the Docker client is only built when it is first needed -----------------

def test_the_docker_client_is_not_built_until_it_is_used(monkeypatch):
    built = []
    real = type("Real", (), {"containers": "c", "images": "i"})()
    monkeypatch.setattr(docker, "from_env", lambda: built.append(1) or real)
    lazy = dm._LazyDockerClient()
    assert built == []                      # creating it contacts nothing
    assert (lazy.containers, lazy.images) == ("c", "i")
    assert built == [1]                     # built once, then reused


def test_a_failed_connection_is_retried_on_the_next_use(monkeypatch):
    attempts = []

    def from_env():
        attempts.append(1)
        if len(attempts) == 1:
            raise DockerException("Error while fetching server API version: refused")
        return type("Real", (), {"ping": lambda self: "pong"})()

    monkeypatch.setattr(docker, "from_env", from_env)
    lazy = dm._LazyDockerClient()
    with pytest.raises(DockerException):
        lazy.ping
    assert lazy.ping() == "pong"            # Docker came up: no restart of the app needed
    assert len(attempts) == 2


# --- a missing Docker is explained, not a traceback --------------------------

class _DownClient:
    """A Docker client whose every call fails with `exc`."""
    def __init__(self, exc):
        self._exc = exc

    def __getattr__(self, name):
        raise self._exc


def _unreachable(reason="Connection refused", cause_type=requests.exceptions.ConnectionError):
    exc = DockerException(f"Error while fetching server API version: {reason}")
    exc.__cause__ = cause_type(reason)
    return exc


def test_docker_not_running_shows_what_to_do_instead_of_a_traceback(client, monkeypatch):
    monkeypatch.setattr(dm, "client", _DownClient(_unreachable()))
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    assert resp.status_code == 503
    assert "Docker isn't reachable" in page and "Try again" in page
    assert "sudo systemctl start docker" in page
    assert "Connection refused" in page      # what Docker said, for whoever has to debug it


def test_inside_wsl_the_page_says_how_to_start_docker_there(client, monkeypatch):
    monkeypatch.setattr(dm, "client", _DownClient(_unreachable()))
    monkeypatch.setenv("WSL_DISTRO_NAME", "OdooEdu")
    page = client.get("/").get_data(as_text=True)
    assert "sudo service docker start" in page and "WSL integration" in page


def test_no_permission_on_the_socket_says_how_to_get_it(client, monkeypatch):
    err = _unreachable("Permission denied", cause_type=PermissionError)
    monkeypatch.setattr(dm, "client", _DownClient(err))
    page = client.get("/").get_data(as_text=True)
    assert client.get("/").status_code == 503
    assert "sudo usermod -aG docker $USER" in page


def test_a_connection_error_after_the_client_was_built_is_explained_too(client, monkeypatch):
    # docker-py raises a plain requests ConnectionError once it has a client
    monkeypatch.setattr(dm, "client", _DownClient(requests.exceptions.ConnectionError("Connection aborted")))
    assert client.get("/").status_code == 503


def test_other_docker_errors_are_not_reported_as_docker_being_down(client, monkeypatch):
    monkeypatch.setattr(dm, "client", _DownClient(NotFound("no such container")))
    with pytest.raises(NotFound):            # unchanged: handled (or not) by whoever called Docker
        client.get("/")


def test_a_docker_hub_outage_is_not_blamed_on_docker(client, monkeypatch):
    hub = requests.exceptions.ConnectionError(
        "hub unreachable", request=requests.Request("GET", "https://hub.docker.com/v2/x").prepare())
    monkeypatch.setattr(dm, "client", _DownClient(hub))
    with pytest.raises(requests.exceptions.ConnectionError):
        client.get("/")


# --- running inside WSL ------------------------------------------------------

def test_the_vscode_button_opens_the_workspace_through_wsl(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "OdooEdu")
    url = settings.workspace_url("/home/edu/.local/share/x/y.code-workspace")
    assert url == "vscode://vscode-remote/wsl+OdooEdu/home/edu/.local/share/x/y.code-workspace?windowId=_blank"


def test_a_distro_name_with_a_space_is_quoted_in_the_link(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu 24.04")
    assert settings.workspace_url("/w.code-workspace").startswith("vscode://vscode-remote/wsl+Ubuntu%2024.04/w")


def test_outside_wsl_the_vscode_link_is_the_plain_file_link(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert settings.workspace_url("/w.code-workspace") == "vscode://file/w.code-workspace?windowId=_blank"


def test_the_code_server_link_is_the_same_inside_wsl(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    plain = settings.workspace_url("/w.code-workspace", "code-server")
    monkeypatch.setenv("WSL_DISTRO_NAME", "OdooEdu")
    assert settings.workspace_url("/w.code-workspace", "code-server") == plain


def test_the_settings_page_mentions_the_wsl_extension_only_inside_wsl(client, monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert "remote-wsl" not in client.get("/settings").get_data(as_text=True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "OdooEdu")
    assert "remote-wsl" in client.get("/settings").get_data(as_text=True)


@pytest.mark.parametrize("typed, expected", [
    ("C:\\Users\\Ana\\exercises", "/mnt/c/Users/Ana/exercises"),
    ("c:/Users/Ana/exercises", "/mnt/c/Users/Ana/exercises"),
    ("D:\\", "/mnt/d"),
    ("D:", "/mnt/d"),
    ("C:\\Users\\Ana\\My Odoo Work\\", "/mnt/c/Users/Ana/My Odoo Work"),
    ("\\\\wsl$\\OdooEdu\\home\\edu\\addons", "/home/edu/addons"),
    ("\\\\wsl.localhost\\OdooEdu\\home\\edu\\addons", "/home/edu/addons"),
    ("/home/edu/addons", "/home/edu/addons"),
    ("  /mnt/c/Users/Ana/x  ", "/mnt/c/Users/Ana/x"),
])
def test_windows_paths_pasted_inside_wsl_become_wsl_paths(monkeypatch, typed, expected):
    monkeypatch.setenv("WSL_DISTRO_NAME", "OdooEdu")
    assert app_module._host_path(typed) == expected


def test_paths_are_left_alone_outside_wsl(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert app_module._host_path("C:\\Users\\Ana\\x") == "C:\\Users\\Ana\\x"


def test_the_addons_field_translates_each_line(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "OdooEdu")
    raw = "C:\\Users\\Ana\\school\n\n/home/edu/own\n D:\\work "
    assert app_module._parse_addons_paths(raw) == ["/mnt/c/Users/Ana/school", "/home/edu/own", "/mnt/d/work"]
