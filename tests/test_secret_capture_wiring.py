"""Tests for the F4 secret-capture global callback wiring (Phase 5.1).

Vanilla upstream's tools/skills_tool exposes set_secret_capture_callback(fn)
— a single global slot for the secret-prompt callback. The plugin
registers a wrapper at register-time that:

1. Takes the vanilla-shaped (name, prompt, metadata) signature.
2. Looks up the active MyahAdapter via _LATEST_ADAPTER module attr.
3. Resolves stream_id from the active session_key contextvar.
4. Delegates to adapter._secret_capture_callback with the right stream.

These tests verify the wiring without triggering a real agent run.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _build_fake_adapter(*, with_streams=True):
    """Construct a stub adapter with the attrs the secret-capture wrapper consults."""
    adapter = MagicMock()
    if with_streams:
        adapter._session_streams = {'session-A': 'stream-A'}
    else:
        adapter._session_streams = {}
    # Configure the bound method to return a sentinel result so we can
    # assert the right adapter was reached.
    adapter._secret_capture_callback.return_value = {
        'success': True,
        'skipped': False,
        'stored_as': 'TEST_KEY',
        'validated': True,
        'message': 'mocked',
    }
    return adapter


def test_wrapper_routes_to_adapter_via_session_key():
    """The wrapper resolves stream_id from session_key and calls the adapter."""
    from myah_hermes_plugin.myah_platform import adapter as adapter_module
    from myah_hermes_plugin.myah_platform import _wire_secret_capture_callback

    fake_adapter = _build_fake_adapter()
    with patch.object(adapter_module, '_LATEST_ADAPTER', fake_adapter), \
         patch('tools.skills_tool.set_secret_capture_callback') as set_cb:
        _wire_secret_capture_callback()
        # Capture the registered wrapper INSIDE the patch scope so the
        # wrapper's lookup of adapter_module._LATEST_ADAPTER still sees
        # the patched value when we invoke it.
        set_cb.assert_called_once()
        wrapper = set_cb.call_args[0][0]

        from tools.approval import set_current_session_key, reset_current_session_key
        tok = set_current_session_key('session-A')
        try:
            result = wrapper('TEST_KEY', 'Enter your test key', {'help': 'hint'})
        finally:
            reset_current_session_key(tok)

    fake_adapter._secret_capture_callback.assert_called_once_with(
        'TEST_KEY',
        'Enter your test key',
        metadata={'help': 'hint'},
        stream_id='stream-A',
    )
    assert result['validated'] is True


def test_wrapper_falls_back_to_first_stream_when_session_key_unset():
    """If contextvar isn't set, fall back to any active stream (single-tenant assumption)."""
    from myah_hermes_plugin.myah_platform import adapter as adapter_module
    from myah_hermes_plugin.myah_platform import _wire_secret_capture_callback

    fake_adapter = _build_fake_adapter()

    with patch.object(adapter_module, '_LATEST_ADAPTER', fake_adapter), \
         patch('tools.skills_tool.set_secret_capture_callback') as set_cb:
        _wire_secret_capture_callback()
        wrapper = set_cb.call_args[0][0]

        # Don't set a session_key contextvar — wrapper should still route to
        # the only active stream.
        result = wrapper('K', 'p', {})

    fake_adapter._secret_capture_callback.assert_called_once()
    kwargs = fake_adapter._secret_capture_callback.call_args.kwargs
    assert kwargs['stream_id'] == 'stream-A'
    assert result['stored_as'] == 'TEST_KEY'


def test_wrapper_skips_gracefully_when_no_adapter_active():
    """If no MyahAdapter has been constructed, the wrapper auto-skips with success."""
    from myah_hermes_plugin.myah_platform import adapter as adapter_module
    from myah_hermes_plugin.myah_platform import _wire_secret_capture_callback

    with patch.object(adapter_module, '_LATEST_ADAPTER', None), \
         patch('tools.skills_tool.set_secret_capture_callback') as set_cb:
        _wire_secret_capture_callback()
        wrapper = set_cb.call_args[0][0]

        result = wrapper('K', 'p', None)

    assert result['skipped'] is True
    assert result['stored_as'] == 'K'
    assert 'No active Myah adapter' in result['message']


def test_wrapper_skips_when_no_streams_active():
    """If the adapter exists but no stream is open, wrapper passes empty stream_id."""
    from myah_hermes_plugin.myah_platform import adapter as adapter_module
    from myah_hermes_plugin.myah_platform import _wire_secret_capture_callback

    fake_adapter = _build_fake_adapter(with_streams=False)
    fake_adapter._secret_capture_callback.return_value = {
        'success': True,
        'skipped': True,
        'stored_as': 'K',
        'validated': False,
        'message': 'No stream',
    }

    with patch.object(adapter_module, '_LATEST_ADAPTER', fake_adapter), \
         patch('tools.skills_tool.set_secret_capture_callback') as set_cb:
        _wire_secret_capture_callback()
        wrapper = set_cb.call_args[0][0]

        wrapper('K', 'p', None)

    kwargs = fake_adapter._secret_capture_callback.call_args.kwargs
    assert kwargs['stream_id'] == ''


def test_wire_silent_noop_when_skills_tool_unavailable():
    """If tools.skills_tool can't be imported, wiring silently no-ops (older Hermes builds)."""
    from myah_hermes_plugin.myah_platform import _wire_secret_capture_callback

    # Force ImportError on the skills_tool import path
    with patch.dict('sys.modules', {'tools.skills_tool': None}):
        # Should not raise
        _wire_secret_capture_callback()


def test_latest_adapter_pointer_set_on_construction():
    """MyahAdapter.__init__ updates _LATEST_ADAPTER so the wrapper finds it."""
    from gateway.config import PlatformConfig

    from myah_hermes_plugin.myah_platform import adapter as adapter_module

    # Reset to a known state
    adapter_module._LATEST_ADAPTER = None

    # Construct an adapter using the same shape used in production (see
    # Phase 4d MyahAdapter registration in myah_platform/__init__.py).
    cfg = PlatformConfig(enabled=True, extra={'auth_key': 'x'})
    adapter_inst = adapter_module.MyahAdapter(cfg)

    assert adapter_module._LATEST_ADAPTER is adapter_inst
