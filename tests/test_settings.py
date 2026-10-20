"""settings.py: the load/save round-trip, and the editor URLs it builds.
Isolated from the real settings.json via monkeypatching SETTINGS_PATH to a
tmp file, since that path is computed once at import time from DATA_DIR and
can't be redirected via the ODOO_EDU_MANAGER_DATA env var after the fact.
"""
import pytest

from odoo_edu_manager import settings


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    return settings


def test_defaults_are_returned_when_nothing_was_saved(isolated_settings):
    loaded = isolated_settings.load()
    assert loaded["code_server_template"] == isolated_settings.DEFAULT_CODE_SERVER_TEMPLATE
    assert set(loaded) == {"code_server_template", "code_server_password"}


def test_a_saved_value_round_trips(isolated_settings):
    isolated_settings.save({"code_server_template": "http://example.test/?workspace={workspace}"})
    assert isolated_settings.code_server_template() == "http://example.test/?workspace={workspace}"


def test_unknown_keys_are_filtered_out(isolated_settings):
    isolated_settings.save({"not_a_real_setting": "x"})
    assert "not_a_real_setting" not in isolated_settings.load()


def test_save_only_updates_given_keys(isolated_settings):
    isolated_settings.save({"code_server_template": "http://a.test/?workspace={workspace}"})
    isolated_settings.save({"code_server_password": "secret"})
    loaded = isolated_settings.load()
    assert loaded["code_server_template"] == "http://a.test/?workspace={workspace}"
    assert loaded["code_server_password"] == "secret"


def test_the_code_server_password_is_generated_once_and_kept(isolated_settings):
    first = isolated_settings.code_server_password()
    assert first and isolated_settings.code_server_password() == first


def test_a_template_without_the_workspace_placeholder_is_refused(isolated_settings):
    isolated_settings.save({"code_server_template": "http://example.test/"})
    with pytest.raises(ValueError):
        isolated_settings.workspace_url("/w.code-workspace", "code-server")


def test_vscode_url_opens_a_new_window(isolated_settings):
    # Without windowId=_blank, VS Code replaces the last active window.
    url = isolated_settings.workspace_url("/data/workspaces/inst.code-workspace", "vscode")
    assert url == "vscode://file/data/workspaces/inst.code-workspace?windowId=_blank"
