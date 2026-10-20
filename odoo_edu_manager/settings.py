"""Small persisted app settings, kept apart from per-instance state.

Instance configuration lives on the containers themselves (as labels), so
this file is only for preferences that belong to the manager rather than
to any one instance. Currently that's just how to open a workspace.
"""

import json
import os
import secrets
from urllib.parse import quote

from . import docker_manager as dm

SETTINGS_PATH = os.path.join(dm.DATA_DIR, "settings.json")

# Opening the desktop app is fixed — vscode://file<path> is just what the
# URL handler is. Only the browser target is worth configuring, since it
# could be the container this app manages, a code-server elsewhere, or a
# tunnel. {workspace} is substituted with the absolute path of the
# generated .code-workspace file. windowId=_blank makes the URL handler
# open a new window instead of replacing the last active one.
VSCODE_TEMPLATE = "vscode://file{workspace}?windowId=_blank"
DEFAULT_CODE_SERVER_TEMPLATE = "http://localhost:%d/?workspace={workspace}" % dm.CODE_SERVER_PORT

DEFAULTS = {
    "code_server_template": DEFAULT_CODE_SERVER_TEMPLATE,
    # Generated on first start rather than asked for: it exists to stop the
    # container being an open shell on the network, not as a secret the
    # user picks. Lives in data/, which is gitignored.
    "code_server_password": "",
}


def code_server_password():
    """The managed container's password, generating and persisting one on
    first use so starting it is a single click."""
    current = load().get("code_server_password")
    if not current:
        current = secrets.token_urlsafe(18)
        save({"code_server_password": current})
    return current


def load():
    try:
        with open(SETTINGS_PATH) as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError):
        stored = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return merged


def save(values):
    current = load()
    current.update({k: v for k, v in values.items() if k in DEFAULTS})
    os.makedirs(dm.DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(current, f, indent=2)
    return current


def code_server_template():
    return load().get("code_server_template") or DEFAULT_CODE_SERVER_TEMPLATE


def wsl_distro():
    """The WSL distribution this app is running in, or None anywhere else.
    WSL sets WSL_DISTRO_NAME for every process it starts."""
    return os.environ.get("WSL_DISTRO_NAME") or None


def is_wsl():
    return wsl_distro() is not None


def vscode_template():
    """The desktop button's URL template. Inside WSL the workspace file sits in
    the Linux file system, which VS Code on Windows can only open through its
    WSL integration, so it gets the remote form of the link (that needs VS
    Code's "WSL" extension); everywhere else it is the plain file link."""
    distro = wsl_distro()
    if distro:
        return "vscode://vscode-remote/wsl+" + quote(distro, safe="") + "{workspace}?windowId=_blank"
    return VSCODE_TEMPLATE


def workspace_url(workspace_path, target="vscode"):
    """Build the open-in-editor URL for a generated workspace file.

    `target` is "vscode" (the desktop app's URL handler) or "code-server"
    (whatever browser editor is configured)."""
    template = vscode_template() if target == "vscode" else code_server_template()
    if "{workspace}" not in template:
        raise ValueError("editor URL template must contain {workspace}")
    return template.replace("{workspace}", workspace_path)
