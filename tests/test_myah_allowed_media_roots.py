"""Tests for the configurable _myah_allowed_media_roots derivation.

Validates the Myah-marked addition that derives allowed media roots
from Hermes' terminal.cwd config plus MYAH_MEDIA_ALLOWED_ROOTS env var,
in addition to the always-allowed cache directories.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

# Adapter and runtime-admin now fail closed when auth_key is empty.
# Tests must construct adapters with a real auth_key and authed headers.
_TEST_AUTH_KEY = "test-bearer-key-for-test_myah_allowed_media_roots"
_AUTHED_HEADERS = {"Authorization": f"Bearer {_TEST_AUTH_KEY}"}



def _make_adapter():
    """Return a MyahAdapter instance bypassing __init__ for unit tests."""
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
    return MyahAdapter.__new__(MyahAdapter)


def test_allowed_roots_includes_terminal_cwd_from_config(monkeypatch, tmp_path):
    """When config.yaml has terminal.cwd set, that specific path must appear.

    We use a deeply nested subdir so that the assertion cannot pass just
    because tmp_path is a parent of HERMES_HOME (which is tmp_path/hermes_test).
    """
    monkeypatch.delenv('MYAH_MEDIA_ALLOWED_ROOTS', raising=False)
    # Use a path that is NOT a parent of HERMES_HOME to avoid false positives.
    terminal_cwd = tmp_path / 'workspace' / 'myproject'
    terminal_cwd.mkdir(parents=True)
    fake_config = {'terminal': {'cwd': str(terminal_cwd)}}
    with patch('hermes_cli.config.load_config', return_value=fake_config):
        adapter = _make_adapter()
        roots = adapter._myah_allowed_media_roots()
        resolved = [str(r) for r in roots]
        assert any('workspace' in r and 'myproject' in r for r in resolved), (
            f"Expected {terminal_cwd} in allowed roots, got {resolved!r}"
        )


def test_allowed_roots_includes_env_var_paths(monkeypatch, tmp_path):
    """MYAH_MEDIA_ALLOWED_ROOTS env var (colon-separated) must extend the allowlist."""
    extra1 = str(tmp_path / 'extra_path1')
    extra2 = str(tmp_path / 'extra_path2')
    monkeypatch.setenv('MYAH_MEDIA_ALLOWED_ROOTS', f'{extra1}:{extra2}')
    fake_config = {}
    with patch('hermes_cli.config.load_config', return_value=fake_config):
        adapter = _make_adapter()
        roots = adapter._myah_allowed_media_roots()
        resolved = [str(r) for r in roots]
        assert any('extra_path1' in r for r in resolved), f"extra_path1 missing from {resolved!r}"
        assert any('extra_path2' in r for r in resolved), f"extra_path2 missing from {resolved!r}"


def test_allowed_roots_always_includes_cache_dirs(monkeypatch):
    """Even with empty config and no env var, the cache dirs must be present."""
    monkeypatch.delenv('MYAH_MEDIA_ALLOWED_ROOTS', raising=False)
    fake_config = {}
    with patch('hermes_cli.config.load_config', return_value=fake_config):
        adapter = _make_adapter()
        roots = adapter._myah_allowed_media_roots()
        resolved = [str(r) for r in roots]
        assert any('cache' in r for r in resolved), f"cache dir missing from {resolved!r}"

def test_allowed_roots_include_default_agent_artifact_dirs_when_env_missing(monkeypatch):
    """OSS/local Hermes often writes generated artifacts to /tmp, /data, /workspace, or /root.

    Hosted containers inject MYAH_MEDIA_ALLOWED_ROOTS with these directories, but
    a public OSS plugin run may not have that platform container env injection.
    The media endpoint must still allow the same default artifact dirs so
    agent-produced MEDIA:/tmp/foo.mp4 output can be fetched and persisted
    instead of becoming a media-expired placeholder.
    """
    monkeypatch.delenv('MYAH_MEDIA_ALLOWED_ROOTS', raising=False)
    fake_config = {}
    with patch('hermes_cli.config.load_config', return_value=fake_config):
        adapter = _make_adapter()
        resolved = {str(r) for r in adapter._myah_allowed_media_roots()}

    assert {'/tmp', '/data', '/workspace', '/root'}.issubset(resolved)

@pytest.mark.asyncio
async def test_media_get_serves_tmp_artifact_without_env_injection(monkeypatch, tmp_path):
    """End-to-end-ish regression: /myah/v1/media can serve a /tmp artifact in OSS.

    This exercises the same path persist_and_rewrite uses when it fetches
    MEDIA:/tmp/foo.mp4 from the adapter. Without default /tmp allowlisting,
    this returned 403 and the platform replaced the media with a placeholder.
    """
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.delenv('MYAH_MEDIA_ALLOWED_ROOTS', raising=False)
    artifact = tmp_path / 'clip.mp4'
    artifact.write_bytes(b'fake mp4 bytes')

    with patch('hermes_cli.config.load_config', return_value={}):
        adapter = _make_adapter()
        adapter._auth_key = _TEST_AUTH_KEY
        request = make_mocked_request(
            'GET',
            f'/myah/v1/media?path={artifact}',
            headers=_AUTHED_HEADERS,
        )
        response = await adapter._handle_media_get(request)

    assert response.status == 200
    assert response.headers.get('Content-Type') == 'video/mp4'

def test_allowed_roots_include_terminal_cwd_env_bridge_for_relative_config(monkeypatch, tmp_path):
    """Gateway runs often bridge terminal.cwd='.' into TERMINAL_CWD.

    If the adapter resolves config cwd='.' relative to its own process cwd,
    host files the agent actually wrote/read under TERMINAL_CWD get rejected
    with 403 by /myah/v1/media. Include the explicit env bridge when present.
    """
    workspace = tmp_path / 'gateway-workspace'
    workspace.mkdir()
    monkeypatch.setenv('TERMINAL_CWD', str(workspace))
    monkeypatch.delenv('MESSAGING_CWD', raising=False)
    monkeypatch.delenv('MYAH_MEDIA_ALLOWED_ROOTS', raising=False)

    with patch('hermes_cli.config.load_config', return_value={'terminal': {'cwd': '.'}}):
        adapter = _make_adapter()
        resolved = {str(r) for r in adapter._myah_allowed_media_roots()}

    assert str(workspace) in resolved


def test_allowed_roots_include_messaging_cwd_env_bridge(monkeypatch, tmp_path):
    """MESSAGING_CWD gets the same effective-cwd treatment as TERMINAL_CWD."""
    workspace = tmp_path / 'messaging-workspace'
    workspace.mkdir()
    monkeypatch.delenv('TERMINAL_CWD', raising=False)
    monkeypatch.setenv('MESSAGING_CWD', str(workspace))
    monkeypatch.delenv('MYAH_MEDIA_ALLOWED_ROOTS', raising=False)

    with patch('hermes_cli.config.load_config', return_value={}):
        adapter = _make_adapter()
        resolved = {str(r) for r in adapter._myah_allowed_media_roots()}

    assert str(workspace) in resolved
