"""Tests for Myah admin Reflex webhook route management."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import _webhooks


def _client(monkeypatch) -> TestClient:
    monkeypatch.delenv("HERMES_WEB_SESSION_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(_webhooks.router)
    return TestClient(app)


def test_subscribe_webhook_registers_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    client = _client(monkeypatch)

    resp = client.post(
        "/webhooks/reflex-rx-1",
        json={
            "events": ["gmail.new_email"],
            "prompt": "Draft a reply",
            "secret": "abc123",
            "deliver": "myah",
            "deliver_extra": {"reflex_id": "rx-1", "profile_id": "default"},
        },
    )
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["route_name"] == "reflex-rx-1"
    assert first["url"].endswith("/webhooks/reflex-rx-1")
    assert first["secret"] == "abc123"

    duplicate = client.post(
        "/webhooks/reflex-rx-1",
        json={
            "events": ["gmail.new_email"],
            "prompt": "Draft a reply",
            "secret": "rotated-should-not-win",
            "deliver": "myah",
            "deliver_extra": {"reflex_id": "rx-1", "profile_id": "default"},
        },
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["secret"] == "rotated-should-not-win"

    stored = _webhooks.load_webhook_subscriptions()
    assert stored["reflex-rx-1"]["events"] == ["gmail.new_email"]
    assert stored["reflex-rx-1"]["prompt"] == "Draft a reply"
    assert stored["reflex-rx-1"]["deliver"] == "myah"
    assert stored["reflex-rx-1"]["deliver_extra"]["reflex_id"] == "rx-1"
    assert stored["reflex-rx-1"]["deliver_extra"]["chat_id"] == "webhook:reflex-rx-1:myah"
    assert (tmp_path / "hermes" / "webhook_subscriptions.json").exists()


def test_delete_webhook_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    client = _client(monkeypatch)
    payload = {
        "events": ["gmail.new_email"],
        "prompt": "Draft a reply",
        "secret": "abc123",
        "deliver": "myah",
        "deliver_extra": {"reflex_id": "rx-1"},
    }
    assert client.post("/webhooks/reflex-rx-1", json=payload).status_code == 200

    deleted = client.delete("/webhooks/reflex-rx-1")
    deleted_again = client.delete("/webhooks/reflex-rx-1")

    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "route_name": "reflex-rx-1"}
    assert deleted_again.status_code == 200
    assert _webhooks.load_webhook_subscriptions() == {}


def test_rejects_invalid_route_name(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    client = _client(monkeypatch)
    resp = client.post(
        "/webhooks/../escape",
        json={"events": ["x"], "prompt": "p", "secret": "s", "deliver": "myah"},
    )
    assert resp.status_code in (404, 422)

def test_subscribe_webhook_rejects_blank_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    client = _client(monkeypatch)

    resp = client.post(
        "/webhooks/reflex-rx-1",
        json={
            "events": ["gmail.new_email"],
            "prompt": "Draft a reply",
            "secret": "   ",
            "deliver": "myah",
            "deliver_extra": {"reflex_id": "rx-1"},
        },
    )

    assert resp.status_code == 422
    assert "secret" in resp.json()["detail"]
    assert _webhooks.load_webhook_subscriptions() == {}

def test_subscribe_webhook_upserts_existing_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    client = _client(monkeypatch)

    first = client.post(
        "/webhooks/reflex-rx-1",
        json={
            "events": ["gmail.new_email"],
            "prompt": "Draft a reply",
            "secret": "first-secret",
            "deliver": "myah",
            "deliver_extra": {"reflex_id": "rx-1", "profile_id": "default"},
        },
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/webhooks/reflex-rx-1",
        json={
            "events": ["linear.issue_created"],
            "prompt": "Summarize the issue",
            "secret": "rotated-secret",
            "deliver": "myah",
            "deliver_extra": {"reflex_id": "rx-1", "profile_id": "support"},
        },
    )

    assert second.status_code == 200, second.text
    assert second.json()["secret"] == "rotated-secret"
    stored = _webhooks.load_webhook_subscriptions()["reflex-rx-1"]
    assert stored["events"] == ["linear.issue_created"]
    assert stored["prompt"] == "Summarize the issue"
    assert stored["secret"] == "rotated-secret"
    assert stored["deliver_extra"]["profile_id"] == "support"
    assert stored["deliver_extra"]["chat_id"] == "webhook:reflex-rx-1:myah"
