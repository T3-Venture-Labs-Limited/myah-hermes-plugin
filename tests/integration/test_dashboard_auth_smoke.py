"""End-to-end smoke for the dashboard auth-compat patch.

LIMITATION ON THE CURRENT HERMES PIN
====================================
The plugin's hermes-agent pin (``faa13e49f``, 2026-05-07) PRE-DATES the
upstream commit ``ec9329e`` (2026-05-10) that removes ``/api/plugins/*``
from ``auth_middleware``'s exemption list. On this pin, plugin routes are
middleware-exempt and ``/api/plugins/myah-admin/health`` returns 200 with
OR without auth — the patch has no observable wire-level effect because
there is nothing to defend against yet.

This test still has value:
  * It exercises plugin discovery + loading end-to-end.
  * It catches regressions where the patch raises at import time, breaks
    the router, or otherwise prevents the dashboard from booting.
  * It becomes a meaningful regression catcher (no-auth=401, with-auth=200)
    when the Hermes pin is bumped past ``ec9329e`` (planned in ``myah-hosted``
    P3 of the overall plan).

ENV ISOLATION
=============
``~/.hermes/.env`` has ``HERMES_WEB_SESSION_TOKEN`` baked in from prior runs
of this dev machine. ``hermes_cli.env_loader.load_hermes_dotenv`` uses
``override=True`` — so passing ``HERMES_WEB_SESSION_TOKEN`` via subprocess
env is NOT enough. The test isolates ``HERMES_HOME`` to a tmpdir AND writes
a ``.env`` with the known token there, so the dashboard's dotenv load picks
up OUR token.

Marked ``integration`` so it can be excluded from quick runs.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

INTEGRATION_TIMEOUT_S = 30.0


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _copy_plugin_shim_into(hermes_home: Path) -> None:
    """Copy the production plugin shim (manifest + plugin_api.py) so the
    dashboard's discovery routine finds myah-admin under the isolated HOME.

    The shim's ``from myah_hermes_plugin... import router`` still resolves
    against the editable install at the worktree path, which is what we
    want (we're testing the patch in the editable code, not a stale
    snapshot).
    """
    src = Path.home() / ".hermes" / "plugins" / "myah-admin"
    if not src.is_dir():
        pytest.skip(
            f"Production shim not installed at {src}; run "
            "`myah-hermes-plugin install --dashboard-only` first."
        )
    dst = hermes_home / "plugins" / "myah-admin"
    shutil.copytree(src, dst)


@pytest.mark.integration
def test_dashboard_boots_with_patch_and_serves_plugin_route():
    """The patched plugin loads cleanly in a real dashboard subprocess.

    Asserts:
      * Dashboard binds and serves ``/api/status`` (boot succeeded).
      * ``/api/plugins/myah-admin/health`` is reachable end-to-end
        with ``Authorization: Bearer <known token>`` (200, expected body).

    NOTE: on the current Hermes pin, no-auth ALSO returns 200 here because
    plugin routes are middleware-exempt. The with-auth assertion is the
    scaffold that becomes meaningful once the pin is bumped past
    ``ec9329e``.
    """
    token = "smoke-test-token-bbbbbbbbbbbbbbbb"
    port = _pick_free_port()

    with tempfile.TemporaryDirectory(prefix="hermes_smoke_") as tmp:
        hermes_home = Path(tmp) / "hermes"
        hermes_home.mkdir()

        # Bake the token into <HERMES_HOME>/.env so dotenv-override leaves
        # it in place (subprocess env alone would lose to ~/.hermes/.env).
        (hermes_home / ".env").write_text(
            f"HERMES_WEB_SESSION_TOKEN={token}\n"
        )

        _copy_plugin_shim_into(hermes_home)

        env = {
            **os.environ,
            "HERMES_HOME": str(hermes_home),
            "HERMES_WEB_SESSION_TOKEN": token,
        }

        proc = subprocess.Popen(
            ["hermes", "dashboard", "--port", str(port), "--no-open"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            deadline = time.monotonic() + INTEGRATION_TIMEOUT_S
            ready = False
            last_err: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    r = httpx.get(
                        f"http://127.0.0.1:{port}/api/status",
                        timeout=2.0,
                    )
                    if r.status_code == 200:
                        ready = True
                        break
                except httpx.HTTPError as e:
                    last_err = e
                time.sleep(0.5)

            if not ready:
                stdout, stderr = proc.communicate(timeout=2.0)
                pytest.fail(
                    f"Dashboard did not become ready within "
                    f"{INTEGRATION_TIMEOUT_S}s.\n"
                    f"Last error: {last_err!r}\n"
                    f"--- stdout ---\n{stdout[:2000]}\n"
                    f"--- stderr ---\n{stderr[:2000]}"
                )

            r = httpx.get(
                f"http://127.0.0.1:{port}/api/plugins/myah-admin/health",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            assert r.status_code == 200, f"got {r.status_code}: {r.text}"
            assert r.json() == {"status": "ok", "plugin": "myah-admin"}

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
