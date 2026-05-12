"""Tests for the secrets agent tool (session-keyed adaptation)."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from myah_hermes_plugin.myah_tools.secrets_tool import (
    secrets_tool,
    set_secrets_request_callback,
    set_secret_request_session_key,
    reset_secret_request_session_key,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

# A name recognised as secret-like by _is_secret_like_name (ends with _API_KEY).
_SECRET_KEY = 'EXISTING_API_KEY'
_SECRET_KEY_2 = 'NEW_API_KEY'
_SESSION = 'test-session'


@pytest.fixture(autouse=True)
def _write_env_file(tmp_path, monkeypatch):
    """Write a .env containing _SECRET_KEY into the isolated HERMES_HOME.

    The conftest _isolate_hermes_home fixture has already redirected HERMES_HOME
    to tmp_path/hermes_test; we patch it again here to ensure we own the value
    so we can also write the .env file into the right location.
    """
    hermes_home = Path(os.environ['HERMES_HOME'])
    env_file = hermes_home / '.env'
    env_file.write_text(f'{_SECRET_KEY}=test-value-123\n', encoding='utf-8')
    yield
    # Clean up any lingering session callback between tests.
    set_secrets_request_callback(_SESSION, None)


@pytest.fixture
def with_callback():
    """Set the active session key and register a fake capture callback."""
    token = set_secret_request_session_key(_SESSION)
    fake_cb = MagicMock(return_value={
        'success': True,
        'stored_as': _SECRET_KEY_2,
        'validated': False,
        'skipped': False,
        'message': 'Stored.',
    })
    set_secrets_request_callback(_SESSION, fake_cb)
    yield fake_cb
    reset_secret_request_session_key(token)
    set_secrets_request_callback(_SESSION, None)


# ── list ─────────────────────────────────────────────────────────────────────


class TestSecretsList:
    def test_list_returns_secrets_key(self):
        result = json.loads(secrets_tool({'action': 'list'}))
        assert 'secrets' in result

    def test_list_includes_configured_secret(self):
        result = json.loads(secrets_tool({'action': 'list'}))
        assert _SECRET_KEY in result['secrets']

    def test_list_includes_missing_for_skills(self):
        result = json.loads(secrets_tool({'action': 'list'}))
        assert 'missing_for_skills' in result

    def test_list_includes_passthrough_registered(self):
        result = json.loads(secrets_tool({'action': 'list'}))
        assert 'passthrough_registered' in result


# ── check ─────────────────────────────────────────────────────────────────────


class TestSecretsCheck:
    def test_check_configured_key(self):
        result = json.loads(secrets_tool({'action': 'check', 'keys': [_SECRET_KEY]}))
        assert _SECRET_KEY in result.get('configured', [])
        assert _SECRET_KEY not in result.get('missing', [])

    def test_check_missing_key(self):
        result = json.loads(secrets_tool({'action': 'check', 'keys': ['MISSING_API_KEY']}))
        assert 'MISSING_API_KEY' in result.get('missing', [])
        assert 'MISSING_API_KEY' not in result.get('configured', [])

    def test_check_configured_vs_missing(self):
        result = json.loads(secrets_tool({
            'action': 'check',
            'keys': [_SECRET_KEY, 'MISSING_API_KEY'],
        }))
        assert _SECRET_KEY in result.get('configured', [])
        assert 'MISSING_API_KEY' in result.get('missing', [])

    def test_check_non_secret_name_is_rejected(self):
        result = json.loads(secrets_tool({'action': 'check', 'keys': ['PLAIN_VAR']}))
        assert 'PLAIN_VAR' in result.get('rejected', [])

    def test_check_invalid_keys_type_returns_error(self):
        result = json.loads(secrets_tool({'action': 'check', 'keys': 'not-a-list'}))
        assert 'error' in result


# ── request ──────────────────────────────────────────────────────────────────


class TestSecretsRequest:
    def test_request_with_callback_calls_it(self, with_callback):
        secrets_tool({'action': 'request', 'key': _SECRET_KEY_2})
        with_callback.assert_called_once()

    def test_request_with_callback_returns_success(self, with_callback):
        result = json.loads(secrets_tool({
            'action': 'request',
            'key': _SECRET_KEY_2,
            'description': 'A test key',
        }))
        assert result.get('success') is True

    def test_request_with_callback_returns_stored(self, with_callback):
        result = json.loads(secrets_tool({'action': 'request', 'key': _SECRET_KEY_2}))
        assert result.get('stored') is True

    def test_request_callback_receives_key_and_prompt(self, with_callback):
        secrets_tool({
            'action': 'request',
            'key': _SECRET_KEY_2,
            'prompt': 'Enter the key now',
        })
        call_args = with_callback.call_args
        assert call_args[0][0] == _SECRET_KEY_2
        assert call_args[0][1] == 'Enter the key now'

    def test_request_callback_receives_metadata(self, with_callback):
        secrets_tool({
            'action': 'request',
            'key': _SECRET_KEY_2,
            'description': 'desc',
            'instructions': 'find it here',
        })
        call_args = with_callback.call_args
        metadata = call_args[0][2]
        assert metadata.get('description') == 'desc'
        assert metadata.get('instructions') == 'find it here'

    def test_request_without_callback_returns_failure(self):
        # No session key set — no callback registered.
        result = json.loads(secrets_tool({'action': 'request', 'key': _SECRET_KEY_2}))
        assert result.get('success') is False

    def test_request_non_secret_name_returns_error(self, with_callback):
        result = json.loads(secrets_tool({'action': 'request', 'key': 'PLAIN_NONSECRET'}))
        assert 'error' in result
        assert result.get('success') is not True

    def test_request_missing_key_returns_error(self, with_callback):
        result = json.loads(secrets_tool({'action': 'request', 'key': ''}))
        assert 'error' in result

    def test_request_skipped_callback_returns_not_stored(self):
        token = set_secret_request_session_key(_SESSION)
        skipped_cb = MagicMock(return_value={
            'success': False,
            'skipped': True,
            'message': 'User skipped.',
        })
        set_secrets_request_callback(_SESSION, skipped_cb)
        try:
            result = json.loads(secrets_tool({'action': 'request', 'key': _SECRET_KEY_2}))
            assert result.get('skipped') is True
            assert result.get('stored') is False
        finally:
            reset_secret_request_session_key(token)
            set_secrets_request_callback(_SESSION, None)

    def test_request_callback_exception_returns_failure(self):
        token = set_secret_request_session_key(_SESSION)
        error_cb = MagicMock(side_effect=RuntimeError('callback exploded'))
        set_secrets_request_callback(_SESSION, error_cb)
        try:
            result = json.loads(secrets_tool({'action': 'request', 'key': _SECRET_KEY_2}))
            assert result.get('success') is False
            assert 'error' in result
        finally:
            reset_secret_request_session_key(token)
            set_secrets_request_callback(_SESSION, None)


# ── delete ────────────────────────────────────────────────────────────────────


class TestSecretsDelete:
    def test_delete_existing_key_reports_success(self):
        result = json.loads(secrets_tool({'action': 'delete', 'key': _SECRET_KEY}))
        assert result.get('success') is True

    def test_delete_existing_key_removes_from_file(self):
        hermes_home = Path(os.environ['HERMES_HOME'])
        secrets_tool({'action': 'delete', 'key': _SECRET_KEY})
        env_content = (hermes_home / '.env').read_text(encoding='utf-8')
        assert _SECRET_KEY not in env_content

    def test_delete_includes_deleted_key_in_response(self):
        result = json.loads(secrets_tool({'action': 'delete', 'key': _SECRET_KEY}))
        assert result.get('deleted') == _SECRET_KEY

    def test_delete_non_secret_name_returns_error(self):
        result = json.loads(secrets_tool({'action': 'delete', 'key': 'PLAIN_NONSECRET'}))
        assert 'error' in result

    def test_delete_missing_key_reports_success(self):
        # _delete_env_key is a best-effort operation; missing key still returns success.
        result = json.loads(secrets_tool({'action': 'delete', 'key': 'NONEXISTENT_API_KEY'}))
        assert result.get('success') is True


# ── inject ────────────────────────────────────────────────────────────────────


class TestSecretsInject:
    def test_inject_existing_key_returns_success(self):
        result = json.loads(secrets_tool({'action': 'inject', 'keys': [_SECRET_KEY]}))
        assert result.get('success') is True

    def test_inject_existing_key_appears_in_injected(self):
        result = json.loads(secrets_tool({'action': 'inject', 'keys': [_SECRET_KEY]}))
        assert _SECRET_KEY in result.get('injected', [])

    def test_inject_missing_key_appears_in_missing(self):
        result = json.loads(secrets_tool({'action': 'inject', 'keys': ['MISSING_API_KEY']}))
        assert 'MISSING_API_KEY' in result.get('missing', [])
        assert 'MISSING_API_KEY' not in result.get('injected', [])

    def test_inject_non_secret_name_appears_in_rejected(self):
        result = json.loads(secrets_tool({'action': 'inject', 'keys': ['PLAIN_VAR']}))
        assert 'PLAIN_VAR' in result.get('rejected', [])

    def test_inject_invalid_keys_type_returns_error(self):
        result = json.loads(secrets_tool({'action': 'inject', 'keys': 'not-a-list'}))
        assert 'error' in result


# ── unknown action ────────────────────────────────────────────────────────────


class TestSecretsUnknownAction:
    def test_unknown_action_returns_error(self):
        result = json.loads(secrets_tool({'action': 'frobnicate'}))
        assert 'error' in result

    def test_missing_action_returns_error(self):
        result = json.loads(secrets_tool({}))
        assert 'error' in result
