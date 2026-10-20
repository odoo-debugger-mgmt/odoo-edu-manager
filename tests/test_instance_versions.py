"""Real end-to-end lifecycle test: for each version, create a fresh
instance with no backup and no Enterprise (so Odoo's own --database=X
auto-init is what has to succeed, on the plain community image), poll it
until the login page actually responds, then tear it down. Slow — each
version not already pulled locally means a multi-GB image download on top
of the boot/module-install time — so this is excluded from a plain
`pytest` run (see pytest.ini) and has to be run explicitly:

    pytest -m slow tests/test_instance_versions.py -v
"""
import time

import pytest
import requests

from odoo_edu_manager import docker_manager as dm

VERSIONS = ["15.0", "16.0", "17.0", "18.0", "19.0"]
BOOT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 5


@pytest.mark.slow
@pytest.mark.parametrize("version", VERSIONS)
def test_fresh_instance_boots_without_backup(version):
    name = f"pytest-{version.replace('.', '')}"
    port = dm.next_free_port()

    # Leftover from a previous interrupted run — clear it so this run
    # starts clean rather than failing on a name collision.
    try:
        dm.remove_instance(name, remove_volumes=True)
    except Exception:
        pass

    try:
        dm.create_instance(name, version, addons_paths=[], port=port)

        container_name = dm._container_name(name)
        deadline = time.time() + BOOT_TIMEOUT_SECONDS
        last_error = None
        booted = False
        while time.time() < deadline:
            container = dm.client.containers.get(container_name)
            container.reload()
            if container.status not in ("running", "created"):
                pytest.fail(
                    f"odoo {version}: container status is {container.status!r}, "
                    f"expected running — logs:\n{container.logs(tail=80).decode(errors='replace')}"
                )
            try:
                resp = requests.get(f"http://localhost:{port}/web/login", timeout=5)
                if resp.status_code == 200:
                    booted = True
                    break
                last_error = f"HTTP {resp.status_code}"
            except requests.RequestException as e:
                last_error = str(e)
            time.sleep(POLL_INTERVAL_SECONDS)

        if not booted:
            container = dm.client.containers.get(container_name)
            pytest.fail(
                f"odoo {version}: never responded 200 on /web/login within "
                f"{BOOT_TIMEOUT_SECONDS}s (last: {last_error}) — logs:\n"
                f"{container.logs(tail=80).decode(errors='replace')}"
            )
    finally:
        try:
            dm.remove_instance(name, remove_volumes=True)
        except Exception:
            pass
