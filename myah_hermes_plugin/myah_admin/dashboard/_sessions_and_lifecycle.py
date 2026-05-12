"""Session + lifecycle handlers for the myah-admin dashboard plugin.

Mounted by ``plugin_api.py`` as a sub-router. Covers the session/title/append
+ session-model + global-model handlers ported from the legacy
``gateway/platforms/myah_management.py`` (lines 282-459 and 1032-1106).

Two flavours of handler live here side-by-side:

1. **File-system handlers** (title, append, get/put global model) — write
   directly to ``SessionDB`` or shell out to ``hermes config set``.

2. **Runner-coupled handlers** (get/put session model) — proxy to the
   gateway's runtime-control surface at
   ``http://localhost:{API_SERVER_PORT}/myah/v1/admin/sessions/{key}/override``
   via :data:`gateway_client`. The gateway is the only place that holds
   live :class:`GatewayRunner` state, so anything that needs to read or
   write a session-scoped override has to round-trip there.

Why both groups in one file: ``GET/PUT /config/model`` and
``GET/PUT /sessions/{key}/model`` are the *same product feature* viewed
through two scopes (global vs per-session). Splitting them across files
would make it harder to keep their contracts and tests in sync.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

# Phase 4e: clean relative import inside the pip-package layout. The
# ``/opt/myah/plugins/myah-admin/dashboard/plugin_api.py`` shim materialized
# by ``myah-hermes-plugin install`` imports the real router from the pip
# package, so the dashboard loader's ``spec_from_file_location`` path never
# touches this file directly.
from ._common import gateway_client, hermes_home, require_session_token

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_session_token)])


# ── Pydantic bodies ─────────────────────────────────────────────────────────


class SetTitleBody(BaseModel):
    """Body for ``POST /sessions/{id}/title``."""

    title: str = Field(default="")


_ALLOWED_ROLES = frozenset({"user", "assistant", "tool"})


class AppendMessageBody(BaseModel):
    """Body for ``POST /sessions/{id}/append``.

    Mirrors the legacy contract: any role accepted at the ORM level is OK,
    but we restrict to the roles the cron-output pipeline actually uses
    (``user``/``assistant``/``tool``) to keep the surface minimal.
    """

    role: str = Field(default="assistant")
    content: str = Field(default="")


class SessionModelBody(BaseModel):
    """Body for ``PUT /sessions/{key}/model``."""

    model: str
    provider: str | None = Field(default=None)
    base_url: str | None = Field(default=None)


class GlobalModelBody(BaseModel):
    """Body for ``PUT /config/model``."""

    model: str
    provider: str | None = Field(default=None)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _async_subprocess(
    *cmd: str, timeout: float = 10.0
) -> tuple[int, str, str]:
    """Run a subprocess without blocking the event loop.

    Returns ``(returncode, stdout, stderr)``. Mirrors the helper in
    ``gateway/platforms/myah_management.py:77`` — duplicated here so the
    plugin doesn't import gateway internals.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


def _read_config_yaml() -> dict[str, Any]:
    """Read ``$HERMES_HOME/config.yaml`` or return an empty dict."""
    config_path = os.path.join(hermes_home(), "config.yaml")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # pragma: no cover — defensive
        logger.exception("[myah-admin] failed to read config.yaml")
        return {}


# ── Title / append (direct SessionDB writes) ────────────────────────────────


@router.post("/sessions/{session_id}/title")
async def set_session_title(
    session_id: str = Path(..., min_length=1),
    body: SetTitleBody = ...,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Set or clear a session's title (legacy: handle_set_session_title)."""
    from hermes_state import SessionDB

    title = body.title or ""
    try:
        db = SessionDB()
        try:
            found = db.set_session_title(session_id, title)
        finally:
            # SessionDB doesn't provide a context manager; rely on its own
            # internal connection lifecycle. We don't close here because
            # the underlying connection is reused across calls.
            pass
    except ValueError as exc:
        # Title-too-long / duplicate-title — the legacy returns 422.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("[myah-admin] set_session_title failed for %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to set session title: {exc}",
        ) from exc

    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return {"session_id": session_id, "title": title}


@router.post("/sessions/{session_id}/append")
async def append_session_message(
    session_id: str = Path(..., min_length=1),
    body: AppendMessageBody = ...,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Append a message to a session — used by cron output delivery.

    Legacy: handle_append_session_message (myah_management.py:1055-1106).
    Creates the session row if it doesn't exist (legacy behaviour) so cron
    jobs that target a session that hasn't received a chat message yet
    still write through cleanly.
    """
    from hermes_state import SessionDB

    role = (body.role or "assistant").strip()
    if role not in _ALLOWED_ROLES:
        # Legacy accepts any role string — but we tighten the surface.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"role must be one of {sorted(_ALLOWED_ROLES)}",
        )

    content = body.content or ""
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="content is required",
        )

    try:
        db = SessionDB()
        db.ensure_session(session_id, source="myah")
        msg_id = db.append_message(session_id, role=role, content=content)
    except Exception as exc:
        logger.exception(
            "[myah-admin] append_session_message failed for %s", session_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to append message: {exc}",
        ) from exc

    return {
        "session_id": session_id,
        "message_id": msg_id,
        "role": role,
    }


# ── Session-scoped model overrides (runner-coupled, proxied) ───────────────


@router.get("/sessions/{session_key}/model")
async def get_session_model(
    session_key: str = Path(..., min_length=1),
) -> dict[str, Any]:
    """Read the active model override for a session.

    Proxies to ``GET /myah/v1/admin/sessions/{key}/override`` on the
    gateway. The gateway returns ``{"override": {...}}``; we unpack to
    match the legacy ``{model, provider, base_url?}`` contract from
    ``myah_management.py:323-336``.
    """
    body = await gateway_client.request_or_raise(
        "GET", f"/sessions/{session_key}/override"
    )
    override = (body or {}).get("override") if isinstance(body, dict) else None
    override = override or {}
    payload: dict[str, Any] = {
        "model": override.get("model", ""),
        "provider": override.get("provider", ""),
    }
    base_url = override.get("base_url")
    if base_url:
        payload["base_url"] = base_url
    return payload


@router.put("/sessions/{session_key}/model")
async def put_session_model(
    session_key: str = Path(..., min_length=1),
    body: SessionModelBody = ...,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Set a session-scoped model override.

    1. Validate model+provider via :func:`switch_model` (executor-bound;
       it makes network calls).
    2. PUT the resulting override to the gateway. The gateway-side
       handler evicts the cached agent, which triggers the memory
       provider's ``shutdown`` on the next message — replacing the
       legacy in-process ``shutdown_memory_provider`` call (which lived
       in ``myah_management.py:418-431`` and depended on the gateway
       runner being available in-process).
    """
    raw_input = (body.model or "").strip()
    if not raw_input:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="model is required",
        )

    explicit_provider = (body.provider or "").strip()

    # Build the switch_model() context from config.yaml, layering any
    # existing session override on top so unset fields survive a
    # provider change. Mirrors myah_management.py:367-389.
    cfg = _read_config_yaml()
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    current_model = model_cfg.get("default") or (
        cfg.get("model") if isinstance(cfg.get("model"), str) else ""
    )
    current_provider = model_cfg.get("provider", "") or "openrouter"
    current_base_url = model_cfg.get("base_url", "") or ""
    user_providers = cfg.get("providers")
    custom_providers = cfg.get("custom_providers")

    # Layer existing override (read via gateway, same contract as GET above).
    try:
        existing_body = await gateway_client.request_or_raise(
            "GET", f"/sessions/{session_key}/override"
        )
        existing = (
            (existing_body or {}).get("override")
            if isinstance(existing_body, dict)
            else None
        ) or {}
        if existing:
            current_model = existing.get("model", current_model)
            current_provider = existing.get("provider", current_provider)
            current_base_url = existing.get("base_url", current_base_url)
    except HTTPException:
        # If the gateway is unreachable we still try to validate; the
        # PUT below will surface the gateway error to the caller.
        pass

    from hermes_cli.model_switch import switch_model

    def _run_switch_model():
        return switch_model(
            raw_input=raw_input,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key="",
            is_global=False,
            explicit_provider=explicit_provider,
            user_providers=user_providers,
            custom_providers=custom_providers,
        )

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, _run_switch_model
        )
    except Exception as exc:
        logger.exception(
            "[myah-admin] switch_model failed for session %s", session_key
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"switch_model failed: {exc}",
        ) from exc

    if not getattr(result, "success", False):
        error_msg = getattr(result, "error_message", "") or "Model not recognized"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # Hand the validated override to the gateway. It will:
    #   1. Persist via runner.set_session_override(session_key, ...)
    #   2. Evict the cached agent (set_session_override evicts internally)
    # which causes the next message to rebuild the agent — the same
    # rebuild sequence that triggers shutdown_memory_provider on the old
    # instance. This replaces the in-process teardown the legacy did at
    # myah_management.py:418-431.
    override_payload = {
        "model": result.new_model,
        "provider": result.target_provider,
        "api_key": getattr(result, "api_key", "") or "",
        "base_url": getattr(result, "base_url", "") or "",
        "api_mode": getattr(result, "api_mode", "") or "",
    }
    await gateway_client.request_or_raise(
        "PUT",
        f"/sessions/{session_key}/override",
        json_body=override_payload,
    )

    return {
        "model": result.new_model,
        "provider": result.target_provider,
        "provider_label": getattr(result, "provider_label", "") or "",
        "warning": getattr(result, "warning_message", None) or None,
    }


# ── Global model setter (file-system + global cache evict) ─────────────────


@router.get("/config/model")
async def get_global_model() -> dict[str, Any]:
    """Read the model from config.yaml (legacy: handle_get_model)."""
    cfg = _read_config_yaml()
    model = cfg.get("model", "")
    if isinstance(model, dict):
        # Newer config layout: {model: {default, provider, ...}}.
        # Legacy returned the dict as-is, so callers see either string
        # or dict — we mirror that contract.
        return {"model": model}
    return {"model": model or ""}


@router.put("/config/model")
async def put_global_model(
    body: GlobalModelBody = ...,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Update the global model via ``hermes config set``.

    Legacy contract (myah_management.py:291-321) only writes ``model``;
    we additionally accept an optional ``provider`` so callers can
    change provider in one call. After the write we ask the gateway to
    evict every cached agent so the next message everywhere picks up
    the new model.
    """
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="model is required",
        )

    # Validate via switch_model() with is_global=True so we surface
    # bad-model / bad-provider errors before mutating disk.
    cfg = _read_config_yaml()
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    current_model = model_cfg.get("default") or (
        cfg.get("model") if isinstance(cfg.get("model"), str) else ""
    )
    current_provider = model_cfg.get("provider", "") or "openrouter"
    current_base_url = model_cfg.get("base_url", "") or ""
    explicit_provider = (body.provider or "").strip()

    from hermes_cli.model_switch import switch_model

    def _run_switch_model():
        return switch_model(
            raw_input=model,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key="",
            is_global=True,
            explicit_provider=explicit_provider,
            user_providers=cfg.get("providers"),
            custom_providers=cfg.get("custom_providers"),
        )

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, _run_switch_model
        )
    except Exception as exc:
        logger.exception("[myah-admin] global switch_model failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"switch_model failed: {exc}",
        ) from exc

    if not getattr(result, "success", False):
        error_msg = getattr(result, "error_message", "") or "Model not recognized"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # Persist via ``hermes config set``. Legacy used the simple
    # ``hermes config set model <value>`` form; we keep that and add a
    # second call for the provider when one was supplied (and validated).
    rc, _, stderr = await _async_subprocess(
        "hermes", "config", "set", "model", result.new_model, timeout=10.0
    )
    if rc != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"hermes config set failed: {stderr.strip()}",
        )

    if explicit_provider:
        rc2, _, stderr2 = await _async_subprocess(
            "hermes",
            "config",
            "set",
            "model.provider",
            result.target_provider,
            timeout=10.0,
        )
        if rc2 != 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"hermes config set provider failed: {stderr2.strip()}",
            )

    # Global model change → evict every cached agent so next message
    # everywhere rebuilds with the new model.
    await gateway_client.request_or_raise("POST", "/cache/evict-all")

    return {
        "model": result.new_model,
        "provider": result.target_provider,
        "warning": getattr(result, "warning_message", None) or None,
    }


# ── Sessions list + messages (Phase 7.7 plugin migration — loopback) ──────
# See docs/superpowers/specs/2026-05-12-plugin-dashboard-migration-design.md.

from ._proxy import proxy_to_native  # noqa: E402


@router.get("/sessions")
async def list_sessions(limit: int = 50, offset: int = 0) -> dict:
    """Plugin-namespace mirror of GET /api/sessions.

    Lists sessions from upstream's SessionDB. Upstream returns
    ``{sessions, total, limit, offset}`` — we pass it through unchanged.
    """
    return await proxy_to_native(
        "GET", "/api/sessions", params={"limit": limit, "offset": offset},
    )


@router.get("/sessions/{session_id}/messages")
async def get_session_messages_proxy(session_id: str) -> dict:
    """Plugin-namespace mirror of GET /api/sessions/{id}/messages.

    Upstream's resolve_session_id + SessionDB.get_messages stay the source
    of truth — we never re-implement. Upstream returns
    ``{session_id, messages}``.
    """
    return await proxy_to_native(
        "GET", f"/api/sessions/{session_id}/messages",
    )
