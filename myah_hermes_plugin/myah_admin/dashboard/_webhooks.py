"""Reflex webhook route management for the myah-admin dashboard plugin."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from hermes_constants import get_hermes_home

from ._common import require_session_token

router = APIRouter()
_ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class WebhookSubscribeBody(BaseModel):
    events: list[str] = Field(default_factory=list)
    prompt: str
    secret: str | None = None
    deliver: str = "myah"
    deliver_extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


def _validate_route_name(route_name: str) -> str:
    if not _ROUTE_RE.match(route_name or "") or ".." in route_name:
        raise HTTPException(status_code=422, detail="Invalid webhook route name")
    return route_name


def _validate_secret(secret: str | None) -> str:
    value = (secret or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="secret is required")
    return value


def _subscriptions_path() -> Path:
    # Match upstream hermes_cli.webhook / gateway.platforms.webhook exactly:
    # the webhook adapter hot-reloads this native file on every POST.
    path = get_hermes_home() / "webhook_subscriptions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_webhook_subscriptions() -> dict[str, dict[str, Any]]:
    path = _subscriptions_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_webhook_subscriptions(subscriptions: dict[str, dict[str, Any]]) -> None:
    path = _subscriptions_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(subscriptions, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _webhook_url(route_name: str) -> str:
    base = "http://agent:8644"
    return f"{base}/webhooks/{route_name}"


@router.post("/webhooks/{route_name}", dependencies=[Depends(require_session_token)])
async def subscribe_webhook(route_name: str, body: WebhookSubscribeBody) -> dict[str, Any]:
    route_name = _validate_route_name(route_name)
    if not body.prompt.strip():
        raise HTTPException(status_code=422, detail="prompt is required")
    if not body.events:
        raise HTTPException(status_code=422, detail="at least one event is required")
    secret = _validate_secret(body.secret)

    subscriptions = load_webhook_subscriptions()
    deliver_extra = dict(body.deliver_extra)
    deliver_extra.setdefault("chat_id", f"webhook:{route_name}:myah")

    record = {
        "route_name": route_name,
        "url": _webhook_url(route_name),
        "events": body.events,
        "prompt": body.prompt,
        "secret": secret,
        "deliver": body.deliver,
        "deliver_extra": deliver_extra,
    }
    subscriptions[route_name] = record
    save_webhook_subscriptions(subscriptions)
    return {"route_name": route_name, "url": record["url"], "secret": record["secret"]}


@router.delete("/webhooks/{route_name}", dependencies=[Depends(require_session_token)])
async def delete_webhook(route_name: str) -> dict[str, Any]:
    route_name = _validate_route_name(route_name)
    subscriptions = load_webhook_subscriptions()
    subscriptions.pop(route_name, None)
    save_webhook_subscriptions(subscriptions)
    return {"ok": True, "route_name": route_name}
