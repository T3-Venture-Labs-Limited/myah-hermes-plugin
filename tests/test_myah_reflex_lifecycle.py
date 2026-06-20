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
