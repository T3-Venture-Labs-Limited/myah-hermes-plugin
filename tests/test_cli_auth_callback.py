"""Tests for relaying local-browser CLI OAuth callbacks back to the agent container."""
from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from gateway.config import PlatformConfig

_AUTH_KEY = "test-cli-auth-bearer"
_AUTHED_HEADERS = {"Authorization": f"Bearer {_AUTH_KEY}"}


def _make_adapter(auth_key: str = _AUTH_KEY):
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter

    return MyahAdapter(PlatformConfig(enabled=True, extra={"auth_key": auth_key}))


def _make_app(adapter) -> web.Application:
    app = web.Application()
    adapter._register_routes_on_app(app)
    return app


def _mark_pending_auth(adapter, stream_id: str = "stream-1", auth_id: str = "shopify-auth-1", ports=None) -> None:
    adapter._streams[stream_id] = asyncio.Queue()
    adapter._push_event_sync(stream_id, {
        "event": "cli_auth.required",
        "stream_id": stream_id,
        "auth_id": auth_id,
        "provider": "shopify",
        "allowed_callback_ports": ports or [3456],
    })
    adapter._streams[stream_id].get_nowait()


class _FakeResponse:
    def __init__(self, status: int = 204, text: str = "ok") -> None:
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        return self._text


class _FakeSession:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, **kwargs):
        self.__class__.calls.append({"url": url, **kwargs})
        return _FakeResponse()


@pytest.mark.asyncio
async def test_cli_auth_callback_rejects_unregistered_stream():
    adapter = _make_adapter()
    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            "/myah/v1/cli-auth/missing/callback",
            json={
                "auth_id": "shopify-auth-1",
                "provider": "shopify",
                "callback_url": "http://127.0.0.1:3456/callback?code=abc&state=def",
                "allowed_callback_ports": [3456],
            },
            headers=_AUTHED_HEADERS,
        )
        body = await resp.json()

    assert resp.status == 404
    assert "stream" in body["error"].lower()


@pytest.mark.asyncio
async def test_cli_auth_callback_rejects_non_localhost_url():
    adapter = _make_adapter()
    _mark_pending_auth(adapter)

    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            "/myah/v1/cli-auth/stream-1/callback",
            json={
                "auth_id": "shopify-auth-1",
                "provider": "shopify",
                "callback_url": "https://evil.example/callback?code=abc&state=def",
                "allowed_callback_ports": [3456],
            },
            headers=_AUTHED_HEADERS,
        )
        body = await resp.json()

    assert resp.status == 400
    assert "localhost" in body["error"].lower()


@pytest.mark.asyncio
async def test_cli_auth_callback_rejects_unapproved_port():
    adapter = _make_adapter()
    _mark_pending_auth(adapter)

    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            "/myah/v1/cli-auth/stream-1/callback",
            json={
                "auth_id": "shopify-auth-1",
                "provider": "shopify",
                "callback_url": "http://127.0.0.1:4444/callback?code=abc&state=def",
                "allowed_callback_ports": [3456],
            },
            headers=_AUTHED_HEADERS,
        )
        body = await resp.json()

    assert resp.status == 400
    assert "port" in body["error"].lower()


@pytest.mark.asyncio
async def test_cli_auth_callback_relays_localhost_url_and_redacts_response(monkeypatch):
    from myah_hermes_plugin.myah_platform import adapter as adapter_mod

    _FakeSession.calls = []
    monkeypatch.setattr(adapter_mod._myah_aiohttp, "ClientSession", _FakeSession)

    adapter = _make_adapter()
    _mark_pending_auth(adapter)

    callback = "http://127.0.0.1:3456/callback?code=SECRET_CODE&state=SECRET_STATE"
    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            "/myah/v1/cli-auth/stream-1/callback",
            json={
                "auth_id": "shopify-auth-1",
                "provider": "shopify",
                "callback_url": callback,
                "allowed_callback_ports": [3456],
            },
            headers=_AUTHED_HEADERS,
        )
        body = await resp.json()

    assert resp.status == 200, body
    assert body == {"ok": True, "status": 204}
    assert _FakeSession.calls[0]["url"] == callback
    assert _FakeSession.calls[0]["allow_redirects"] is False
    assert "SECRET_CODE" not in str(body)
    assert "SECRET_STATE" not in str(body)


@pytest.mark.asyncio
async def test_cli_auth_callback_ignores_request_supplied_ports(monkeypatch):
    adapter = _make_adapter()
    _mark_pending_auth(adapter, ports=[3456])

    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            "/myah/v1/cli-auth/stream-1/callback",
            json={
                "auth_id": "shopify-auth-1",
                "provider": "shopify",
                "callback_url": "http://127.0.0.1:6379/callback?code=abc&state=def",
                "allowed_callback_ports": [6379],
            },
            headers=_AUTHED_HEADERS,
        )
        body = await resp.json()

    assert resp.status == 400
    assert "port" in body["error"].lower()


@pytest.mark.asyncio
async def test_cli_auth_callback_error_event_does_not_include_listener_body(monkeypatch):
    adapter = _make_adapter()
    _mark_pending_auth(adapter)

    class RejectingSession(_FakeSession):
        def get(self, url: str, **kwargs):
            self.__class__.calls.append({"url": url, **kwargs})
            return _FakeResponse(status=500, text="code=SECRET&state=SECRET_STATE")

    from myah_hermes_plugin.myah_platform import adapter as adapter_mod

    monkeypatch.setattr(adapter_mod._myah_aiohttp, "ClientSession", RejectingSession)
    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            "/myah/v1/cli-auth/stream-1/callback",
            json={
                "auth_id": "shopify-auth-1",
                "provider": "shopify",
                "callback_url": "http://127.0.0.1:3456/callback?code=abc&state=def",
                "allowed_callback_ports": [3456],
            },
            headers=_AUTHED_HEADERS,
        )

    assert resp.status == 502
    queued = adapter._streams["stream-1"].get_nowait()
    assert queued["event"] == "cli_auth.failed"
    assert queued["error"] == "CLI callback listener returned HTTP 500"
    assert "SECRET" not in queued["error"]


def test_tool_complete_emits_pending_cli_auth_required_event():
    adapter = _make_adapter()
    adapter._streams["stream-tool"] = asyncio.Queue()
    event = adapter._extract_cli_auth_required_event(
        "stream-tool",
        "Open https://accounts.shopify.com/oauth/authorize?client_id=abc&redirect_uri=http%3A%2F%2F127.0.0.1%3A3456%2Fcallback to continue",
    )

    assert event is not None
    assert event["event"] == "cli_auth.required"
    assert event["auth_id"].startswith("shopify-")
    assert event["allowed_callback_ports"] == [3456]
    adapter._push_event_sync("stream-tool", event)
    assert adapter._pending_cli_auth["stream-tool"][event["auth_id"]]["allowed_callback_ports"] == [3456]
