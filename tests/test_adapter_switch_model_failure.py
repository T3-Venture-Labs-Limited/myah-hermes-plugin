"""Regression tests for switch_model failure handling in MyahAdapter.

Bug: when ``switch_model`` returns failure inside ``_handle_message_endpoint``,
the previous turn's session override is never cleared. Subsequent chat turns
in the same session reuse the stale model id, leading to "wrong model"
errors that only resolve by switching models away and back.

Fix: on failure, clear the override (so we fall back to agent defaults),
clean up any stream state created so far, and surface a 400 to the platform
with a clear error so the frontend can display it.
"""

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request


class _FakeRunner:
    """Minimal runner stand-in tracking session-override state."""

    def __init__(self) -> None:
        self._session_model_overrides: dict[str, dict] = {}

    def get_session_override(self, session_key: str) -> dict | None:
        return self._session_model_overrides.get(session_key)

    def set_session_override(self, session_key: str, override: dict) -> None:
        self._session_model_overrides[session_key] = dict(override)


def _make_adapter_with_runner(initial_override: dict | None = None):
    """Construct a MyahAdapter wired to a _FakeRunner."""
    from gateway.config import PlatformConfig
    with patch('gateway.platforms.api_server.register_pre_setup_hook'):
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
        adapter = MyahAdapter(PlatformConfig(enabled=True, extra={'auth_key': ''}))
    runner = _FakeRunner()
    adapter.gateway_runner = runner
    if initial_override is not None:
        # Pre-seed for any session_key — _handle_message_endpoint computes
        # the key itself, so we install the override after we know it.
        adapter._test_initial_override = initial_override  # noqa: SLF001
    return adapter, runner


def _install_fake_model_switch(success: bool, error_message: str = 'boom',
                               new_model: str = 'B', target_provider: str = 'openrouter'):
    """Install a fake hermes_cli.model_switch module for the import inside the endpoint."""
    fake_mod = types.ModuleType('hermes_cli.model_switch')

    def _switch_model(**kwargs):  # noqa: ARG001
        return SimpleNamespace(
            success=success,
            error_message=error_message,
            new_model=new_model,
            target_provider=target_provider,
            api_key='ak',
            base_url='https://x',
            api_mode='chat_completions',
        )

    fake_mod.switch_model = _switch_model

    # Ensure parent package exists in sys.modules so the relative import works.
    hermes_cli_existed = 'hermes_cli' in sys.modules
    sys.modules['hermes_cli.model_switch'] = fake_mod
    return fake_mod, hermes_cli_existed


@pytest.fixture
def fake_switch_failure(monkeypatch):
    """Patch switch_model to return failure."""
    fake_mod, _ = _install_fake_model_switch(success=False, error_message='unsupported model B')
    yield fake_mod
    sys.modules.pop('hermes_cli.model_switch', None)


@pytest.fixture
def fake_switch_success(monkeypatch):
    """Patch switch_model to return success — used to seed an initial override."""
    fake_mod, _ = _install_fake_model_switch(success=True, new_model='A', target_provider='openrouter')
    yield fake_mod
    sys.modules.pop('hermes_cli.model_switch', None)


def _make_message_request(body: dict):
    """Build a mocked POST /myah/v1/message request."""
    request = make_mocked_request('POST', '/myah/v1/message')
    request.json = AsyncMock(return_value=body)
    return request


@pytest.mark.asyncio
async def test_switch_model_failure_clears_override(fake_switch_failure):
    """When switch_model fails, any previously-set override must be cleared."""
    adapter, runner = _make_adapter_with_runner()

    # Compute the session_key the endpoint will derive so we can pre-seed.
    from gateway.session import build_session_key
    source_args = dict(
        chat_id='chat-1', chat_name=None, chat_type='dm',
        user_id='u-1', user_name=None,
    )
    source = adapter.build_source(**source_args)
    session_key = build_session_key(
        source,
        group_sessions_per_user=adapter.config.extra.get('group_sessions_per_user', True),
        thread_sessions_per_user=adapter.config.extra.get('thread_sessions_per_user', False),
    )
    # Seed a stale override from a previous turn.
    runner.set_session_override(session_key, {
        'model': 'A', 'provider': 'openrouter', 'api_key': 'ak',
        'base_url': 'https://x', 'api_mode': 'chat_completions',
    })
    assert runner.get_session_override(session_key) is not None

    request = _make_message_request({
        'message': 'hello',
        'session_id': 'chat-1',
        'user_id': 'u-1',
        'model': 'B',  # triggers switch_model path
    })

    resp = await adapter._handle_message_endpoint(request)

    # Endpoint must return 400 surfacing the failure.
    assert resp.status == 400, f'expected 400, got {resp.status} body={resp.body!r}'
    body = json.loads(resp.body)
    assert 'error' in body
    assert 'unsupported model B' in body['error'] or 'B' in body['error']

    # The stale override MUST have been cleared.
    assert runner.get_session_override(session_key) is None, (
        'session override was not cleared on switch_model failure'
    )

    # Stream state should not leak — no streams should be left open since
    # the failure happened before dispatch.
    assert not adapter._streams
    assert not adapter._chat_id_streams
    assert not adapter._session_streams
    assert not adapter._stream_sessions


@pytest.mark.asyncio
async def test_switch_model_failure_returns_400_with_error_message(fake_switch_failure):
    """The 400 body must contain the contract error message."""
    adapter, _runner = _make_adapter_with_runner()
    request = _make_message_request({
        'message': 'hi',
        'session_id': 'chat-x',
        'user_id': 'u-x',
        'model': 'B',
    })

    resp = await adapter._handle_message_endpoint(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    # Contract: {"error": "Failed to switch to model <name>: <reason>"}
    assert body['error'].startswith('Failed to switch to model B:')
    assert 'unsupported model B' in body['error']


# ── OSS Issue #2 — graceful fallback when model name unresolvable ──────


@pytest.mark.asyncio
async def test_oss_unresolvable_model_falls_back_to_default(monkeypatch):
    """OSS regression: the platform's user.default_model can be a model
    the user's hermes pool doesn't have (e.g. openai/gpt-4o-mini when
    they only have openrouter + opencode-go). In that case, the message
    should still dispatch using the agent's existing default rather
    than 400-failing the whole turn.
    """
    monkeypatch.setenv('MYAH_DEPLOYMENT_MODE', 'oss')
    fake_mod, _ = _install_fake_model_switch(
        success=False,
        error_message="model 'openai/gpt-4o-mini' not in any configured provider's catalog",
    )
    try:
        adapter, runner = _make_adapter_with_runner()

        # Compute session_key the endpoint will derive so we can pre-seed.
        from gateway.session import build_session_key
        source = adapter.build_source(
            chat_id='chat-1', chat_name=None, chat_type='dm',
            user_id='user-1', user_name=None,
        )
        session_key = build_session_key(
            source,
            group_sessions_per_user=adapter.config.extra.get('group_sessions_per_user', True),
            thread_sessions_per_user=adapter.config.extra.get('thread_sessions_per_user', False),
        )
        # Pre-populate a stale override that should be cleared
        runner._session_model_overrides[session_key] = {
            'model': 'previous-default-model',
            'provider': 'openrouter',
        }

        request = _make_message_request({
            'message': 'hi',
            'session_id': 'chat-1',
            'user_id': 'user-1',
            'model': 'openai/gpt-4o-mini',
        })

        # Patch auth + handle_message to short-circuit to dispatch
        with patch.object(adapter, '_check_auth', return_value=None), \
             patch.object(adapter, 'handle_message', new=AsyncMock(return_value=None)):
            resp = await adapter._handle_message_endpoint(request)

        # OSS-mode behavior: 202 (accepted), with a stream_id, AND the
        # stale override should be cleared so the next turn falls back.
        assert resp.status == 202, (
            f'OSS expected graceful 202 dispatch despite unresolvable '
            f'model, got {resp.status}: {resp.body!r}'
        )
        assert session_key not in runner._session_model_overrides
    finally:
        sys.modules.pop('hermes_cli.model_switch', None)
