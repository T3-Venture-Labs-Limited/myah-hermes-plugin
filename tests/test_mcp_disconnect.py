"""Tests for F7 per-server MCP disconnect (Phase 5.2).

Vanilla upstream's tools/mcp_tool only exposes a "shutdown ALL servers"
helper. The plugin's admin UI needs per-server teardown via direct
access to the module's private state:
- _servers: Dict[str, MCPServerTask]
- _lock: threading.Lock                (sync, NOT asyncio)
- _run_on_mcp_loop(coro, timeout=...)  (cross-loop bridge)

These tests verify:

1. Returns False when no server is registered.
2. Returns True after popping the server from _servers.
3. Calls shutdown() via _run_on_mcp_loop (NOT directly).
4. Tolerates shutdown() raising — still pops the server.
5. CI guard: upstream private state must still exist at plugin CI time.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


def test_returns_false_when_server_missing(monkeypatch):
    """Unknown name → returns False without touching _servers."""
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, '_servers', {}, raising=False)
    monkeypatch.setattr(mcp_tool, '_lock', threading.Lock(), raising=False)
    monkeypatch.setattr(
        mcp_tool, '_run_on_mcp_loop', lambda c, timeout=15: None, raising=False
    )

    from myah_hermes_plugin.runtime_extensions.mcp_disconnect import disconnect_mcp_server

    result = disconnect_mcp_server('nonexistent')
    assert result is False


def test_calls_shutdown_via_run_on_mcp_loop(monkeypatch):
    """Happy path: server.shutdown() runs through _run_on_mcp_loop, then popped."""
    import tools.mcp_tool as mcp_tool

    fake_server = MagicMock()
    fake_shutdown_coro = MagicMock(name='shutdown_coro')
    fake_server.shutdown.return_value = fake_shutdown_coro

    captured = {}

    def fake_run_on_loop(coro, timeout=15):
        captured['coro'] = coro
        captured['timeout'] = timeout

    servers = {'my-server': fake_server}
    monkeypatch.setattr(mcp_tool, '_servers', servers, raising=False)
    monkeypatch.setattr(mcp_tool, '_lock', threading.Lock(), raising=False)
    monkeypatch.setattr(mcp_tool, '_run_on_mcp_loop', fake_run_on_loop, raising=False)

    from myah_hermes_plugin.runtime_extensions.mcp_disconnect import disconnect_mcp_server

    result = disconnect_mcp_server('my-server')

    assert result is True
    fake_server.shutdown.assert_called_once()
    assert captured['coro'] is fake_shutdown_coro
    assert captured['timeout'] == 15.0
    assert 'my-server' not in servers


def test_explicit_timeout_propagates(monkeypatch):
    """Caller-supplied timeout must reach _run_on_mcp_loop."""
    import tools.mcp_tool as mcp_tool

    fake_server = MagicMock()
    captured = {}

    def fake_run_on_loop(coro, timeout=15):
        captured['timeout'] = timeout

    monkeypatch.setattr(mcp_tool, '_servers', {'s': fake_server}, raising=False)
    monkeypatch.setattr(mcp_tool, '_lock', threading.Lock(), raising=False)
    monkeypatch.setattr(mcp_tool, '_run_on_mcp_loop', fake_run_on_loop, raising=False)

    from myah_hermes_plugin.runtime_extensions.mcp_disconnect import disconnect_mcp_server

    disconnect_mcp_server('s', timeout=42.5)
    assert captured['timeout'] == 42.5


def test_swallows_shutdown_exception_and_still_pops(monkeypatch):
    """If shutdown() raises, the server is still removed (don't strand state)."""
    import tools.mcp_tool as mcp_tool

    fake_server = MagicMock()
    fake_server.shutdown.return_value = MagicMock(name='coro')

    def boom(coro, timeout=15):
        raise RuntimeError('mcp server hung')

    servers = {'bad': fake_server}
    monkeypatch.setattr(mcp_tool, '_servers', servers, raising=False)
    monkeypatch.setattr(mcp_tool, '_lock', threading.Lock(), raising=False)
    monkeypatch.setattr(mcp_tool, '_run_on_mcp_loop', boom, raising=False)

    from myah_hermes_plugin.runtime_extensions.mcp_disconnect import disconnect_mcp_server

    result = disconnect_mcp_server('bad')
    assert result is True
    assert 'bad' not in servers


def test_returns_false_when_private_state_missing(monkeypatch):
    """Upstream rename guard: missing _servers attribute → False, not exception."""
    import tools.mcp_tool as mcp_tool

    # Wipe both private attrs to simulate a hypothetical upstream refactor
    monkeypatch.delattr(mcp_tool, '_servers', raising=False)
    monkeypatch.delattr(mcp_tool, '_lock', raising=False)

    from myah_hermes_plugin.runtime_extensions.mcp_disconnect import disconnect_mcp_server

    result = disconnect_mcp_server('whatever')
    assert result is False


# ── CI guard against upstream API drift ─────────────────────────────


def test_upstream_state_present():
    """If this fails, upstream renamed/removed _servers, _lock, or
    _run_on_mcp_loop. Investigate before merging."""
    import tools.mcp_tool as mcp_tool

    assert hasattr(mcp_tool, '_servers'), (
        'upstream tools.mcp_tool._servers missing — F7 disconnect broken'
    )
    assert hasattr(mcp_tool, '_lock'), (
        'upstream tools.mcp_tool._lock missing — F7 disconnect broken'
    )
    assert hasattr(mcp_tool, '_run_on_mcp_loop'), (
        'upstream tools.mcp_tool._run_on_mcp_loop missing — '
        'cross-loop shutdown bridge broken'
    )


def test_upstream_lock_is_threading_not_asyncio():
    """Upstream uses sync threading.Lock; if it switches to asyncio.Lock
    the helper here would deadlock the gateway. Catch early."""
    import tools.mcp_tool as mcp_tool

    lock = mcp_tool._lock
    assert hasattr(lock, 'acquire') and hasattr(lock, 'release'), (
        'upstream _lock missing acquire/release — API change?'
    )

    # threading.Lock has acquire(blocking=True, timeout=-1) — asyncio.Lock
    # has acquire() without those args. The fact that we can call .locked()
    # on threading.Lock since 3.12 (and it always existed on asyncio.Lock)
    # makes a runtime-type check the most reliable: threading.Lock is
    # actually `<class 'builtin_function_or_method'>` (returns a primitive
    # lock object), while asyncio.Lock is a class.
    #
    # The cleanest probe is: threading locks are usable from non-async
    # context with a `with` statement that doesn't return a coroutine.
    with lock:
        pass  # Should NOT raise; asyncio.Lock would error here
