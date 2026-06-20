"""Tests for the plugin-owned myah_webhook agent tool."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from myah_hermes_plugin.myah_platform import register
from myah_hermes_plugin.myah_tools import webhook_tool


class RecordingContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.platforms: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_platform(self, **kwargs: Any) -> None:
        self.platforms.append(kwargs)

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))


def test_register_exposes_myah_webhook_tool(monkeypatch):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "https://app.myah.test")
    monkeypatch.setenv("MYAH_PLATFORM_BEARER", "bearer-token")
    ctx = RecordingContext()

    register(ctx)

    tools = {tool["name"]: tool for tool in ctx.tools}
    assert "myah_webhook" in tools
    assert tools["myah_webhook"]["toolset"] == "hermes-myah"
    assert callable(tools["myah_webhook"]["handler"])


def test_list_triggers_calls_platform_api(monkeypatch):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "https://app.myah.test")
    monkeypatch.setenv("MYAH_PLATFORM_BEARER", "bearer-token")
    calls = []

    def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((method, path, kwargs))
        return {
            "status": 200,
            "body": [
                {
                    "toolkit_slug": "gmail",
                    "trigger_slug": "GMAIL_NEW_EMAIL",
                    "connected_account_id": "ca-1",
                    "label": "New email",
                }
            ],
        }

    with patch.object(webhook_tool, "platform_request", side_effect=fake_request):
        body = json.loads(webhook_tool.handle({"action": "list_triggers"}))

    assert body["ok"] is True
    assert body["triggers"][0]["trigger_slug"] == "GMAIL_NEW_EMAIL"
    assert calls[0][0] == "GET"
    assert calls[0][1] == "/api/v1/integrations/triggers"


def test_create_calls_from_trigger_endpoint_and_returns_summary(monkeypatch):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "https://app.myah.test")
    monkeypatch.setenv("MYAH_PLATFORM_BEARER", "bearer-token")
    calls = []

    def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((method, path, kwargs))
        assert kwargs["json_body"]["connected_account_id"] == "ca-1"
        return {
            "status": 200,
            "body": {
                "id": "rx-1",
                "route_name": "reflex-rx-1",
                "name": "Gmail autoreply",
            },
        }

    with patch.object(webhook_tool, "platform_request", side_effect=fake_request):
        body = json.loads(
            webhook_tool.handle(
                {
                    "action": "create",
                    "name": "Gmail autoreply",
                    "prompt": "Draft a reply",
                    "profile_id": "default",
                    "connected_account_id": "ca-1",
                    "trigger_slug": "GMAIL_NEW_EMAIL",
                    "trigger_config": {"label": "inbox"},
                    "model": "anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                }
            )
        )

    assert body["ok"] is True
    assert body["reflex_id"] == "rx-1"
    assert body["route_name"] == "reflex-rx-1"
    assert "Gmail autoreply" in body["summary"]
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/v1/integrations/reflexes/from-trigger"


def test_pause_maps_to_reflex_patch(monkeypatch):
    calls = []

    def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((method, path, kwargs))
        return {"status": 200, "body": {"id": "rx-1", "enabled": False}}

    with patch.object(webhook_tool, "platform_request", side_effect=fake_request):
        body = json.loads(webhook_tool.handle({"action": "pause", "reflex_id": "rx-1"}))

    assert body["ok"] is True
    assert calls == [("PATCH", "/api/v1/reflexes/rx-1", {"json_body": {"enabled": False}})]
