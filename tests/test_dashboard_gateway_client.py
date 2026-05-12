"""Regression tests for the dashboard plugin's ``GatewayClient`` port resolution.

The runtime-control routes (``/myah/v1/admin/*``) are registered on the
``MyahStandaloneRunner``'s aiohttp app, NOT the FastAPI ``api_server``.

* Standalone runner port — ``MYAH_GATEWAY_PORT`` env var, default 8643
  (see ``myah_hermes_plugin.myah_platform.standalone_runner.resolve_default_port``).
* FastAPI api_server port — ``API_SERVER_PORT`` env var, default 8642.
  Hosts ``/v1/*`` chat completions only; ``/myah/v1/admin/*`` is NOT mounted here.

Tier 2A Task 2A.3 (2026-05-07) moved the runtime-control surface from the
api_server to the standalone runner. The dashboard plugin's
``GatewayClient.__init__`` initially kept reading ``API_SERVER_PORT`` ->
every PUT to ``/myah/v1/admin/sessions/{key}/override`` (used by the
session-model-override endpoint) returned 404.

Symptom in production (2026-05-11): every model-picker click triggered

    PUT /api/v1/agent/sessions/{id}/model → 404

even though the dashboard plugin route itself was registered and switch_model()
validation succeeded — the failure was the GatewayClient hitting the wrong port
when proxying to the runtime-control surface.

These tests pin the port resolution to the standalone runner's default.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_common(monkeypatch: pytest.MonkeyPatch):
    """Re-import ``_common`` so the module-level ``gateway_client`` picks up
    fresh env vars. The module caches port + auth at import time."""
    import myah_hermes_plugin.myah_admin.dashboard._common as _common
    return importlib.reload(_common)


class TestGatewayClientPort:
    """``GatewayClient`` must target the MyahStandaloneRunner's port,
    NOT the FastAPI api_server port. The runtime-control routes
    (``/myah/v1/admin/*``) live on the runner only."""

    def test_default_port_is_myah_standalone_runner_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """With no env vars set, GatewayClient must target 8643 (the
        ``MyahStandaloneRunner`` default), NOT 8642 (the api_server)."""
        monkeypatch.delenv("MYAH_GATEWAY_PORT", raising=False)
        monkeypatch.delenv("API_SERVER_PORT", raising=False)

        common = _reload_common(monkeypatch)
        client = common.GatewayClient()

        # Why 8643 specifically: this is ``standalone_runner._DEFAULT_PORT``
        # and the port the MyahAdapter actually binds inside production
        # containers. If a future refactor centralises the default to a
        # shared constant, update both locations.
        assert client._port == 8643, (
            f"GatewayClient targeted port {client._port}; expected 8643 "
            "(MyahStandaloneRunner default). API_SERVER_PORT (8642) hosts "
            "chat-completions only — /myah/v1/admin/* lives on the runner."
        )

    def test_myah_gateway_port_env_var_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """If ``MYAH_GATEWAY_PORT`` is set, GatewayClient must honour it.

        Mirrors ``MyahStandaloneRunner.resolve_default_port``: same env
        var, same fallback behaviour, single source of truth for "where
        does the runtime-control surface live."
        """
        monkeypatch.setenv("MYAH_GATEWAY_PORT", "9876")
        monkeypatch.delenv("API_SERVER_PORT", raising=False)

        common = _reload_common(monkeypatch)
        client = common.GatewayClient()

        assert client._port == 9876

    def test_api_server_port_does_not_change_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Setting ``API_SERVER_PORT`` MUST NOT change which port the
        runtime-control surface targets. The api_server hosts a different
        URL space (``/v1/*``); reading its port would re-introduce the
        B2 production regression.
        """
        monkeypatch.delenv("MYAH_GATEWAY_PORT", raising=False)
        monkeypatch.setenv("API_SERVER_PORT", "1234")  # arbitrary

        common = _reload_common(monkeypatch)
        client = common.GatewayClient()

        assert client._port != 1234, (
            "GatewayClient read API_SERVER_PORT for the runtime-control "
            "surface. This is the B2 regression — runtime_admin lives on "
            "the standalone runner (MYAH_GATEWAY_PORT), not the api_server."
        )

    def test_base_url_targets_myah_admin_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The ``_base_url`` must be ``http://localhost:<runner_port>/myah/v1/admin``.

        This is the exact prefix the runtime_admin handlers use
        (``register_runtime_admin_routes`` mounts under ``/myah/v1/admin``).
        If this string drifts, every dashboard-plugin → gateway call 404s.
        """
        monkeypatch.delenv("MYAH_GATEWAY_PORT", raising=False)
        monkeypatch.delenv("API_SERVER_PORT", raising=False)

        common = _reload_common(monkeypatch)
        client = common.GatewayClient()

        assert client._base_url == "http://localhost:8643/myah/v1/admin"

    def test_invalid_port_falls_back_to_runner_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Non-integer ``MYAH_GATEWAY_PORT`` must fall back to 8643 with a
        warning rather than crashing the GatewayClient at construction
        time. Same defensive contract as
        ``standalone_runner.resolve_default_port``."""
        monkeypatch.setenv("MYAH_GATEWAY_PORT", "not-a-port")
        monkeypatch.delenv("API_SERVER_PORT", raising=False)

        common = _reload_common(monkeypatch)
        client = common.GatewayClient()

        assert client._port == 8643
