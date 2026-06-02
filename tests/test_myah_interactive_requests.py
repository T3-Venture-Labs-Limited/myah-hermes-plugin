"""Tests for Myah interactive human-in-the-loop request endpoints."""

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig

_TEST_AUTH_KEY = "test-bearer-key-for-interactive-requests"
_AUTHED_HEADERS = {"Authorization": f"Bearer {_TEST_AUTH_KEY}"}


def _make_adapter():
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter

    return MyahAdapter(PlatformConfig(enabled=True, extra={"auth_key": _TEST_AUTH_KEY}))


def _make_app(adapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/myah/v1/clarify/{stream_id}", adapter._handle_clarify_endpoint)
    app.router.add_post("/myah/v1/secret/{stream_id}", adapter._handle_secret_endpoint)
    return app


@pytest.mark.asyncio
async def test_send_clarify_emits_required_event_and_endpoint_resolves_response():
    adapter = _make_adapter()
    stream_id = "stream-clarify"
    session_key = "session-clarify"
    adapter._session_streams[session_key] = stream_id
    adapter._chat_id_streams["chat-1"] = stream_id
    # _push_event is patched, so no concrete asyncio.Queue is needed.

    pushed = []

    with patch.object(adapter, "_push_event", side_effect=lambda sid, evt: pushed.append((sid, evt))):
        result = await adapter.send_clarify(
            chat_id="chat-1",
            question="Which env?",
            choices=["staging", "prod"],
            clarify_id="clarify-123",
            session_key=session_key,
        )

        assert result.success is True
        assert pushed[0][0] == stream_id
        assert pushed[0][1]["event"] == "clarify.required"
        assert pushed[0][1]["clarify_id"] == "clarify-123"
        assert pushed[0][1]["choices"] == ["staging", "prod"]

        with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    f"/myah/v1/clarify/{stream_id}",
                    json={"clarify_id": "clarify-123", "response": "staging"},
                    headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 200, body
        assert body == {"ok": True, "resolved": 1}
        resolve.assert_called_once_with("clarify-123", "staging")
        assert pushed[-1][1]["event"] == "clarify.resolved"
        assert pushed[-1][1]["status"] == "answered"
        assert "clarify-123" not in adapter._pending_clarifies.get(stream_id, {})


@pytest.mark.asyncio
async def test_clarify_endpoint_rejects_unknown_or_blank_response():
    adapter = _make_adapter()
    stream_id = "stream-clarify"

    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            f"/myah/v1/clarify/{stream_id}",
            json={"clarify_id": "missing", "response": "answer"},
            headers=_AUTHED_HEADERS,
        )
        assert resp.status == 404

        resp = await cli.post(
            f"/myah/v1/clarify/{stream_id}",
            json={"clarify_id": "missing", "response": "   "},
            headers=_AUTHED_HEADERS,
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_secret_endpoint_cancel_unblocks_capture_without_value():
    adapter = _make_adapter()
    stream_id = "stream-secret"

    class FakeEvent:
        def __init__(self):
            self.set_called = False

        def set(self):
            self.set_called = True

    event = FakeEvent()
    adapter._pending_secrets[stream_id] = {
        "event": event,
        "var_name": "OPENROUTER_API_KEY",
        "result": None,
    }

    async with TestClient(TestServer(_make_app(adapter))) as cli:
        resp = await cli.post(
            f"/myah/v1/secret/{stream_id}",
            json={"var_name": "OPENROUTER_API_KEY", "cancel": True},
            headers=_AUTHED_HEADERS,
        )
        body = await resp.json()

    assert resp.status == 200, body
    assert body == {"ok": True, "cancelled": True, "stored_as": "OPENROUTER_API_KEY"}
    assert event.set_called is True
    assert adapter._pending_secrets[stream_id]["result"] == {
        "success": True,
        "skipped": True,
        "stored_as": "OPENROUTER_API_KEY",
        "validated": False,
        "message": "Secret entry cancelled.",
        "cancelled": True,
    }
