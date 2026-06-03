from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter
from myah_hermes_plugin.runtime_extensions.webhook_metadata import install


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id="recorded")


@pytest.mark.asyncio
async def test_webhook_cross_platform_delivery_forwards_source_metadata():
    install()
    adapter = WebhookAdapter(SimpleNamespace(enabled=True, extra={}))
    target_adapter = RecordingAdapter()
    adapter.gateway_runner = SimpleNamespace(
        adapters={Platform("myah"): target_adapter},
        config=SimpleNamespace(get_home_channel=lambda platform: None),
    )

    delivery = {
        "deliver": "myah",
        "deliver_extra": {"chat_id": "myah-chat"},
        "payload": {
            "action": "opened",
            "repository": {"full_name": "T3-Venture-Labs-Limited/myah-hosted"},
            "pull_request": {"number": 42, "title": "Visible webhook runs"},
        },
        "delivery_id": "github-delivery-42",
        "route_name": "myah-hosted-pr-review",
        "event_type": "pull_request",
    }

    result = await adapter._deliver_cross_platform("myah", "final review", delivery)

    assert result.success is True
    assert target_adapter.calls == [
        {
            "chat_id": "myah-chat",
            "content": "final review",
            "metadata": {
                "source_platform": "webhook",
                "webhook_payload": delivery["payload"],
                "webhook_deliver_extra": delivery["deliver_extra"],
                "webhook_delivery": {
                    "delivery_id": "github-delivery-42",
                    "route_name": "myah-hosted-pr-review",
                    "event_type": "pull_request",
                },
            },
        }
    ]
