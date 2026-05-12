"""Tests for the configurable _myah_allowed_media_roots derivation.

Validates the Myah-marked addition that derives allowed media roots
from Hermes' terminal.cwd config plus MYAH_MEDIA_ALLOWED_ROOTS env var,
in addition to the always-allowed cache directories.
"""
from __future__ import annotations

from unittest.mock import patch



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
