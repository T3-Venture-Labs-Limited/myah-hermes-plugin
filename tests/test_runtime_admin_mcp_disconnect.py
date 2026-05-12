"""Tests for the runtime_admin /myah/v1/admin/mcp/disconnect/{name} endpoint.

Phase 5 regression gate. The handler at runtime_admin.py:disconnect_mcp
previously imported ``disconnect_mcp_server`` from ``agent.mcp_registry`` —
a module that doesn't exist anywhere in the repo (verified via repo-wide
grep). Every call hit the ``except Exception`` branch and returned a 500
``"MCP registry module not available"``, so the dashboard's
``DELETE /mcp/<name>`` route — which delegates to this endpoint over HTTP
(see test_myah_admin_skills_plugins_mcp.py:425) — silently failed to
actually disconnect anything.

The correct module is ``myah_hermes_plugin.runtime_extensions.mcp_disconnect``
(Phase E, shipped in PR #106). This test drives ``_make_handlers`` against
a fake ``tools.mcp_tool`` private state and asserts the endpoint actually
runs the real disconnect path (returns 200 + ``ok: True``), NOT the
import-error fallback.

Without this test the dashboard appears to work (the dashboard layer
captures the 500 and falls through to ``servers.pop()`` in its own
config-yaml mutation), but the underlying ``tools.mcp_tool._servers``
dict on the gateway side keeps the stale reference, so the toolset
cache still treats the MCP server as connected until the gateway
restarts.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from myah_hermes_plugin.myah_platform.runtime_admin import _make_handlers


@pytest.fixture
def fake_runner():
    return MagicMock(name='GatewayRunner')


@pytest.mark.asyncio
async def test_disconnect_mcp_runs_real_disconnect_path(fake_runner, monkeypatch):
    """The endpoint must resolve the plugin-side disconnect helper and
    actually remove the server from tools.mcp_tool._servers.

    Pre-Phase-5 the handler imported from ``agent.mcp_registry`` (does not
    exist) and ALWAYS hit the ``except Exception`` branch returning 500.
    The plugin-side ``runtime_extensions.mcp_disconnect`` has been shipped
    since PR #106; this test pins the runtime_admin handler to use it.
    """
    import tools.mcp_tool as mcp_tool

    fake_server = MagicMock(name='MCPServerTask')
    fake_server.shutdown.return_value = MagicMock(name='shutdown_coro')
    servers = {'my-mcp': fake_server}

    def fake_run_on_loop(coro, timeout=15):
        # Plugin-side disconnect helper drives shutdown() through this
        # cross-loop bridge. Recording the call is enough — we don't need
        # to actually await the coro for unit-test purposes.
        return None

    monkeypatch.setattr(mcp_tool, '_servers', servers, raising=False)
    monkeypatch.setattr(mcp_tool, '_lock', threading.Lock(), raising=False)
    monkeypatch.setattr(mcp_tool, '_run_on_mcp_loop', fake_run_on_loop, raising=False)

    handlers = _make_handlers(fake_runner, auth_key='')
    req = make_mocked_request(
        'POST',
        '/myah/v1/admin/mcp/disconnect/my-mcp',
        match_info={'name': 'my-mcp'},
    )

    resp = await handlers['disconnect_mcp'](req)

    assert resp.status == 200, (
        f'Expected 200 from the real disconnect path; got {resp.status} '
        f'(body={resp.body!r}). If this is 500 with "MCP registry module not '
        f"available\", the handler is still importing the dead "
        f'agent.mcp_registry module — apply the Phase 5 import swap.'
    )
    body = json.loads(resp.body.decode())
    assert body == {'ok': True, 'name': 'my-mcp'}
    # Cross-check: the real disconnect helper popped the server.
    assert 'my-mcp' not in servers, (
        'The plugin-side disconnect_mcp_server should have popped the '
        'server from tools.mcp_tool._servers — if this is still present, '
        'the handler imported from a stub or returned early.'
    )


@pytest.mark.asyncio
async def test_disconnect_mcp_no_such_server_still_returns_ok(fake_runner, monkeypatch):
    """Unknown server name: helper returns False, but the HTTP layer still
    reports ok=True (the caller already wanted it gone — idempotent).

    This matches the dashboard-layer behaviour at
    test_myah_admin_skills_plugins_mcp.py::test_remove_mcp_writes_config_and_calls_disconnect_then_evict
    where the dashboard treats a successful disconnect as the precondition
    for the cache-evict-all that follows; surfacing a 404 here would
    needlessly abort the cleanup chain.
    """
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, '_servers', {}, raising=False)
    monkeypatch.setattr(mcp_tool, '_lock', threading.Lock(), raising=False)
    monkeypatch.setattr(
        mcp_tool, '_run_on_mcp_loop', lambda c, timeout=15: None, raising=False
    )

    handlers = _make_handlers(fake_runner, auth_key='')
    req = make_mocked_request(
        'POST',
        '/myah/v1/admin/mcp/disconnect/ghost',
        match_info={'name': 'ghost'},
    )

    resp = await handlers['disconnect_mcp'](req)

    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body == {'ok': True, 'name': 'ghost'}
