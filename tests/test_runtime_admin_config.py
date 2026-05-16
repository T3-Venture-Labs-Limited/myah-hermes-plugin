"""Tests for the runtime_admin /myah/v1/admin/config endpoint.

This endpoint returns the hermes ``config.yaml`` model block so the
platform can discover the user's configured default provider/model
without going through the dashboard's separate auth (which OSS users
typically don't configure).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml
from aiohttp.test_utils import make_mocked_request

from myah_hermes_plugin.myah_platform.runtime_admin import _make_handlers

# Shared real auth_key + Bearer header — the runtime admin handlers now
# fail closed when auth_key is empty, so business-logic tests must use
# a real key. A dedicated test covers the fail-closed path.
_TEST_AUTH_KEY = "runtime-admin-config-tests-bearer"
_AUTHED_HEADERS = {"Authorization": f"Bearer {_TEST_AUTH_KEY}"}


@pytest.fixture
def fake_runner():
    return MagicMock(name="GatewayRunner")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a tmp dir for this test only."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Force hermes_constants to re-read on the next get_hermes_home() call
    # (the module caches, but the handler imports lazily so a monkeypatched
    # env var works as long as get_hermes_home() reads it each time).
    return tmp_path


@pytest.mark.asyncio
async def test_config_returns_model_block(hermes_home, fake_runner):
    cfg = {
        "model": {
            "provider": "opencode-go",
            "default": "mimo-v2.5",
            "base_url": "https://opencode.ai/zen/go/v1",
            "api_mode": "chat_completions",
        },
        # Non-model fields must NOT leak — only the model block is returned.
        "honcho": {"api_key": "should-not-appear"},
        "providers": {"openrouter": {"api_key": "secret"}},
    }
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg))

    handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
    req = make_mocked_request("GET", "/myah/v1/admin/config", headers=_AUTHED_HEADERS)
    resp = await handlers["get_hermes_config"](req)

    assert resp.status == 200
    import json
    body = json.loads(resp.body.decode())
    assert body["model"] == {
        "provider": "opencode-go",
        "default": "mimo-v2.5",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_mode": "chat_completions",
    }
    # Secrets must NOT appear in the response under any key
    body_str = resp.body.decode()
    assert "should-not-appear" not in body_str
    assert "secret" not in body_str


@pytest.mark.asyncio
async def test_config_missing_file_returns_empty(hermes_home, fake_runner):
    """No config.yaml → return {model: {}} with status 200, don't error."""
    handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
    req = make_mocked_request("GET", "/myah/v1/admin/config", headers=_AUTHED_HEADERS)
    resp = await handlers["get_hermes_config"](req)

    assert resp.status == 200
    import json
    body = json.loads(resp.body.decode())
    assert body == {"model": {}}


@pytest.mark.asyncio
async def test_config_missing_model_block_returns_empty(hermes_home, fake_runner):
    """config.yaml exists but no model section → {model: {}}."""
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({"agent": {"max_turns": 100}}))
    handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
    req = make_mocked_request("GET", "/myah/v1/admin/config", headers=_AUTHED_HEADERS)
    resp = await handlers["get_hermes_config"](req)

    assert resp.status == 200
    import json
    body = json.loads(resp.body.decode())
    assert body == {"model": {}}


@pytest.mark.asyncio
async def test_config_invalid_yaml_returns_empty(hermes_home, fake_runner):
    """Malformed YAML → degrade gracefully, return empty."""
    (hermes_home / "config.yaml").write_text("model:\n  default: [unclosed list\n")
    handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
    req = make_mocked_request("GET", "/myah/v1/admin/config", headers=_AUTHED_HEADERS)
    resp = await handlers["get_hermes_config"](req)

    assert resp.status == 200
    import json
    body = json.loads(resp.body.decode())
    assert body == {"model": {}}


@pytest.mark.asyncio
async def test_config_partial_model_block(hermes_home, fake_runner):
    """Only provider+default set; base_url and api_mode missing → empty strings.

    The platform's resolver decides what to do with empty fields. Plugin
    just normalizes to strings so the contract is stable.
    """
    cfg = {"model": {"provider": "openrouter", "default": "moonshotai/kimi-k2.6"}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg))

    handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
    req = make_mocked_request("GET", "/myah/v1/admin/config", headers=_AUTHED_HEADERS)
    resp = await handlers["get_hermes_config"](req)

    import json
    body = json.loads(resp.body.decode())
    assert body["model"]["provider"] == "openrouter"
    assert body["model"]["default"] == "moonshotai/kimi-k2.6"
    assert body["model"]["base_url"] == ""
    assert body["model"]["api_mode"] == ""


@pytest.mark.asyncio
async def test_config_requires_auth_when_key_set(hermes_home, fake_runner):
    """If the adapter has an auth_key, the endpoint MUST require it."""
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "x"}}))
    handlers = _make_handlers(fake_runner, auth_key="secret-key-abc")

    # No Authorization header → 401
    req = make_mocked_request("GET", "/myah/v1/admin/config", headers=_AUTHED_HEADERS)
    resp = await handlers["get_hermes_config"](req)
    assert resp.status == 401

    # Wrong token → 401
    req = make_mocked_request(
        "GET", "/myah/v1/admin/config",
        headers={"Authorization": "Bearer wrong-token"},
    )
    resp = await handlers["get_hermes_config"](req)
    assert resp.status == 401

    # Correct token → 200
    req = make_mocked_request(
        "GET", "/myah/v1/admin/config",
        headers={"Authorization": "Bearer secret-key-abc"},
    )
    resp = await handlers["get_hermes_config"](req)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_config_fails_closed_when_auth_key_empty(hermes_home, fake_runner):
    """OSS install with MYAH_ADAPTER_AUTH_KEY unset must NOT serve config.

    Previously this test asserted the opposite — auth_key='' meant 'accept
    all requests', which combined with the parallel adapter bug to leave
    every /myah/v1/admin/* endpoint reachable without credentials. The
    fix-closed behaviour is verified here.
    """
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "x"}}))
    handlers = _make_handlers(fake_runner, auth_key="")

    req = make_mocked_request("GET", "/myah/v1/admin/config")
    resp = await handlers["get_hermes_config"](req)
    assert resp.status == 503
    import json as _json
    body = _json.loads(resp.body.decode())
    assert "MYAH_ADAPTER_AUTH_KEY" in body.get("detail", "")
