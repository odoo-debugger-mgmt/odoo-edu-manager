"""windows/verify.ps1 looks for the app's containers by name. Those names live in
docker_manager.py; this keeps the two from drifting apart unnoticed."""
import re
from pathlib import Path

from odoo_edu_manager import docker_manager as dm

VERIFY = (Path(__file__).resolve().parent.parent / "windows" / "verify.ps1").read_text()


def verify_value(variable):
    match = re.search(r"^\$" + variable + r"\s*=\s*(?:'([^']*)'|(\d+))\s*$", VERIFY, re.MULTILINE)
    assert match, f"${variable} is not set in windows/verify.ps1"
    return match.group(1) if match.group(1) is not None else int(match.group(2))


def test_verify_looks_for_the_shared_database_by_the_apps_name():
    assert verify_value("DbContainer") == dm.DB_CONTAINER_NAME


def test_verify_looks_for_the_code_server_by_the_apps_name_and_port():
    assert verify_value("CodeServerContainer") == dm.CODE_SERVER_CONTAINER_NAME
    assert verify_value("CodeServerPort") == dm.CODE_SERVER_PORT


def test_verify_recognises_instance_containers_by_the_apps_prefix():
    assert dm._container_name("x") == verify_value("InstancePrefix") + "x"


def test_verify_asks_for_the_label_the_app_puts_on_instance_containers():
    # list_instances() keeps only containers whose "role" label is "odoo"
    assert "label=role=odoo" in VERIFY
    assert 'c.labels.get("role") != "odoo"' in Path(dm.__file__).read_text()
