"""Tests for the OSS user_id bootstrap in the plugin's register(ctx).

Hosted Myah injects MYAH_USER_ID per-container at spawn time, so the
bootstrap is a no-op there. OSS self-hosted has no spawner — the
plugin queries the platform's /whoami endpoint at register time to
auto-discover its own MYAH_USER_ID.

Coverage:
1. Idempotent: MYAH_USER_ID already set → no /whoami call.
2. Missing config (base_url or bearer) → silent skip with log.
3. HTTP error from /whoami → silent skip with log.
4. /whoami 200 OK with user_id → MYAH_USER_ID populated.
5. /whoami 200 OK with empty user_id → silent skip with log.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure MYAH_USER_ID and platform-related env vars start unset."""
    for var in (
        'MYAH_USER_ID',
        'MYAH_PLATFORM_BASE_URL',
        'MYAH_PLATFORM_BEARER',
        'MYAH_AGENT_BEARER_TOKEN',
        'MYAH_AGENT_TOKEN',
    ):
        monkeypatch.delenv(var, raising=False)


def _fake_whoami_response(payload: dict) -> MagicMock:
    """Build an object that mimics urllib.request.urlopen's context manager."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_bootstrap_skips_when_user_id_already_set(clean_env, monkeypatch):
    """If MYAH_USER_ID is set (hosted spawner injected it), don't call /whoami."""
    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_USER_ID', 'preexisting-user-id')
    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080')
    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'tok')

    with patch('urllib.request.urlopen') as urlopen:
        _bootstrap_user_id()

    urlopen.assert_not_called()


def test_bootstrap_skips_without_base_url(clean_env, monkeypatch):
    """No MYAH_PLATFORM_BASE_URL → silent skip (no /whoami call)."""
    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'tok')
    # MYAH_PLATFORM_BASE_URL unset

    with patch('urllib.request.urlopen') as urlopen:
        _bootstrap_user_id()

    urlopen.assert_not_called()


def test_bootstrap_skips_without_bearer(clean_env, monkeypatch):
    """No platform bearer in env → silent skip (no /whoami call)."""
    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080')
    # No MYAH_AGENT_BEARER_TOKEN / MYAH_PLATFORM_BEARER / MYAH_AGENT_TOKEN

    with patch('urllib.request.urlopen') as urlopen:
        _bootstrap_user_id()

    urlopen.assert_not_called()


def test_bootstrap_populates_user_id_on_success(clean_env, monkeypatch):
    """Happy path: /whoami returns user_id → MYAH_USER_ID set in env."""
    import os

    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080')
    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'tok')

    fake_resp = _fake_whoami_response(
        {'user_id': 'user-123', 'user_name': 'Alice', 'deployment_mode': 'oss'}
    )
    with patch('urllib.request.urlopen', return_value=fake_resp) as urlopen:
        _bootstrap_user_id()

    urlopen.assert_called_once()
    # Verify the request URL was correct
    req = urlopen.call_args[0][0]
    assert req.full_url == 'http://platform:8080/api/v1/myah/whoami'
    assert req.headers.get('Authorization', '') == 'Bearer tok'
    assert os.environ.get('MYAH_USER_ID') == 'user-123'


def test_bootstrap_handles_http_error(clean_env, monkeypatch):
    """HTTPError (e.g. 404 no users) → silent skip with log; MYAH_USER_ID stays unset."""
    import os

    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080')
    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'tok')

    err = urllib.error.HTTPError(
        url='http://platform:8080/api/v1/myah/whoami',
        code=404,
        msg='Not Found',
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b''),
    )

    with patch('urllib.request.urlopen', side_effect=err):
        _bootstrap_user_id()

    assert 'MYAH_USER_ID' not in os.environ


def test_bootstrap_handles_url_error(clean_env, monkeypatch):
    """URLError (network unreachable) → silent skip; MYAH_USER_ID stays unset."""
    import os

    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080')
    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'tok')

    with patch(
        'urllib.request.urlopen', side_effect=urllib.error.URLError('connection refused')
    ):
        _bootstrap_user_id()

    assert 'MYAH_USER_ID' not in os.environ


def test_bootstrap_handles_empty_user_id_in_response(clean_env, monkeypatch):
    """/whoami returns 200 but empty user_id → silent skip; MYAH_USER_ID stays unset."""
    import os

    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080')
    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'tok')

    fake_resp = _fake_whoami_response({'user_id': '', 'user_name': '', 'deployment_mode': 'oss'})
    with patch('urllib.request.urlopen', return_value=fake_resp):
        _bootstrap_user_id()

    assert 'MYAH_USER_ID' not in os.environ


def test_bootstrap_uses_alias_token_var(clean_env, monkeypatch):
    """Three env-var aliases for the bearer: MYAH_PLATFORM_BEARER /
    MYAH_AGENT_BEARER_TOKEN / MYAH_AGENT_TOKEN. Any one is acceptable."""
    import os

    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080')
    # Use the legacy MYAH_AGENT_TOKEN alias
    monkeypatch.setenv('MYAH_AGENT_TOKEN', 'legacy-tok')

    fake_resp = _fake_whoami_response({'user_id': 'user-x', 'user_name': '', 'deployment_mode': 'oss'})
    with patch('urllib.request.urlopen', return_value=fake_resp) as urlopen:
        _bootstrap_user_id()

    assert urlopen.call_args[0][0].headers.get('Authorization') == 'Bearer legacy-tok'
    assert os.environ.get('MYAH_USER_ID') == 'user-x'


def test_bootstrap_strips_trailing_slash_from_base_url(clean_env, monkeypatch):
    """Base URLs with trailing slashes shouldn't produce double-slash paths."""
    from myah_hermes_plugin.myah_platform import _bootstrap_user_id

    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'http://platform:8080/')
    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'tok')

    fake_resp = _fake_whoami_response({'user_id': 'u', 'user_name': '', 'deployment_mode': 'oss'})
    with patch('urllib.request.urlopen', return_value=fake_resp) as urlopen:
        _bootstrap_user_id()

    assert urlopen.call_args[0][0].full_url == 'http://platform:8080/api/v1/myah/whoami'
