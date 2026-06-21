"""Agent-facing Reflex webhook management tool for Myah."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _platform_base_url() -> str:
    return (os.environ.get("MYAH_PLATFORM_BASE_URL") or "").strip().rstrip("/")


def _platform_bearer() -> str:
    return (
        os.environ.get("MYAH_PLATFORM_BEARER")
        or os.environ.get("MYAH_AGENT_BEARER_TOKEN")
        or os.environ.get("MYAH_AGENT_TOKEN")
        or ""
    ).strip()


def platform_request(method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = _platform_base_url()
    if not base_url:
        return {"status": 503, "body": {"detail": "MYAH_PLATFORM_BASE_URL is not configured"}}

    data = None
    headers = {"accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["content-type"] = "application/json"
    bearer = _platform_bearer()
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    user_id = (os.environ.get("MYAH_USER_ID") or "").strip()
    if user_id:
        headers["x-myah-user-id"] = user_id

    req = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            try:
                body: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = raw
            return {"status": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = raw
        return {"status": exc.code, "body": body}
    except OSError as exc:
        return {"status": 503, "body": {"detail": str(exc)}}


def _error_response(action: str, response: dict[str, Any]) -> str:
    body = response.get("body")
    detail = body.get("detail") if isinstance(body, dict) else body
    return json.dumps(
        {"ok": False, "action": action, "error": str(detail or "Platform request failed")[:200]},
        ensure_ascii=False,
    )


def _ok(action: str, **fields: Any) -> str:
    return json.dumps({"ok": True, "action": action, **fields}, ensure_ascii=False)


def _require_reflex_id(args: dict[str, Any]) -> str | None:
    reflex_id = str(args.get("reflex_id") or "").strip()
    return reflex_id or None


def _action_list_triggers(args: dict[str, Any]) -> str:
    response = platform_request("GET", "/api/v1/integrations/triggers")
    if response.get("status") != 200:
        return _error_response("list_triggers", response)
    body = response.get("body")
    if isinstance(body, list):
        triggers = body
    elif isinstance(body, dict):
        triggers = body.get("items") or body.get("triggers", [])
    else:
        triggers = []
    return _ok("list_triggers", triggers=triggers)


def _infer_toolkit_slug(args: dict[str, Any]) -> str | None:
    explicit = str(args.get("toolkit_slug") or "").strip()
    if explicit:
        return explicit
    trigger_slug = str(args.get("trigger_slug") or "").strip()
    if "_" in trigger_slug:
        return trigger_slug.split("_", 1)[0].lower()
    return None


def _action_create(args: dict[str, Any]) -> str:
    payload = {
        "name": args.get("name"),
        "prompt": args.get("prompt"),
        "profile_id": args.get("profile_id") or "default",
        "connected_account_id": args.get("connected_account_id"),
        "trigger_slug": args.get("trigger_slug"),
        "toolkit_slug": _infer_toolkit_slug(args),
        "trigger_config": args.get("trigger_config") or {},
        "model": args.get("model"),
        "provider": args.get("provider"),
    }
    response = platform_request("POST", "/api/v1/integrations/reflexes/from-trigger", json_body=payload)
    if response.get("status") not in (200, 201):
        return _error_response("create", response)
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    reflex_id = body.get("id") or body.get("reflex_id")
    route_name = body.get("route_name") or body.get("hermes_route_name")
    name = body.get("name") or args.get("name") or "Reflex"
    return _ok(
        "create",
        reflex_id=reflex_id,
        route_name=route_name,
        summary=f"Created Reflex '{name}' ({route_name or reflex_id}).",
    )


def _patch_enabled(args: dict[str, Any], enabled: bool, action: str) -> str:
    reflex_id = _require_reflex_id(args)
    if not reflex_id:
        return json.dumps({"ok": False, "action": action, "error": "reflex_id is required"})
    response = platform_request("PATCH", f"/api/v1/reflexes/{reflex_id}", json_body={"enabled": enabled})
    if response.get("status") != 200:
        return _error_response(action, response)
    return _ok(action, reflex_id=reflex_id, reflex=response.get("body"))


def _simple_reflex_action(args: dict[str, Any], action: str, method: str, path_suffix: str = "") -> str:
    reflex_id = _require_reflex_id(args)
    if not reflex_id and action != "list":
        return json.dumps({"ok": False, "action": action, "error": "reflex_id is required"})
    path = "/api/v1/reflexes/" if action == "list" else f"/api/v1/reflexes/{reflex_id}{path_suffix}"
    response = platform_request(method, path)
    if response.get("status") not in (200, 204):
        return _error_response(action, response)
    return _ok(action, reflex_id=reflex_id, result=response.get("body"))


def handle(args: dict[str, Any], **_kwargs: Any) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action == "list_triggers":
        return _action_list_triggers(args)
    if action == "create":
        return _action_create(args)
    if action == "list":
        return _simple_reflex_action(args, action, "GET")
    if action == "pause":
        return _patch_enabled(args, False, action)
    if action == "resume":
        return _patch_enabled(args, True, action)
    if action == "delete":
        return _simple_reflex_action(args, action, "DELETE")
    if action == "test":
        return _simple_reflex_action(args, action, "POST", "/test")
    if action == "runs":
        return _simple_reflex_action(args, action, "GET", "/runs")
    return json.dumps({"ok": False, "action": action, "error": f"Unknown action: {action}"}, ensure_ascii=False)


SCHEMA = {
    "name": "myah_webhook",
    "description": "Create and manage Myah Reflexes backed by Composio triggers and Hermes webhooks.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_triggers", "create", "list", "update", "pause", "resume", "delete", "test", "runs"],
            },
            "reflex_id": {"type": "string"},
            "name": {"type": "string"},
            "prompt": {"type": "string"},
            "profile_id": {"type": "string"},
            "connected_account_id": {"type": "string"},
            "trigger_slug": {"type": "string"},
            "toolkit_slug": {"type": "string"},
            "trigger_config": {"type": "object"},
            "model": {"type": "string"},
            "provider": {"type": "string"},
        },
        "required": ["action"],
    },
}
