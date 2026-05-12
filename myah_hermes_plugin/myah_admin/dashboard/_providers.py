"""Provider catalog + credential management routes for the myah-admin plugin.

Ported from ``gateway/platforms/myah_management.py`` (the legacy aiohttp
``/myah/api/providers/*`` surface) into FastAPI on the dashboard plugin
process. Every contract field — ``v1_visible``, ``write_type``,
``validation``, ``custom_provider`` — is preserved verbatim because the
Myah onboarding frontend depends on the exact shape.

Source line ranges (``agent/hermes/gateway/platforms/myah_management.py``):

* ``_build_catalog``                  → 1568-1608
* ``_validate_api_key``               → 1648-1693  (converted to httpx)
* ``handle_list_providers``           → 1611-1620
* ``handle_provider_models``          → 1623-1639
* ``handle_connect_credential``       → 1697-1768
* ``handle_delete_credential``        → 1771-1799
* ``handle_delete_all_credentials``   → 1802-1821

The catalog-build helper and validation helper are inlined into this
module so the plugin is self-contained (the plan calls for deleting
``myah_management.py`` once Phase 2 lands).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

# Phase 4e: this module is now a proper package member, so a clean relative
# import works in every load context. The ``/opt/myah/plugins/myah-admin/
# dashboard/plugin_api.py`` shim materialized by ``myah-hermes-plugin install``
# imports the real router from the pip package, so the dashboard loader's
# ``spec_from_file_location`` path never touches this file directly.
from ._common import require_session_token

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic request bodies ─────────────────────────────────────────────────


class ConnectCredentialBody(BaseModel):
    api_key: str
    label: str = "primary"


# ── Catalog helpers (ported verbatim from myah_management.py) ───────────────


def _build_model_entry(provider: str, model_id: str) -> dict:
    """Catalog entry for one model, enriched with capabilities when available.

    Source: ``myah_management.py:1538-1561``. Capabilities are omitted on any
    failure — the catalog must never fail because one capability lookup did.
    """
    from agent.models_dev import get_model_capabilities

    entry: dict = {"id": model_id, "name": model_id}
    try:
        caps = get_model_capabilities(provider, model_id)
        if caps is not None:
            entry["capabilities"] = {
                "supports_tools": caps.supports_tools,
                "supports_vision": caps.supports_vision,
                "supports_reasoning": caps.supports_reasoning,
                "context_window": caps.context_window,
                "max_output_tokens": caps.max_output_tokens,
                "model_family": caps.model_family,
            }
    except Exception as exc:
        logger.warning(f"Failed to fetch capabilities for {provider}/{model_id}: {exc}")

    return entry


async def _build_catalog() -> dict:
    """Build the full provider catalog from upstream registries + MYAH_OVERRIDES.

    Source: ``myah_management.py:1568-1608``.
    """
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.models import CANONICAL_PROVIDERS, _PROVIDER_MODELS
    from myah_hermes_plugin.myah_admin.myah_overrides import MYAH_OVERRIDES
    from hermes_cli.providers import HERMES_OVERLAYS, normalize_provider

    out: dict[str, dict] = {}

    # Pass 1: entries from upstream CANONICAL_PROVIDERS
    for entry in CANONICAL_PROVIDERS:
        slug = entry.slug
        cfg = PROVIDER_REGISTRY.get(slug)
        # HERMES_OVERLAYS is keyed on models.dev slugs which may differ
        # from CANONICAL_PROVIDERS slugs. Normalise before lookup.
        overlay = HERMES_OVERLAYS.get(normalize_provider(slug)) or HERMES_OVERLAYS.get(slug)
        _ = overlay  # parity with legacy; reserved for future overlay-derived fields

        catalog_entry = {
            "id": slug,
            "display_name": entry.label,
            "description": entry.tui_desc,
            "auth_type": cfg.auth_type if cfg else "api_key",
            "env_var": (cfg.api_key_env_vars[0] if cfg and cfg.api_key_env_vars else None),
            "inference_base_url": cfg.inference_base_url if cfg else "",
            "curated_models": [_build_model_entry(slug, m) for m in _PROVIDER_MODELS.get(slug, [])],
            "v1_visible": False,
            "write_type": "env_var",
        }

        override = MYAH_OVERRIDES.get(slug, {})
        catalog_entry.update(override)
        out[slug] = catalog_entry

    # Pass 2: synthetic entries from MYAH_OVERRIDES not in CANONICAL_PROVIDERS
    for slug, override in MYAH_OVERRIDES.items():
        if slug in out:
            continue
        out[slug] = {"id": slug, "curated_models": [], "v1_visible": False, **override}

    return out


async def _validate_api_key(catalog_entry: dict, api_key: str) -> tuple[bool, str]:
    """Validate the API key against the provider's validation URL.

    Source: ``myah_management.py:1648-1693``. Converted from ``aiohttp`` to
    ``httpx.AsyncClient`` for consistency with the rest of the plugin.

    Returns ``(accepted, reason)``. Only HTTP 401/403 (explicit auth denial)
    returns ``(False, ...)``. Timeouts, 429, 5xx, and network errors are
    accepted optimistically so transient infra issues do not permanently
    block valid credentials. Native Hermes CLI accepts keys with no
    validation at all; this adds early-feedback validation for the Myah
    onboarding UI without blocking on transient failures.
    """
    validation = catalog_entry.get("validation") or {}
    url = validation.get("url")
    if not url:
        return True, "no validation URL configured"

    headers: dict[str, str] = {}
    params: dict[str, str] | None = None
    method = validation.get("method", "GET")
    auth = validation.get("auth", "bearer")
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth == "x-api-key":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif auth == "query":
        params = {"key": api_key}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.request(method, url, headers=headers, params=params)
            if r.status_code in (401, 403):
                return False, f"auth denied by provider (HTTP {r.status_code})"
            if r.status_code < 400:
                return True, "validated"
            # 429 / 5xx / other — cannot confirm auth; accept optimistically
            logger.warning(
                f"[myah] validation endpoint returned {r.status_code} for {url}; "
                "optimistic accept"
            )
            return True, f"optimistic accept (validation HTTP {r.status_code})"
    except httpx.TimeoutException:
        logger.warning(f"[myah] validation endpoint timed out for {url}; optimistic accept")
        return True, "optimistic accept (validation timeout)"
    except Exception as exc:
        logger.warning(
            f"[myah] validation endpoint error for {url}: {exc}; optimistic accept"
        )
        return True, f"optimistic accept (validation error: {exc})"


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/providers", dependencies=[Depends(require_session_token)])
async def list_providers(visible: str = "all") -> dict:
    """Return the merged provider catalog.

    Query: ``?visible=v1`` filters to ``v1_visible=True`` entries only;
    ``?visible=all`` (default) returns the full catalog. The frontend
    onboarding flow sends ``visible=v1`` explicitly, so the legacy default
    of ``all`` is preserved here.

    Source: ``myah_management.py:1611-1620``.
    """
    out = await _build_catalog()
    if visible == "v1":
        out = {k: v for k, v in out.items() if v.get("v1_visible")}
    return out


@router.get(
    "/providers/{provider_id}/models",
    dependencies=[Depends(require_session_token)],
)
async def provider_models(provider_id: str) -> list[dict]:
    """Live model list for one provider.

    Source: ``myah_management.py:1623-1639``.
    """
    from hermes_cli.models import provider_model_ids

    catalog = await _build_catalog()
    if provider_id not in catalog:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown provider: {provider_id}",
        )

    try:
        loop = asyncio.get_event_loop()
        ids = await loop.run_in_executor(None, provider_model_ids, provider_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return [{"id": m, "name": m} for m in ids]


@router.post(
    "/providers/{provider_id}/credential",
    dependencies=[Depends(require_session_token)],
)
async def connect_credential(provider_id: str, body: ConnectCredentialBody) -> dict:
    """Add or replace a credential. Routes on catalog ``write_type``.

    Writes occur in three places (matching legacy):
    * ``~/.hermes/.env`` via ``save_env_value`` for the env-var key
    * ``~/.hermes/config.yaml`` providers block for ``custom_provider`` writes
    * The credential pool via ``PooledCredential``

    Source: ``myah_management.py:1697-1768``.
    """
    from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential, load_pool
    from hermes_cli.config import load_config, save_config, save_env_value

    api_key = (body.api_key or "").strip()
    label = (body.label or "primary").strip()

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="api_key required"
        )

    catalog = await _build_catalog()
    entry = catalog.get(provider_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown provider: {provider_id}",
        )

    accepted, reason = await _validate_api_key(entry, api_key)
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"validation failed for {provider_id}: {reason}",
        )
    if "optimistic" in reason:
        logger.info(f"[myah] credential connect {provider_id}: {reason}")

    write_type = entry.get("write_type", "env_var")
    key_last_four = api_key[-4:] if len(api_key) >= 4 else "****"
    entry_id = f"myah-{uuid.uuid4().hex[:12]}"

    if write_type in ("env_var", "custom_provider"):
        env_var = entry.get("env_var")
        if env_var:
            save_env_value(env_var, api_key)

        if write_type == "custom_provider":
            cp = entry.get("custom_provider", {})
            cfg = load_config()
            providers_block = cfg.setdefault("providers", {})
            providers_block[cp["slug"]] = {
                "base_url": cp["base_url"],
                "key_env": env_var,
                "api_mode": cp.get("api_mode", "openai_chat"),
            }
            save_config(cfg)

        pool = load_pool(provider_id)
        cred = PooledCredential(
            provider=provider_id,
            id=entry_id,
            label=label,
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source="myah:api",
            access_token=api_key,
        )
        pool.add_entry(cred)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"write_type {write_type!r} not supported here; "
                "use the OAuth endpoints"
            ),
        )

    return {
        "entry_id": entry_id,
        "key_last_four": key_last_four,
        "is_valid": True,
    }


@router.delete(
    "/providers/{provider_id}/credential/{entry_id}",
    dependencies=[Depends(require_session_token)],
)
async def delete_credential(provider_id: str, entry_id: str) -> dict:
    """Remove one credential from the pool by entry_id.

    If the pool is empty after removal, also clear the env var.

    Source: ``myah_management.py:1771-1799``.
    """
    from hermes_cli.config import remove_env_value

    from agent.credential_pool import load_pool

    pool = load_pool(provider_id)
    removed = False
    for idx, cred in enumerate(pool.entries(), start=1):
        if cred.id == entry_id:
            pool.remove_index(idx)
            removed = True
            break

    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="entry not found"
        )

    # If pool now empty, clear the env var
    pool2 = load_pool(provider_id)
    if not pool2.entries():
        catalog = await _build_catalog()
        env_var = (catalog.get(provider_id) or {}).get("env_var")
        if env_var:
            remove_env_value(env_var)

    return {"ok": True}


@router.delete(
    "/providers/{provider_id}",
    dependencies=[Depends(require_session_token)],
)
async def delete_all_credentials(provider_id: str) -> dict:
    """Remove ALL credentials for a provider.

    Clears auth.json (via ``clear_provider_auth``) and the env var.

    Source: ``myah_management.py:1802-1821``.
    """
    from hermes_cli.auth import clear_provider_auth
    from hermes_cli.config import remove_env_value

    try:
        clear_provider_auth(provider_id)
    except Exception:
        pass

    catalog = await _build_catalog()
    env_var = (catalog.get(provider_id) or {}).get("env_var")
    if env_var:
        remove_env_value(env_var)

    return {"ok": True}


# ── OAuth device-flow loopback handlers (Phase 7.7 plugin migration) ────────
# See docs/superpowers/specs/2026-05-12-plugin-dashboard-migration-design.md.

from ._proxy import proxy_to_native  # noqa: E402


class _OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


@router.post(
    '/providers/oauth/{provider_id}/start',
    dependencies=[Depends(require_session_token)],
)
async def oauth_start(provider_id: str) -> dict:
    """Plugin-namespace mirror of POST /api/providers/oauth/{id}/start."""
    return await proxy_to_native('POST', f'/api/providers/oauth/{provider_id}/start')


@router.get(
    '/providers/oauth/{provider_id}/poll/{session_id}',
    dependencies=[Depends(require_session_token)],
)
async def oauth_poll(provider_id: str, session_id: str) -> dict:
    """Plugin-namespace mirror of GET /api/providers/oauth/{id}/poll/{session_id}."""
    return await proxy_to_native(
        'GET', f'/api/providers/oauth/{provider_id}/poll/{session_id}',
    )


@router.post(
    '/providers/oauth/{provider_id}/submit',
    dependencies=[Depends(require_session_token)],
)
async def oauth_submit(provider_id: str, body: _OAuthSubmitBody) -> dict:
    """Plugin-namespace mirror of POST /api/providers/oauth/{id}/submit (PKCE)."""
    return await proxy_to_native(
        'POST',
        f'/api/providers/oauth/{provider_id}/submit',
        json_body=body.model_dump(),
    )


@router.delete(
    '/providers/oauth/sessions/{session_id}',
    dependencies=[Depends(require_session_token)],
)
async def oauth_cancel(session_id: str) -> dict:
    """Plugin-namespace mirror of DELETE /api/providers/oauth/sessions/{id}."""
    return await proxy_to_native('DELETE', f'/api/providers/oauth/sessions/{session_id}')
