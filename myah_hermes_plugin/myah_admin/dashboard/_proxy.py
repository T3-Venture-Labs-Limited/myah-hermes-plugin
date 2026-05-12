"""Loopback HTTP proxy to the dashboard's native /api/* surface.

The dashboard plugin runs in the same FastAPI process as the dashboard
itself (web_server.py:4186 — app.include_router(router, prefix=...)),
so a localhost HTTP request to /api/<native> hits a real handler in
the same process. We forward the dashboard's own _SESSION_TOKEN as
the auth header so the dashboard's auth middleware (web_server.py:236)
accepts the request.

Why loopback instead of importing handler functions directly:

1. Upstream's normalize/denormalize logic (e.g. _denormalize_config_from_web
   for PUT /api/config) stays the source of truth. We never re-implement.
2. Future upstream changes propagate automatically; the plugin doesn't
   need to track them.
3. Only ONE private symbol is imported (_SESSION_TOKEN) instead of
   ~15 (handler functions, module-level state dicts, helper functions).
4. Each plugin handler becomes ~5 lines — pure routing, no logic.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_DASHBOARD_BASE = 'http://localhost:9119'
_TIMEOUT_SECONDS = 15.0


async def proxy_to_native(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT_SECONDS,
) -> Any:
    """Forward an HTTP request to the dashboard's native /api/* surface.

    The dashboard's _SESSION_TOKEN is imported at request time (not module
    load) so reloads pick up the current value.
    """
    from hermes_cli.web_server import _SESSION_TOKEN

    headers = {'Authorization': f'Bearer {_SESSION_TOKEN}'}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method,
                f'{_DASHBOARD_BASE}{path}',
                json=json_body,
                params=params,
                headers=headers,
            )
    except httpx.RequestError as exc:
        logger.warning(
            '[myah-admin] loopback proxy failed: %s %s — %s', method, path, exc,
        )
        raise HTTPException(
            status_code=503, detail=f'Dashboard loopback failed: {exc}',
        ) from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()
