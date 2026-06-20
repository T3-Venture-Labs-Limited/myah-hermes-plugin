"""Tests for plugin patching Hermes webhook delivery to Myah."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter

from myah_hermes_plugin.myah_platform import register


class RecordingContext:
    def __init__(self) -> None:
        self.tools = []
        self.platforms = []
        self.hooks = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


class FakeMyahAdapter:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="sent-1")


@pytest.mark.asyncio
async def test_register_patches_webhook_cross_platform_delivery_for_myah():
    register(RecordingContext())
    webhook = WebhookAdapter(PlatformConfig(enabled=True, extra={}))
    myah = FakeMyahAdapter()
    webhook.gateway_runner = SimpleNamespace(adapters={"myah": myah}, config=SimpleNamespace(get_home_channel=lambda _p: None))

    result = await webhook._deliver_cross_platform(
        "myah",
        "Drafted reply",
        {"deliver_extra": {"chat_id": "webhook:reflex-rx-1:myah", "reflex_id": "rx-1"}},
    )

    assert result.success is True
    assert myah.calls == [("webhook:reflex-rx-1:myah", "Drafted reply", {"reflex_id": "rx-1"})]
