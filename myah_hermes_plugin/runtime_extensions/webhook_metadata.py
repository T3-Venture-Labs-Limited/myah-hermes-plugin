"""Forward webhook source metadata through cross-platform deliveries.

Vanilla Hermes' webhook adapter stores the source payload while it waits for
the agent's final response, but cross-platform delivery only forwards a
Telegram-style ``thread_id`` metadata dict to the target adapter. Myah needs
the webhook route, delivery id, payload, and rendered delivery extras so its
adapter can persist the completed background run through the platform's
external-run endpoint.

This module patches the runtime method rather than editing upstream Hermes
core files. It is intentionally generic: non-Myah targets simply receive extra
metadata keys they can ignore.
"""

from __future__ import annotations

from typing import Any, Dict

_INSTALLED = False
_ORIGINAL_SEND = None
_ORIGINAL_DELIVER_CROSS_PLATFORM = None


def _event_type_from_payload(payload: Dict[str, Any]) -> str:
    if payload.get("pull_request"):
        return "pull_request"
    return str(payload.get("event_type") or payload.get("type") or "unknown")


def install() -> None:
    """Install idempotent webhook metadata forwarding patches."""
    global _INSTALLED, _ORIGINAL_SEND, _ORIGINAL_DELIVER_CROSS_PLATFORM
    if _INSTALLED:
        return

    try:
        from gateway.platforms.webhook import WebhookAdapter
    except Exception:
        return

    _ORIGINAL_SEND = WebhookAdapter.send
    _ORIGINAL_DELIVER_CROSS_PLATFORM = WebhookAdapter._deliver_cross_platform

    async def send_with_delivery_context(self, chat_id, content, reply_to=None, metadata=None):
        delivery = getattr(self, "_delivery_info", {}).get(chat_id)
        if isinstance(delivery, dict) and chat_id.startswith("webhook:"):
            parts = chat_id.split(":", 2)
            if len(parts) == 3:
                delivery.setdefault("route_name", parts[1])
                delivery.setdefault("delivery_id", parts[2])
            payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
            delivery.setdefault("event_type", _event_type_from_payload(payload))
        return await _ORIGINAL_SEND(self, chat_id, content, reply_to=reply_to, metadata=metadata)

    async def deliver_cross_platform_with_metadata(self, platform_name, content, delivery):
        if not getattr(self, "gateway_runner", None):
            return await _ORIGINAL_DELIVER_CROSS_PLATFORM(self, platform_name, content, delivery)

        try:
            from gateway.config import Platform
            from gateway.platforms.base import SendResult
        except Exception:
            return await _ORIGINAL_DELIVER_CROSS_PLATFORM(self, platform_name, content, delivery)

        try:
            target_platform = Platform(platform_name)
        except ValueError:
            return SendResult(success=False, error=f"Unknown platform: {platform_name}")

        adapter = self.gateway_runner.adapters.get(target_platform)
        if not adapter:
            return SendResult(success=False, error=f"Platform {platform_name} not connected")

        extra = delivery.get("deliver_extra", {}) if isinstance(delivery, dict) else {}
        chat_id = extra.get("chat_id", "")
        if not chat_id:
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if home:
                chat_id = home.chat_id
            else:
                return SendResult(success=False, error=f"No chat_id or home channel for {platform_name}")

        metadata = {
            "source_platform": "webhook",
            "webhook_payload": delivery.get("payload", {}),
            "webhook_deliver_extra": extra,
            "webhook_delivery": {
                "delivery_id": delivery.get("delivery_id", ""),
                "route_name": delivery.get("route_name", ""),
                "event_type": delivery.get("event_type", ""),
            },
        }
        thread_id = extra.get("message_thread_id") or extra.get("thread_id")
        if thread_id:
            metadata["thread_id"] = thread_id

        return await adapter.send(chat_id, content, metadata=metadata)

    WebhookAdapter.send = send_with_delivery_context
    WebhookAdapter._deliver_cross_platform = deliver_cross_platform_with_metadata
    _INSTALLED = True
