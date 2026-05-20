"""Auth-compat patch tests — see docs/superpowers/plans/2026-05-20-plugin-auth-compat-patch.md.

The plugin's plugin_api.py monkey-patches hermes_cli.web_server._has_valid_session_token
at module import time (when HERMES_WEB_SESSION_TOKEN is set). These tests exercise the
patched function's behavior via `importlib.reload(plugin_api)`.

Test isolation is achieved by capturing the ORIGINAL upstream function at this module's
import time and restoring it in an autouse teardown fixture.
"""
from __future__ import annotations

import importlib

import pytest
from hermes_cli import web_server as _web_server
from starlette.datastructures import Headers

# Capture the original upstream function BEFORE any test (or this module's other imports)
# can trigger a plugin_api reload that would replace it.
_ORIGINAL_HAS_VALID = _web_server._has_valid_session_token


@pytest.fixture(autouse=True)
def _restore_has_valid_session_token():
    """Teardown: restore the upstream `_has_valid_session_token` after each test.

    Tests in this module install the plugin's wrapper via `importlib.reload(plugin_api)`.
    Without this fixture, the wrapper leaks across tests and pollutes any later test that
    expects upstream behavior.
    """
    yield
    _web_server._has_valid_session_token = _ORIGINAL_HAS_VALID


@pytest.fixture
def session_token(monkeypatch):
    """Provide a known HERMES_WEB_SESSION_TOKEN value for tests that need it.

    Returns the token string so tests can construct expected Bearer/X-Token headers.
    """
    token = "test-token-32chars-XXXXXXXXXXXXXX"
    monkeypatch.setenv("HERMES_WEB_SESSION_TOKEN", token)
    monkeypatch.setenv("MYAH_ADAPTER_AUTH_KEY", "test")
    return token


def make_stub_request(headers: dict):
    """Build a stub Request with case-insensitive .headers (Starlette Headers semantics).

    The real Hermes auth_middleware calls `request.headers.get(name)` with mixed-case names.
    `starlette.datastructures.Headers` normalises names case-insensitively, so we use it
    here so tests catch case-sensitivity bugs in the patch.
    """
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return type("StubRequest", (), {"headers": Headers(raw=raw)})()


def test_bearer_with_env_var_token_returns_true_after_reload(session_token):
    """[RED Cycle 1] After plugin_api reload with HERMES_WEB_SESSION_TOKEN set,
    a request carrying `Authorization: Bearer <env-var-token>` is accepted.

    This drives Cycle 1's GREEN step (P1.2) to install a wrapper on
    `web_server._has_valid_session_token` that recognises our env-var Bearer.
    """
    from myah_hermes_plugin.myah_admin.dashboard import plugin_api
    importlib.reload(plugin_api)

    request = make_stub_request({"Authorization": f"Bearer {session_token}"})
    assert _web_server._has_valid_session_token(request) is True


def test_x_hermes_session_token_with_env_var_returns_true_after_reload(session_token):
    """[RED Cycle 2] After plugin_api reload with HERMES_WEB_SESSION_TOKEN set,
    a request carrying `X-Hermes-Session-Token: <env-var-token>` (no Authorization)
    is accepted.

    Drives Cycle 2's GREEN step (P1.4) to add an `X-Hermes-Session-Token` branch
    alongside Cycle 1's Bearer branch. `X-Hermes-Session-Token` is Hermes's
    preferred header name (per `web_server.py:_SESSION_HEADER_NAME`); the plugin's
    own `require_session_token` dependency already accepts both header forms.
    """
    from myah_hermes_plugin.myah_admin.dashboard import plugin_api
    importlib.reload(plugin_api)

    request = make_stub_request({"X-Hermes-Session-Token": session_token})
    assert _web_server._has_valid_session_token(request) is True
