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

# Shared across every test in this file. Real auth_key + matching Bearer
# header so each business-logic test exercises a real authed request path.
# A separate test class below covers the empty-auth-key fail-closed path.
_TEST_AUTH_KEY = "runtime-admin-provider-tests-bearer"
_AUTHED_HEADERS = {"Authorization": f"Bearer {_TEST_AUTH_KEY}"}


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
    """When an api_key provider's env_var is set in ~/.hermes/.env, has_credential=True."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (hermes_home / ".env").write_text("OPENROUTER_API_KEY=sk-or-v1-test\n")

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=fake_catalog,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request("GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS)
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
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request("GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS)
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
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request("GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS)
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
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request("GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS)
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
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request("GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS)
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
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request("GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS)
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
        req = make_mocked_request("GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS)
        resp = await handlers["get_provider_catalog"](req)
        assert resp.status == 401

        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers",
            headers={"Authorization": "Bearer secret-key"},
        )
        resp = await handlers["get_provider_catalog"](req)
        assert resp.status == 200


# ── Fail-closed when MYAH_ADAPTER_AUTH_KEY is unset ────────────────────────


class TestRuntimeAdminAuthFailsClosed:
    """The runtime admin endpoints (/myah/v1/admin/*) are the privileged
    surface of the plugin. Before this fix, passing ``auth_key=""``
    (which is what happens when MYAH_ADAPTER_AUTH_KEY is unset in
    ~/.hermes/.env) made every handler bypass auth — anyone on
    MYAH_GATEWAY_PORT could read providers, write secrets, swap models,
    and manage sessions without credentials.

    These tests pin the new fail-closed behaviour: handlers must refuse
    requests with 503 + an actionable error pointing at
    ``scripts/setup-myah-oss.sh``.
    """

    @pytest.mark.asyncio
    async def test_get_provider_catalog_refuses_when_auth_key_empty(
        self, fake_runner
    ):
        handlers = _make_handlers(fake_runner, auth_key="")
        req = make_mocked_request(
            "GET",
            "/myah/v1/admin/providers",
            headers={"Authorization": "Bearer anything"},
        )
        resp = await handlers["get_provider_catalog"](req)
        assert resp.status == 503
        body = json.loads(resp.body.decode())
        assert "MYAH_ADAPTER_AUTH_KEY" in body.get("detail", "")
        assert "setup-myah-oss.sh" in body.get("detail", "")

    @pytest.mark.asyncio
    async def test_get_provider_catalog_refuses_when_auth_key_none(
        self, fake_runner
    ):
        handlers = _make_handlers(fake_runner, auth_key=None)
        req = make_mocked_request("GET", "/myah/v1/admin/providers")
        resp = await handlers["get_provider_catalog"](req)
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_other_admin_handlers_also_fail_closed(self, fake_runner):
        """Spot-check that the fail-closed behaviour applies to every
        runtime-admin handler, not just the providers one."""
        handlers = _make_handlers(fake_runner, auth_key="")
        for name in (
            "get_session_override",
            "post_session_override",
            "get_config",
            "post_config",
            "get_active_provider",
            "post_active_provider",
        ):
            handler = handlers.get(name)
            if handler is None:
                continue  # handler may be renamed; tolerate
            req = make_mocked_request("GET", f"/myah/v1/admin/{name}")
            resp = await handler(req)
            assert resp.status == 503, (
                f"handler {name!r} did not fail closed with empty auth_key "
                f"(got status {resp.status})"
            )


# ── T3-1043: env-var-backed provider disconnect fix ──────────────────────────
#
# Bug: line 346 of runtime_admin.py reads `_os.environ.get(env_var)` which
# returns the GATEWAY PROCESS's environment — stale after the user calls
# POST /myah/v1/admin/remove_provider (which writes the .env file via a
# subprocess, not the live process).  After a successful disconnect the
# gateway still sees the key in its own os.environ, so `has_credential`
# stays True and the UI shows the provider as still-connected.
#
# Fix: swap `_os.environ.get(env_var)` → `_load_env_file().get(env_var)`,
# where `_load_env_file` is `hermes_cli.config.load_env` — an mtime-cached
# file read that is always cross-process accurate.
#
# Each test uses a unique, synthetic env-var name (T3_1043_T*_KEY) to
# guarantee no real provider variable bleeds in from the shell environment.


@pytest.fixture
def fake_catalog_t3_1043():
    """Catalog dict with openrouter/anthropic/nous plus the T3-1043 synthetic
    provider — uses a unique env-var name immune to ambient shell env."""
    return {
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
        "t3_1043_provider": {
            "id": "t3_1043_provider",
            "display_name": "T3-1043 Test Provider",
            "description": "Synthetic provider for T3-1043 regression tests",
            "auth_type": "api_key",
            "env_var": "T3_1043_T1_KEY",
            "inference_base_url": "https://test.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
    }


# ── T-1 (RED before fix): env var set in os.environ but ABSENT from .env ─────


@pytest.mark.asyncio
async def test_t3_1043_env_in_os_environ_but_not_dotenv_returns_false(
    hermes_home, fake_runner, fake_catalog_t3_1043, monkeypatch
):
    """T-1 (RED before fix): when T3_1043_T1_KEY is in the PROCESS environment
    but NOT written to ~/.hermes/.env, has_credential must be False.

    This is the exact bug scenario: the key was removed from .env by
    POST /myah/v1/admin/remove_provider but the gateway process's own
    os.environ still carries it from startup.  The old code reads
    os.environ and sees True; the fixed code reads .env and sees False.
    """
    # Inject the key into os.environ (simulates gateway startup env).
    monkeypatch.setenv("T3_1043_T1_KEY", "fake-api-key-t1")
    # .env file is EMPTY — key was removed from disk (simulate post-disconnect).
    dotenv_path = hermes_home / ".env"
    dotenv_path.write_text("")  # empty .env

    async def _fake_catalog_fn():
        return fake_catalog_t3_1043

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_catalog_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    # After fix: reads .env (empty) → False.
    # Before fix: reads os.environ (key present) → True.  This assertion is RED.
    assert providers["t3_1043_provider"]["has_credential"] is False, (
        "has_credential should be False when key is in os.environ "
        "but absent from ~/.hermes/.env (simulates post-disconnect state)"
    )


# ── T-2 (regression guard): env var in BOTH os.environ AND .env → True ───────


@pytest.mark.asyncio
async def test_t3_1043_env_in_both_os_environ_and_dotenv_returns_true(
    hermes_home, fake_runner, fake_catalog_t3_1043, monkeypatch
):
    """T-2 (regression guard): key in both os.environ and .env → True.

    Normal connected state.  Fix must not break this path.
    """
    monkeypatch.setenv("T3_1043_T2_KEY", "fake-api-key-t2")
    dotenv_path = hermes_home / ".env"
    dotenv_path.write_text("T3_1043_T2_KEY=fake-api-key-t2\n")

    # Build a catalog that uses T3_1043_T2_KEY
    import copy
    cat = copy.deepcopy(fake_catalog_t3_1043)
    cat["t3_1043_t2_provider"] = {
        "id": "t3_1043_t2_provider",
        "display_name": "T3-1043 T2 Provider",
        "description": "",
        "auth_type": "api_key",
        "env_var": "T3_1043_T2_KEY",
        "inference_base_url": "https://test2.example.com/v1",
        "curated_models": [],
        "v1_visible": True,
    }

    async def _fake_fn():
        return cat

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["t3_1043_t2_provider"]["has_credential"] is True


# ── T-3 (regression guard): env var in .env ONLY (not os.environ) → True ────


@pytest.mark.asyncio
async def test_t3_1043_env_in_dotenv_only_not_os_environ_returns_true(
    hermes_home, fake_runner, fake_catalog_t3_1043, monkeypatch
):
    """T-3 (regression guard): key in .env but NOT in os.environ → True.

    This is the clean-install path where the user added the key via
    `hermes auth` (writes .env) but the gateway wasn't restarted.
    """
    monkeypatch.delenv("T3_1043_T3_KEY", raising=False)
    dotenv_path = hermes_home / ".env"
    dotenv_path.write_text("T3_1043_T3_KEY=fake-api-key-t3\n")

    import copy
    cat = copy.deepcopy(fake_catalog_t3_1043)
    cat["t3_1043_t3_provider"] = {
        "id": "t3_1043_t3_provider",
        "display_name": "T3-1043 T3 Provider",
        "description": "",
        "auth_type": "api_key",
        "env_var": "T3_1043_T3_KEY",
        "inference_base_url": "https://test3.example.com/v1",
        "curated_models": [],
        "v1_visible": True,
    }

    async def _fake_fn():
        return cat

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["t3_1043_t3_provider"]["has_credential"] is True


# ── T-4 (RED before fix): cross-process integration via subprocess ────────────


@pytest.mark.asyncio
async def test_t3_1043_cross_process_remove_then_check_returns_false(
    hermes_home, fake_runner, monkeypatch
):
    """T-4 (RED before fix): subprocess removes key from .env; handler reads
    .env → False.  Mirrors the actual production disconnect flow.

    Before fix: os.environ still has the key (monkeypatched in) → True.
    After fix: .env is read fresh → False.
    """
    import subprocess
    import sys

    env_var = "T3_1043_T4_KEY"
    fake_value = "fake-api-key-t4"
    dotenv_path = hermes_home / ".env"

    # Write key to .env (simulates connected state)
    dotenv_path.write_text(f"{env_var}={fake_value}\n")
    # Also inject into this process's os.environ (simulates gateway startup)
    monkeypatch.setenv(env_var, fake_value)

    # Subprocess removes the key from .env, simulating remove_env_value running
    # in a separate process without touching the gateway's os.environ.
    remove_script = f"""
import pathlib, re
p = pathlib.Path({str(dotenv_path)!r})
content = p.read_text()
content = re.sub(r'^{env_var}=.*\\n?', '', content, flags=re.MULTILINE)
p.write_text(content)
"""
    subprocess.run([sys.executable, "-c", remove_script], check=True)

    cat = {
        "t3_1043_t4_provider": {
            "id": "t3_1043_t4_provider",
            "display_name": "T3-1043 T4 Provider",
            "description": "",
            "auth_type": "api_key",
            "env_var": env_var,
            "inference_base_url": "https://test4.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        }
    }

    async def _fake_fn():
        return cat

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["t3_1043_t4_provider"]["has_credential"] is False, (
        "has_credential should be False after key removed from .env "
        "even though key remains in os.environ"
    )


# ── T-5 (regression guard): credential_pool takes priority over env check ────


@pytest.mark.asyncio
async def test_t3_1043_credential_pool_takes_priority(
    hermes_home, fake_runner, fake_catalog_t3_1043, monkeypatch
):
    """T-5 (regression guard): credential_pool present → True regardless of
    env var state.  Fix must not disturb the credential_pool short-circuit.
    """
    monkeypatch.delenv("T3_1043_T1_KEY", raising=False)
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 2,
        "providers": {},
        "credential_pool": {"t3_1043_provider": {"keys": [{"key": "pool-key"}]}},
    }))
    dotenv_path = hermes_home / ".env"
    dotenv_path.write_text("")  # key absent from .env

    async def _fake_fn():
        return fake_catalog_t3_1043

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["t3_1043_provider"]["has_credential"] is True


# ── T-6 (regression guard): OAuth path unaffected by env-var fix ─────────────


@pytest.mark.asyncio
async def test_t3_1043_oauth_path_unaffected(
    hermes_home, fake_runner, fake_catalog_t3_1043, monkeypatch
):
    """T-6 (regression guard): OAuth providers use auth_json providers dict,
    not env vars.  Fix must not change their has_credential logic.
    """
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 2,
        "providers": {"nous": {"refresh_token": "rt", "access_token": "at"}},
        "credential_pool": {},
    }))

    async def _fake_fn():
        return fake_catalog_t3_1043

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["nous"]["has_credential"] is True


# ── T-7 (regression guard): null/empty/missing env_var field → False ─────────


@pytest.mark.asyncio
async def test_t3_1043_null_empty_missing_env_var_returns_false(
    hermes_home, fake_runner, monkeypatch
):
    """T-7 (regression guard): providers where env_var is None, '', or absent
    in the catalog entry should never be marked credentialed via the env path.
    """
    import copy

    cat = {
        "no_env_var_null": {
            "id": "no_env_var_null",
            "display_name": "No Env Var (null)",
            "description": "",
            "auth_type": "api_key",
            "env_var": None,
            "inference_base_url": "https://null.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
        "no_env_var_empty": {
            "id": "no_env_var_empty",
            "display_name": "No Env Var (empty string)",
            "description": "",
            "auth_type": "api_key",
            "env_var": "",
            "inference_base_url": "https://empty.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
        "no_env_var_absent": {
            "id": "no_env_var_absent",
            "display_name": "No Env Var (key absent)",
            "description": "",
            "auth_type": "api_key",
            "inference_base_url": "https://absent.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
    }
    dotenv_path = hermes_home / ".env"
    dotenv_path.write_text("")

    async def _fake_fn():
        return cat

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    for p in body["providers"]:
        assert p["has_credential"] is False, (
            f'{p["id"]} should not be credentialed without an env_var'
        )


# ── T-8 (regression guard): missing .env file → False (not an exception) ─────


@pytest.mark.asyncio
async def test_t3_1043_missing_dotenv_file_returns_false_not_exception(
    hermes_home, fake_runner, fake_catalog_t3_1043, monkeypatch
):
    """T-8 (regression guard): when ~/.hermes/.env doesn't exist at all,
    has_credential must be False — not a 500.  Fresh-install / env-less
    deployment path.
    """
    monkeypatch.delenv("T3_1043_T1_KEY", raising=False)
    # Do NOT create .env — hermes_home fixture only sets HERMES_HOME
    dotenv_path = hermes_home / ".env"
    assert not dotenv_path.exists(), "test setup: .env should not exist"

    async def _fake_fn():
        return fake_catalog_t3_1043

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    assert resp.status == 200
    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["t3_1043_provider"]["has_credential"] is False


# ── T-9 (regression guard): multiple providers — only disconnect target → False


@pytest.mark.asyncio
async def test_t3_1043_multi_provider_non_interference(
    hermes_home, fake_runner, monkeypatch
):
    """T-9 (regression guard): disconnecting one provider must not change
    has_credential for other providers.  Uses three unique T3-1043 env-var
    names to prevent ambient shell interference.
    """
    # T9_PROVIDER_A: in .env (connected)
    # T9_PROVIDER_B: was in .env, now removed (disconnected)
    # T9_PROVIDER_C: never configured (not connected)

    env_a = "T3_1043_T9_KEY_A"
    env_b = "T3_1043_T9_KEY_B"
    env_c = "T3_1043_T9_KEY_C"

    # T9_PROVIDER_A: in both os.environ and .env (connected — must remain True after fix)
    # T9_PROVIDER_B: in os.environ only (stale gateway env, removed from .env — must be False after fix)
    # T9_PROVIDER_C: in neither (never configured — must be False before and after fix)
    monkeypatch.setenv(env_a, "real-key-a")
    monkeypatch.setenv(env_b, "still-in-process-env")
    monkeypatch.delenv(env_c, raising=False)

    dotenv_path = hermes_home / ".env"
    dotenv_path.write_text(f"{env_a}=real-key-a\n")  # only A is in .env

    cat = {
        "t9_a": {
            "id": "t9_a",
            "display_name": "T9 Provider A",
            "description": "",
            "auth_type": "api_key",
            "env_var": env_a,
            "inference_base_url": "https://t9a.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
        "t9_b": {
            "id": "t9_b",
            "display_name": "T9 Provider B",
            "description": "",
            "auth_type": "api_key",
            "env_var": env_b,
            "inference_base_url": "https://t9b.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
        "t9_c": {
            "id": "t9_c",
            "display_name": "T9 Provider C",
            "description": "",
            "auth_type": "api_key",
            "env_var": env_c,
            "inference_base_url": "https://t9c.example.com/v1",
            "curated_models": [],
            "v1_visible": True,
        },
    }

    async def _fake_fn():
        return cat

    with patch(
        "myah_hermes_plugin.myah_admin.dashboard._providers._build_catalog",
        new=_fake_fn,
    ):
        handlers = _make_handlers(fake_runner, auth_key=_TEST_AUTH_KEY)
        req = make_mocked_request(
            "GET", "/myah/v1/admin/providers", headers=_AUTHED_HEADERS
        )
        resp = await handlers["get_provider_catalog"](req)

    body = json.loads(resp.body.decode())
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["t9_a"]["has_credential"] is True, "A should still be connected"
    assert providers["t9_b"]["has_credential"] is False, "B should be disconnected"
    assert providers["t9_c"]["has_credential"] is False, "C was never connected"
