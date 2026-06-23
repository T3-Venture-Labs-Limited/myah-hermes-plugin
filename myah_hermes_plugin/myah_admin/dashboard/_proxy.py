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

import os
import logging
from typing import Any

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_DEFAULT_DASHBOARD_PORT = '9119'
_TIMEOUT_SECONDS = 15.0


def _dashboard_base() -> str:
    """Return the dashboard loopback base URL for native /api/* calls.

    The dashboard normally runs on 9119, but local Myah worktrees often run
    isolated dashboards on per-branch ports. The plugin executes inside the
    dashboard process, so it can safely use a loopback URL; the port just needs
    to follow the process env instead of being hard-coded.
    """
    explicit = os.environ.get('HERMES_DASHBOARD_LOOPBACK_URL', '').strip()
    if explicit:
        return explicit.rstrip('/')

    host = os.environ.get('HERMES_DASHBOARD_LOOPBACK_HOST', '127.0.0.1').strip() or '127.0.0.1'
    port = (
        os.environ.get('HERMES_DASHBOARD_PORT')
        or os.environ.get('HERMES_WEB_PORT')
        or os.environ.get('MYAH_HERMES_WEB_PORT')
        or _DEFAULT_DASHBOARD_PORT
    )
    return f'http://{host}:{str(port).strip() or _DEFAULT_DASHBOARD_PORT}'


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
                f'{_dashboard_base()}{path}',
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
