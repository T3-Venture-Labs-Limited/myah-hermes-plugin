"""Tests for the plugin's standalone aiohttp runner.

Verifies the runner binds on an OS-assigned port (port=0) and that
routes registered via the runner respond to live HTTP requests.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from myah_hermes_plugin.myah_platform.standalone_runner import (
    MyahStandaloneRunner,
    resolve_default_port,
)


@pytest.mark.asyncio
async def test_runner_binds_to_ephemeral_port(monkeypatch):
    """Asking for port=0 yields an OS-assigned ephemeral port (>0)."""
    runner = MyahStandaloneRunner()

    def _no_routes(_app: web.Application) -> None:
        return None

    bound = await runner.start(_no_routes, host="127.0.0.1", port=0)
    try:
        assert bound > 0
        assert runner.bound_port == bound
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_runner_serves_registered_routes(monkeypatch):
    """Routes attached in ``register_routes`` respond to HTTP requests."""

    runner = MyahStandaloneRunner()

    async def _hello(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "msg": "hello"})

    def _attach(app: web.Application) -> None:
        app.router.add_get("/myah/test/hello", _hello)

    bound = await runner.start(_attach, host="127.0.0.1", port=0)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{bound}/myah/test/hello"
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body == {"ok": True, "msg": "hello"}
    finally:
        await runner.stop()


def test_resolve_default_port_reads_env(monkeypatch):
    """``resolve_default_port`` reads ``MYAH_GATEWAY_PORT`` and falls back
    to 8643 when unset/invalid."""
    monkeypatch.delenv("MYAH_GATEWAY_PORT", raising=False)
    assert resolve_default_port() == 8643

    monkeypatch.setenv("MYAH_GATEWAY_PORT", "9999")
    assert resolve_default_port() == 9999

    monkeypatch.setenv("MYAH_GATEWAY_PORT", "not-a-number")
    assert resolve_default_port() == 8643
