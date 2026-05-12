"""Tests for the myah-admin plugin's provider/credential routes.

Exercises ``myah_hermes_plugin.myah_admin.dashboard._providers`` directly
via FastAPI's ``TestClient``. The router is mounted on a bare FastAPI app
so the dashboard process is not required.

Auth is disabled by leaving ``HERMES_WEB_SESSION_TOKEN`` unset — the
``require_session_token`` dependency accepts all requests in that case
(matches the legacy aiohttp behaviour).

Phase 4e (2026-05-07): test was migrated from
``agent/hermes/tests/plugins/`` to the pip-plugin's tests/ directory. The
module-loading boilerplate that worked around the hyphen in
``plugins/myah-admin/`` is gone — the dashboard now lives inside the pip
package as a proper Python package and imports cleanly.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import _providers as _providers_module


@pytest.fixture(scope='module')
def providers_mod() -> types.ModuleType:
    """The migrated _providers module (clean package member)."""
    return _providers_module


@pytest.fixture
def app(providers_mod) -> FastAPI:
    """Bare FastAPI app with the plugin's router mounted at /."""
    application = FastAPI()
    application.include_router(providers_mod.router)
    return application


@pytest.fixture
def client(app, monkeypatch) -> TestClient:
    """TestClient with auth disabled (no HERMES_WEB_SESSION_TOKEN env var)."""
    monkeypatch.delenv("HERMES_WEB_SESSION_TOKEN", raising=False)
    return TestClient(app)


# ── GET /providers ──────────────────────────────────────────────────────────


def test_list_providers_v1_filter(client, providers_mod):
    """GET /providers?visible=v1 returns only v1_visible entries from the
    merged catalog (matches legacy ?visible=v1 contract)."""
    fake_catalog = {
        "alpha": {"id": "alpha", "v1_visible": True, "write_type": "env_var"},
        "beta": {"id": "beta", "v1_visible": False, "write_type": "env_var"},
        "gamma": {"id": "gamma", "v1_visible": True, "write_type": "env_var"},
    }

    async def _fake_build():
        return fake_catalog

    with patch.object(providers_mod, "_build_catalog", _fake_build):
        resp = client.get("/providers", params={"visible": "v1"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"alpha", "gamma"}
    assert body["alpha"]["v1_visible"] is True


def test_list_providers_all_default(client, providers_mod):
    """Default (no ?visible) returns the complete catalog (legacy default
    is ``all`` — query param defaults to ``all``)."""
    fake_catalog = {
        "alpha": {"id": "alpha", "v1_visible": True},
        "beta": {"id": "beta", "v1_visible": False},
    }

    async def _fake_build():
        return fake_catalog

    with patch.object(providers_mod, "_build_catalog", _fake_build):
        resp = client.get("/providers")

    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"alpha", "beta"}


def test_list_providers_against_real_catalog(client):
    """Smoke test against the real MYAH_OVERRIDES + CANONICAL_PROVIDERS
    catalog. Confirms the plugin's _build_catalog produces the same
    v1_visible filter the frontend onboarding depends on.

    Skipped in CI environments that do not pip-install the plugin —
    Tier 2A Task 2A.6 moved ``myah_overrides.py`` from ``hermes_cli/``
    into ``myah_hermes_plugin.myah_admin``, but ``tests.yml`` only
    runs ``uv pip install -e ".[all,dev]"`` against the hermes core
    package. Local dev runs (where the plugin is editable-installed)
    still exercise this assertion. The plugin's own test suite covers
    the catalog-build path independently.
    """
    MYAH_OVERRIDES = pytest.importorskip(
        "myah_hermes_plugin.myah_admin.myah_overrides",
    ).MYAH_OVERRIDES

    resp = client.get("/providers", params={"visible": "v1"})
    assert resp.status_code == 200
    body = resp.json()

    expected = {
        slug for slug, override in MYAH_OVERRIDES.items()
        if override.get("v1_visible")
    }
    assert set(body.keys()) == expected


# ── GET /providers/{id}/models ──────────────────────────────────────────────


def test_provider_models_returns_id_name_pairs(client, providers_mod):
    """GET /providers/openrouter/models returns [{id, name}, ...] for a
    known provider."""
    fake_catalog = {"openrouter": {"id": "openrouter"}}

    async def _fake_build():
        return fake_catalog

    fake_ids = ["model-a", "model-b"]
    with patch.object(providers_mod, "_build_catalog", _fake_build), \
         patch("hermes_cli.models.provider_model_ids", return_value=fake_ids):
        resp = client.get("/providers/openrouter/models")

    assert resp.status_code == 200
    assert resp.json() == [
        {"id": "model-a", "name": "model-a"},
        {"id": "model-b", "name": "model-b"},
    ]


def test_provider_models_unknown_provider_returns_404(client, providers_mod):
    async def _fake_build():
        return {"openai": {"id": "openai"}}

    with patch.object(providers_mod, "_build_catalog", _fake_build):
        resp = client.get("/providers/nonexistent/models")

    assert resp.status_code == 404
    assert "unknown provider" in resp.json()["detail"]


def test_provider_models_lookup_failure_returns_502(client, providers_mod):
    """If provider_model_ids raises, return 502 with the error message."""
    async def _fake_build():
        return {"openrouter": {"id": "openrouter"}}

    def _broken(_):
        raise RuntimeError("upstream unreachable")

    with patch.object(providers_mod, "_build_catalog", _fake_build), \
         patch("hermes_cli.models.provider_model_ids", side_effect=_broken):
        resp = client.get("/providers/openrouter/models")

    assert resp.status_code == 502
    assert "upstream unreachable" in resp.json()["detail"]


# ── POST /providers/{id}/credential ─────────────────────────────────────────


def _fake_catalog_entry(write_type="env_var", env_var="OPENROUTER_API_KEY"):
    return {
        "alpha": {
            "id": "alpha",
            "write_type": write_type,
            "env_var": env_var,
            "validation": {"url": "https://example.com/v",
                           "method": "GET", "auth": "bearer"},
        }
    }


def test_connect_credential_rejects_bad_key(client, providers_mod):
    """If _validate_api_key returns (False, ...), respond 400."""
    async def _fake_build():
        return _fake_catalog_entry()

    async def _fake_validate(_entry, _key):
        return (False, "auth denied by provider (HTTP 401)")

    with patch.object(providers_mod, "_build_catalog", _fake_build), \
         patch.object(providers_mod, "_validate_api_key", _fake_validate):
        resp = client.post(
            "/providers/alpha/credential",
            json={"api_key": "bad-key"},
        )

    assert resp.status_code == 400
    assert "validation failed" in resp.json()["detail"]


def test_connect_credential_rejects_empty_key(client, providers_mod):
    """Empty/whitespace api_key returns 400 before catalog lookup."""
    async def _fake_build():
        return _fake_catalog_entry()

    with patch.object(providers_mod, "_build_catalog", _fake_build):
        resp = client.post(
            "/providers/alpha/credential",
            json={"api_key": "   "},
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "api_key required"


def test_connect_credential_unknown_provider_returns_404(client, providers_mod):
    async def _fake_build():
        return _fake_catalog_entry()

    with patch.object(providers_mod, "_build_catalog", _fake_build):
        resp = client.post(
            "/providers/nonexistent/credential",
            json={"api_key": "good-key"},
        )

    assert resp.status_code == 404


def test_connect_credential_success_writes_env_and_pool(client, providers_mod):
    """Happy path: validation accepts, env var saved, pool entry added."""
    async def _fake_build():
        return _fake_catalog_entry()

    async def _fake_validate(_entry, _key):
        return (True, "validated")

    fake_pool = MagicMock()
    fake_pool.add_entry = MagicMock()

    save_env_calls = []

    def _fake_save_env(key, value):
        save_env_calls.append((key, value))

    with patch.object(providers_mod, "_build_catalog", _fake_build), \
         patch.object(providers_mod, "_validate_api_key", _fake_validate), \
         patch("hermes_cli.config.save_env_value", _fake_save_env), \
         patch("agent.credential_pool.load_pool", return_value=fake_pool):
        resp = client.post(
            "/providers/alpha/credential",
            json={"api_key": "sk-good-key-1234", "label": "primary"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is True
    assert body["key_last_four"] == "1234"
    assert body["entry_id"].startswith("myah-")

    # env var written
    assert save_env_calls == [("OPENROUTER_API_KEY", "sk-good-key-1234")]
    # pool entry added with the same id we returned
    fake_pool.add_entry.assert_called_once()
    cred = fake_pool.add_entry.call_args[0][0]
    assert cred.access_token == "sk-good-key-1234"
    assert cred.label == "primary"
    assert cred.id == body["entry_id"]


def test_connect_credential_custom_provider_writes_config(client, providers_mod):
    """write_type=custom_provider also updates config.yaml's providers block."""
    catalog = {
        "openai": {
            "id": "openai",
            "write_type": "custom_provider",
            "env_var": "OPENAI_API_KEY",
            "validation": None,
            "custom_provider": {
                "slug": "custom:openai-direct",
                "base_url": "https://api.openai.com/v1",
                "api_mode": "openai_chat",
            },
        }
    }

    async def _fake_build():
        return catalog

    async def _fake_validate(_entry, _key):
        return (True, "no validation URL configured")

    fake_cfg: dict = {}

    def _fake_load_config():
        return fake_cfg

    saved_cfgs = []

    def _fake_save_config(cfg):
        saved_cfgs.append(dict(cfg))

    with patch.object(providers_mod, "_build_catalog", _fake_build), \
         patch.object(providers_mod, "_validate_api_key", _fake_validate), \
         patch("hermes_cli.config.save_env_value", lambda *_a, **_k: None), \
         patch("hermes_cli.config.load_config", _fake_load_config), \
         patch("hermes_cli.config.save_config", _fake_save_config), \
         patch("agent.credential_pool.load_pool", return_value=MagicMock()):
        resp = client.post(
            "/providers/openai/credential",
            json={"api_key": "sk-1234"},
        )

    assert resp.status_code == 200
    assert saved_cfgs, "save_config should have been called"
    assert "providers" in saved_cfgs[-1]
    block = saved_cfgs[-1]["providers"]["custom:openai-direct"]
    assert block["base_url"] == "https://api.openai.com/v1"
    assert block["key_env"] == "OPENAI_API_KEY"
    assert block["api_mode"] == "openai_chat"


def test_connect_credential_unsupported_write_type(client, providers_mod):
    """write_type=oauth_external rejects with 400 + OAuth hint."""
    async def _fake_build():
        return {"some-oauth": {"id": "some-oauth", "write_type": "oauth_external"}}

    async def _fake_validate(_entry, _key):
        return (True, "validated")

    with patch.object(providers_mod, "_build_catalog", _fake_build), \
         patch.object(providers_mod, "_validate_api_key", _fake_validate):
        resp = client.post(
            "/providers/some-oauth/credential",
            json={"api_key": "anything"},
        )

    assert resp.status_code == 400
    assert "OAuth" in resp.json()["detail"]


# ── DELETE /providers/{id}/credential/{entry_id} ────────────────────────────


def test_delete_credential_removes_entry(client, providers_mod):
    """DELETE removes the matching pool entry and returns ok."""
    cred = MagicMock()
    cred.id = "myah-abc123"

    pool_with_entry = MagicMock()
    pool_with_entry.entries = MagicMock(return_value=[cred])
    pool_with_entry.remove_index = MagicMock()

    pool_after = MagicMock()
    pool_after.entries = MagicMock(return_value=[cred])  # still has entries

    pools = [pool_with_entry, pool_after]

    def _fake_load_pool(_provider):
        return pools.pop(0)

    with patch("agent.credential_pool.load_pool", _fake_load_pool):
        resp = client.delete("/providers/alpha/credential/myah-abc123")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    pool_with_entry.remove_index.assert_called_once_with(1)


def test_delete_credential_clears_env_when_pool_empty(client, providers_mod):
    """When the pool empties, env var is cleared via remove_env_value."""
    cred = MagicMock()
    cred.id = "myah-xyz"

    pool_before = MagicMock()
    pool_before.entries = MagicMock(return_value=[cred])
    pool_before.remove_index = MagicMock()

    pool_empty = MagicMock()
    pool_empty.entries = MagicMock(return_value=[])

    pools = [pool_before, pool_empty]

    def _fake_load_pool(_provider):
        return pools.pop(0)

    async def _fake_build():
        return {"alpha": {"env_var": "ALPHA_KEY"}}

    removed = []

    def _fake_remove_env(key):
        removed.append(key)
        return True

    with patch("agent.credential_pool.load_pool", _fake_load_pool), \
         patch.object(providers_mod, "_build_catalog", _fake_build), \
         patch("hermes_cli.config.remove_env_value", _fake_remove_env):
        resp = client.delete("/providers/alpha/credential/myah-xyz")

    assert resp.status_code == 200
    assert removed == ["ALPHA_KEY"]


def test_delete_credential_not_found_returns_404(client):
    pool = MagicMock()
    pool.entries = MagicMock(return_value=[])
    with patch("agent.credential_pool.load_pool", return_value=pool):
        resp = client.delete("/providers/alpha/credential/nonexistent")
    assert resp.status_code == 404


# ── DELETE /providers/{id} ──────────────────────────────────────────────────


def test_delete_all_credentials(client, providers_mod):
    """DELETE /providers/{id} clears auth.json + env var for the provider."""
    clear_calls = []
    remove_calls = []

    def _fake_clear_auth(provider_id):
        clear_calls.append(provider_id)
        return True

    def _fake_remove_env(key):
        remove_calls.append(key)
        return True

    async def _fake_build():
        return {"alpha": {"env_var": "ALPHA_KEY"}}

    with patch("hermes_cli.auth.clear_provider_auth", _fake_clear_auth), \
         patch("hermes_cli.config.remove_env_value", _fake_remove_env), \
         patch.object(providers_mod, "_build_catalog", _fake_build):
        resp = client.delete("/providers/alpha")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert clear_calls == ["alpha"]
    assert remove_calls == ["ALPHA_KEY"]


def test_delete_all_credentials_swallows_clear_auth_error(client, providers_mod):
    """clear_provider_auth raising must not propagate (legacy parity)."""
    def _broken(_):
        raise RuntimeError("auth.json corrupt")

    async def _fake_build():
        return {"alpha": {"env_var": "ALPHA_KEY"}}

    with patch("hermes_cli.auth.clear_provider_auth", _broken), \
         patch("hermes_cli.config.remove_env_value", lambda *_a: True), \
         patch.object(providers_mod, "_build_catalog", _fake_build):
        resp = client.delete("/providers/alpha")

    assert resp.status_code == 200


# ── _validate_api_key (httpx port) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_api_key_no_url_accepts(providers_mod):
    """No validation URL → optimistic accept with explicit reason."""
    accepted, reason = await providers_mod._validate_api_key({}, "any-key")
    assert accepted is True
    assert "no validation URL" in reason


@pytest.mark.asyncio
async def test_validate_api_key_2xx_accepts(providers_mod):
    """200 response accepts with reason='validated'."""
    entry = {"validation": {"url": "https://x", "method": "GET", "auth": "bearer"}}

    fake_resp = MagicMock()
    fake_resp.status_code = 200

    async def _fake_request(_self, *_a, **_k):
        return fake_resp

    with patch("httpx.AsyncClient.request", _fake_request):
        accepted, reason = await providers_mod._validate_api_key(entry, "good-key")

    assert accepted is True
    assert reason == "validated"


@pytest.mark.asyncio
async def test_validate_api_key_401_rejects(providers_mod):
    entry = {"validation": {"url": "https://x", "method": "GET", "auth": "bearer"}}

    fake_resp = MagicMock()
    fake_resp.status_code = 401

    async def _fake_request(_self, *_a, **_k):
        return fake_resp

    with patch("httpx.AsyncClient.request", _fake_request):
        accepted, reason = await providers_mod._validate_api_key(entry, "bad-key")

    assert accepted is False
    assert "auth denied" in reason


@pytest.mark.asyncio
async def test_validate_api_key_403_rejects(providers_mod):
    entry = {"validation": {"url": "https://x", "method": "GET", "auth": "bearer"}}

    fake_resp = MagicMock()
    fake_resp.status_code = 403

    async def _fake_request(_self, *_a, **_k):
        return fake_resp

    with patch("httpx.AsyncClient.request", _fake_request):
        accepted, reason = await providers_mod._validate_api_key(entry, "bad-key")

    assert accepted is False
    assert "auth denied" in reason


@pytest.mark.asyncio
async def test_validate_api_key_429_optimistic(providers_mod):
    """Rate-limit accepts optimistically — not an auth failure."""
    entry = {"validation": {"url": "https://x", "method": "GET", "auth": "bearer"}}

    fake_resp = MagicMock()
    fake_resp.status_code = 429

    async def _fake_request(_self, *_a, **_k):
        return fake_resp

    with patch("httpx.AsyncClient.request", _fake_request):
        accepted, reason = await providers_mod._validate_api_key(entry, "key")

    assert accepted is True
    assert "optimistic" in reason


@pytest.mark.asyncio
async def test_validate_api_key_5xx_optimistic(providers_mod):
    entry = {"validation": {"url": "https://x", "method": "GET", "auth": "bearer"}}

    fake_resp = MagicMock()
    fake_resp.status_code = 503

    async def _fake_request(_self, *_a, **_k):
        return fake_resp

    with patch("httpx.AsyncClient.request", _fake_request):
        accepted, reason = await providers_mod._validate_api_key(entry, "key")

    assert accepted is True
    assert "optimistic" in reason


@pytest.mark.asyncio
async def test_validate_api_key_timeout_optimistic(providers_mod):
    """Network timeout accepts optimistically."""
    import httpx

    entry = {"validation": {"url": "https://x", "method": "GET", "auth": "bearer"}}

    async def _raise_timeout(_self, *_a, **_k):
        raise httpx.TimeoutException("slow")

    with patch("httpx.AsyncClient.request", _raise_timeout):
        accepted, reason = await providers_mod._validate_api_key(entry, "key")

    assert accepted is True
    assert "timeout" in reason


@pytest.mark.asyncio
async def test_validate_api_key_x_api_key_auth_style(providers_mod):
    """auth=x-api-key sets the x-api-key header + anthropic-version."""
    entry = {
        "validation": {"url": "https://x", "method": "GET", "auth": "x-api-key"}
    }

    captured: dict = {}

    async def _fake_request(_self, method, url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        captured["params"] = kwargs.get("params")
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("httpx.AsyncClient.request", _fake_request):
        await providers_mod._validate_api_key(entry, "secret")

    assert captured["headers"]["x-api-key"] == "secret"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_validate_api_key_query_auth_style(providers_mod):
    """auth=query passes the key as ?key=... query param."""
    entry = {
        "validation": {"url": "https://x", "method": "GET", "auth": "query"}
    }

    captured: dict = {}

    async def _fake_request(_self, method, url, **kwargs):
        captured["params"] = kwargs.get("params")
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("httpx.AsyncClient.request", _fake_request):
        await providers_mod._validate_api_key(entry, "secret")

    assert captured["params"] == {"key": "secret"}
