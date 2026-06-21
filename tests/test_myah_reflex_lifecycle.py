"""Tests for Reflex webhook lifecycle callbacks from MyahAdapter."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from gateway.config import PlatformConfig

from myah_hermes_plugin.myah_admin.dashboard import _webhooks
from myah_hermes_plugin.myah_platform.adapter import MyahAdapter


class FakeResp:
    def __init__(self, status: int, body: dict[str, Any] | None = None):
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return str(self._body)


class FakeSession:
    def __init__(self, posts: list[dict[str, Any]], *args, **kwargs):
        self.posts = posts

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, *, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        if url.endswith("/run-started"):
            return FakeResp(200, {"ok": True, "run_id": "run-1", "chat_id": json["chat_id"]})
        return FakeResp(200, {"ok": True, "run_id": json["run_id"], "status": "completed"})


@pytest.mark.asyncio
async def test_webhook_chat_completion_posts_reflex_run_started_and_complete(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "https://app.myah.test")
    monkeypatch.setenv("MYAH_PLATFORM_BEARER", "bearer-token")
    monkeypatch.setenv("MYAH_USER_ID", "user-1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _webhooks.save_webhook_subscriptions(
        {
            "reflex-rx-1": {
                "events": ["gmail.new_email"],
                "prompt": "Draft a reply",
                "secret": "abc123",
                "deliver": "myah",
                "deliver_extra": {"reflex_id": "rx-1"},
            }
        }
    )
    adapter = MyahAdapter(PlatformConfig(enabled=True, extra={"auth_key": "test"}))

    posts: list[dict[str, Any]] = []
    with patch("aiohttp.ClientSession", lambda *a, **kw: FakeSession(posts, *a, **kw)):
        result = await adapter.send(
            "webhook:reflex-rx-1:myah",
            "Drafted reply",
            metadata=None,
        )

    assert result.success is True
    assert [post["url"].rsplit("/", 1)[-1] for post in posts] == ["run-started", "run-complete"]
    assert posts[0]["headers"] == {"Authorization": "Bearer bearer-token"}
    assert posts[0]["json"] == {
        "reflex_id": "rx-1",
        "user_id": "user-1",
        "chat_id": "webhook:reflex-rx-1:myah",
        "payload": {"route_name": "reflex-rx-1", "delivery_id": "myah"},
    }
    assert posts[1]["json"] == {
        "run_id": "run-1",
        "user_id": "user-1",
        "status": "completed",
        "summary": "Drafted reply",
        "chat_id": "webhook:reflex-rx-1:myah",
    }

@pytest.mark.asyncio
async def test_webhook_lifecycle_merges_payload_with_delivery_identifiers(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "https://app.myah.test")
    monkeypatch.setenv("MYAH_PLATFORM_BEARER", "bearer-token")
    monkeypatch.setenv("MYAH_USER_ID", "user-1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _webhooks.save_webhook_subscriptions(
        {
            "reflex-rx-1": {
                "events": ["shopify.order_created"],
                "prompt": "Summarize the order",
                "secret": "abc123",
                "deliver": "myah",
                "deliver_extra": {"reflex_id": "rx-1"},
            }
        }
    )
    adapter = MyahAdapter(PlatformConfig(enabled=True, extra={"auth_key": "test"}))

    posts: list[dict[str, Any]] = []
    payload = {"data": {"object": {"id": "order-123"}}}
    with patch("aiohttp.ClientSession", lambda *a, **kw: FakeSession(posts, *a, **kw)):
        result = await adapter.send(
            "webhook:reflex-rx-1:delivery-1",
            "Drafted reply",
            metadata={"payload": payload, "event_id": "evt-1", "route_name": "reflex-rx-1"},
        )

    assert result.success is True
    assert posts[0]["json"]["payload"] == {
        "data": {"object": {"id": "order-123"}},
        "route_name": "reflex-rx-1",
        "delivery_id": "delivery-1",
        "event_id": "evt-1",
    }

@pytest.mark.asyncio
async def test_webhook_lifecycle_uses_authoritative_identifiers_over_payload_conflicts(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "https://app.myah.test")
    monkeypatch.setenv("MYAH_PLATFORM_BEARER", "bearer-token")
    monkeypatch.setenv("MYAH_USER_ID", "user-1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _webhooks.save_webhook_subscriptions(
        {
            "reflex-rx-1": {
                "events": ["shopify.order_created"],
                "prompt": "Summarize the order",
                "secret": "abc123",
                "deliver": "myah",
                "deliver_extra": {"reflex_id": "rx-1"},
            }
        }
    )
    adapter = MyahAdapter(PlatformConfig(enabled=True, extra={"auth_key": "test"}))

    posts: list[dict[str, Any]] = []
    payload = {
        "route_name": "provider-route",
        "delivery_id": "provider-delivery",
        "event_id": "provider-event",
        "data": {"object": {"id": "order-123"}},
    }
    with patch("aiohttp.ClientSession", lambda *a, **kw: FakeSession(posts, *a, **kw)):
        result = await adapter.send(
            "webhook:reflex-rx-1:trusted-delivery",
            "Drafted reply",
            metadata={"payload": payload, "event_id": "trusted-event", "route_name": "reflex-rx-1"},
        )

    assert result.success is True
    posted_payload = posts[0]["json"]["payload"]
    assert posted_payload["route_name"] == "reflex-rx-1"
    assert posted_payload["delivery_id"] == "trusted-delivery"
    assert posted_payload["event_id"] == "trusted-event"
    assert posted_payload["data"] == {"object": {"id": "order-123"}}

@pytest.mark.asyncio
async def test_webhook_lifecycle_drops_untrusted_payload_event_id_without_trusted_event(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "https://app.myah.test")
    monkeypatch.setenv("MYAH_PLATFORM_BEARER", "bearer-token")
    monkeypatch.setenv("MYAH_USER_ID", "user-1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _webhooks.save_webhook_subscriptions(
        {
            "reflex-rx-1": {
                "events": ["shopify.order_created"],
                "prompt": "Summarize the order",
                "secret": "abc123",
                "deliver": "myah",
                "deliver_extra": {"reflex_id": "rx-1", "delivery_id": "configured-delivery"},
            }
        }
    )
    adapter = MyahAdapter(PlatformConfig(enabled=True, extra={"auth_key": "test"}))

    posts: list[dict[str, Any]] = []
    payload = {"event_id": "provider-event", "data": {"object": {"id": "order-123"}}}
    with patch("aiohttp.ClientSession", lambda *a, **kw: FakeSession(posts, *a, **kw)):
        result = await adapter.send(
            "webhook:reflex-rx-1:trusted-delivery",
            "Drafted reply",
            metadata={"payload": payload, "delivery_id": "metadata-delivery"},
        )

    assert result.success is True
    posted_payload = posts[0]["json"]["payload"]
    assert posted_payload["delivery_id"] == "trusted-delivery"
    assert posted_payload["route_name"] == "reflex-rx-1"
    assert "event_id" not in posted_payload
    assert posted_payload["data"] == {"object": {"id": "order-123"}}
