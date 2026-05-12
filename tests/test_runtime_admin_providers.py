"""Tests for the runtime_admin /myah/v1/admin/providers endpoint.

This endpoint returns the merged provider catalog with a
``has_credential`` boolean per provider, computed from env vars and
auth.json. Replaces the dashboard's ``/api/plugins/myah-admin/providers``
endpoint for callers that have the standard
``MYAH_ADAPTER_AUTH_KEY``-bearer auth but not the dashboard's separate
``HERMES_WEB_SESSION_TOKEN``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from myah_hermes_plugin.myah_platform.runtime_admin import _make_handlers


@pytest.fixture
def fake_runner():
    return MagicMock(name="GatewayRunner")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_catalog():
    """Stub _build_catalog to return a known shape so we test the
    enrichment logic in isolation."""
    catalog = {
        "openrouter": {
            "id": "openrouter",
            "display_name": "OpenRouter",
            "description": "OpenRouter routes 200+ models",
            "auth_type": "api_key",
            "env_var": "OPENROUTER_API_KEY",
            "inference_base_url": "https://openrouter.ai/api/v1",
            "curated_models": [{"id": "moonshotai/kimi-k2", "name": "Kimi K2"}],
            "v1_visible": True,
        },
        "anthropic": {
            "id": "anthropic",
            "display_name": "Anthropic",
            "description": "Claude models",
            "auth_type": "api_key",
            "env_var": "ANTHROPIC_API_KEY",
            "inference_base_url": "https://api.anthropic.com",
            "curated_models": [],
            "v1_visible": True,
        },
        "nous": {
            "id": "nous",
            "display_name": "Nous Portal",
            "description": "Nous OAuth",
            "auth_type": "oauth_device_code",
            "env_var": None,
            "inference_base_url": "https://inference.nousresearch.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
    }

    async def _stub():
        return catalog

    return _stub


@pytest.mark.asyncio
async def test_providers_lists_env_var_credentialed(
    hermes_home, fake_runner, fake_catalog, monkeypatch
):
    """When an api_key provider's env_var is set, has_credential=True."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=fake_catalog,
    ):
        handlers = _make_handlers(fake_runner, auth_key="")
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)

    assert resp.status == 200
    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["openrouter"]["has_credential"] is True
    assert providers["anthropic"]["has_credential"] is False


@pytest.mark.asyncio
async def test_providers_lists_oauth_credentialed_via_auth_json(
    hermes_home, fake_runner, fake_catalog, monkeypatch
):
    """When an OAuth provider has an entry in auth.json['providers'],
    has_credential=True. Tests the legacy OAuth-token-in-providers path.
    """
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 2,
        "providers": {
            "nous": {"refresh_token": "abc", "access_token": "def"},
        },
        "credential_pool": {},
    }))

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=fake_catalog,
    ):
        handlers = _make_handlers(fake_runner, auth_key="")
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["nous"]["has_credential"] is True


@pytest.mark.asyncio
async def test_providers_lists_credentialed_via_credential_pool(
    hermes_home, fake_runner, fake_catalog, monkeypatch
):
    """When a provider appears in auth.json['credential_pool'], it's marked
    credentialed regardless of auth_type. This is the canonical 'user has
    this provider configured' signal that covers both API-key and OAuth
    providers added via ``hermes auth`` or the setup wizard.
    """
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 2,
        "providers": {},
        "credential_pool": {
            "openrouter": {"keys": [{"key": "sk-or-v1-xxx"}]},
            "anthropic": {"keys": [{"key": "sk-ant-xxx"}]},
            "nous": {"oauth_session_id": "abc"},
        },
    }))
    # Even without env vars set, credential_pool should win.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=fake_catalog,
    ):
        handlers = _make_handlers(fake_runner, auth_key="")
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["openrouter"]["has_credential"] is True
    assert providers["anthropic"]["has_credential"] is True
    assert providers["nous"]["has_credential"] is True


@pytest.mark.asyncio
async def test_providers_lists_empty_when_no_creds(
    hermes_home, fake_runner, fake_catalog, monkeypatch
):
    """No env vars, no auth.json → all providers report has_credential=False."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=fake_catalog,
    ):
        handlers = _make_handlers(fake_runner, auth_key="")
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    for p in body["providers"]:
        assert p["has_credential"] is False, f'{p["id"]} should not be credentialed'


@pytest.mark.asyncio
async def test_providers_returns_required_fields(
    hermes_home, fake_runner, fake_catalog, monkeypatch
):
    """Response shape: each provider has id, label, has_credential, models."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=fake_catalog,
    ):
        handlers = _make_handlers(fake_runner, auth_key="")
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    for p in body["providers"]:
        assert "id" in p
        assert "label" in p  # alias for compat with the platform helper's expected shape
        assert "has_credential" in p
        assert "models" in p


@pytest.mark.asyncio
async def test_providers_handles_catalog_failure_gracefully(
    hermes_home, fake_runner
):
    """If _build_catalog raises, return empty list with 200 (callers degrade)."""

    async def _explode():
        raise RuntimeError("boom")

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_explode,
    ):
        handlers = _make_handlers(fake_runner, auth_key="")
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)

    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body == {"providers": []}


@pytest.mark.asyncio
async def test_providers_requires_auth_when_key_set(
    hermes_home, fake_runner, fake_catalog
):
    """Auth required when adapter has a key set."""
    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=fake_catalog,
    ):
        handlers = _make_handlers(fake_runner, auth_key="secret-key")
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)
        assert resp.status == 401

        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers",
            headers={"Authorization": "Bearer secret-key"},
        )
        resp = await handlers["get_provider_catalog"](req)
        assert resp.status == 200
