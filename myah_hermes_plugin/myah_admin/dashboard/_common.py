"""Shared helpers for the myah-admin dashboard plugin.

The plugin runs in the ``hermes dashboard`` process (port 9119), distinct from
the ``hermes`` gateway process (port 8642). This module supplies:

1. ``require_session_token`` — FastAPI dependency that validates the
   ``X-Hermes-Session-Token`` (or legacy ``Authorization: Bearer``) header.
   Plugin routes are auth-exempt at the dashboard middleware (see
   ``hermes_cli/web_server.py:231``), so each plugin route MUST authenticate
   itself. We reuse the dashboard's ``HERMES_WEB_SESSION_TOKEN`` env var that
   the platform already injects on container spawn.

2. ``GatewayClient`` — small HTTP client to reach the runtime-control admin
   surface at ``http://localhost:{API_SERVER_PORT}/myah/v1/admin/*`` for
   operations that require ``GatewayRunner`` state (session model overrides,
   cache eviction, busy-check, MCP reload).

3. ``json_problem`` — RFC-7807-ish error envelope so the platform's existing
   ``_proxy_response`` and ``_raise_for_upstream_error`` helpers continue to
   render the right messages without changes.

Why a single module: every plugin handler imports these two utilities. Splitting
them into separate files would mean threading dependencies through every route.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)


# ── Auth ────────────────────────────────────────────────────────────────────

_SESSION_TOKEN_ENV = "HERMES_WEB_SESSION_TOKEN"


def _get_session_token() -> str | None:
    """The shared secret the dashboard process expects.

    Read at request time (not module load) so tests can override via
    ``monkeypatch.setenv`` without re-importing the module.
    """
    token = os.environ.get(_SESSION_TOKEN_ENV)
    return token or None


async def require_session_token(
    x_hermes_session_token: str | None = Header(default=None, alias="X-Hermes-Session-Token"),
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: validate the session token.

    Accepts either ``X-Hermes-Session-Token: <token>`` (preferred — matches the
    rest of the dashboard) or ``Authorization: Bearer <token>`` (the platform
    backend's ``hermes_web.py::web_call`` sends this form, so we accept both).
    """
    expected = _get_session_token()
    if not expected:
        # No token configured — accept all. This matches the dashboard's own
        # behaviour when ``HERMES_WEB_SESSION_TOKEN`` is unset (development).
        return

    presented: str | None = None
    if x_hermes_session_token:
        presented = x_hermes_session_token.strip()
    elif authorization and authorization.startswith("Bearer "):
        presented = authorization[7:].strip()

    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing session token",
        )


# ── Gateway client ──────────────────────────────────────────────────────────


class GatewayClient:
    """Minimal HTTP client for the gateway's ``/myah/v1/admin/*`` surface.

    Plugin handlers that need ``GatewayRunner`` state instantiate this and
    forward the call. Reads ``API_SERVER_PORT`` (default 8642) and
    ``MYAH_ADAPTER_AUTH_KEY`` from the container env — same env vars the
    gateway itself uses, so localhost-to-localhost auth round-trips correctly.
    """

    def __init__(self, *, timeout: float = 15.0) -> None:
        # The runtime-control routes (``/myah/v1/admin/*``) live on the
        # MyahStandaloneRunner's aiohttp app, NOT the FastAPI api_server.
        # Tier 2A Task 2A.3 (2026-05-07) moved them off ``API_SERVER_PORT``
        # (8642 — chat-completions only) onto a dedicated standalone port
        # (8643 default, ``MYAH_GATEWAY_PORT`` env override). Reading
        # API_SERVER_PORT here was the 2026-05-11 B2 regression: every
        # session-model override / cache-evict / mcp-reload / busy-check
        # call hit the wrong port and returned 404 silently — including
        # the model-picker click that triggered the user-visible
        # "Could not switch model for this chat" toast.
        #
        # Single source of truth: ``standalone_runner.resolve_default_port``
        # owns the env-var contract (parse + fallback + warning). Importing
        # it here keeps both ends in lockstep — if the runner ever changes
        # which env var or default it uses, the GatewayClient follows.
        from myah_hermes_plugin.myah_platform.standalone_runner import (
            resolve_default_port,
        )
        self._port = resolve_default_port()
        # Auth key resolution order:
        #   1. MYAH_ADAPTER_AUTH_KEY — the canonical name the gateway config
        #      uses (gateway/config.py:1279). Set when the operator passes
        #      it explicitly via docker -e.
        #   2. API_SERVER_KEY — what the platform actually injects on
        #      container spawn (see platform/.env). The container entrypoint
        #      mirrors this into ~/.hermes/.env as MYAH_ADAPTER_AUTH_KEY for
        #      the gateway, but the dashboard process inherits the original
        #      env from supervisord and never sees the .env-only mirror.
        # Reading both lets the plugin's gateway client work on every layer
        # of the deployment without the entrypoint having to remember a
        # supervisord-level export.
        self._auth_key = (
            os.environ.get("MYAH_ADAPTER_AUTH_KEY")
            or os.environ.get("API_SERVER_KEY")
            or ""
        )
        self._base_url = f"http://localhost:{self._port}/myah/v1/admin"
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._auth_key:
            h["Authorization"] = f"Bearer {self._auth_key}"
        return h

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Forward to ``http://localhost:8642/myah/v1/admin{path}``.

        ``path`` should start with ``/`` (e.g. ``/cache/evict-all``).
        Returns ``{status, body}`` — does NOT raise on 4xx/5xx (caller decides).
        """
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout or self._timeout) as client:
                resp = await client.request(method, url, json=json_body, headers=self._headers())
        except httpx.ConnectError as exc:
            logger.error("[myah-admin] gateway connect error %s %s: %s", method, path, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gateway runtime-control surface unavailable",
            ) from exc
        except httpx.TimeoutException as exc:
            logger.error("[myah-admin] gateway timeout %s %s: %s", method, path, exc)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Gateway runtime-control surface timed out",
            ) from exc

        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text or None
        return {"status": resp.status_code, "body": body}

    async def request_or_raise(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        """Same as ``request`` but propagates non-2xx as ``HTTPException``."""
        result = await self.request(method, path, json_body=json_body, timeout=timeout)
        if result["status"] >= 400:
            detail = result["body"]
            if isinstance(detail, dict) and "error" in detail:
                detail = detail
            raise HTTPException(status_code=result["status"], detail=detail)
        return result["body"]


# ── Convenience: shared client instance ─────────────────────────────────────

# Plugin handlers can import this singleton OR instantiate their own. Singleton
# avoids repeated env-var reads in the hot path.
gateway_client = GatewayClient()


# ── Hermes config / paths helpers ───────────────────────────────────────────


def hermes_home() -> str:
    """The active ``HERMES_HOME`` (profile-aware) as a string.

    Wraps ``hermes_constants.get_hermes_home()`` so handlers don't have to
    import it themselves. Falls back to ``~/.hermes`` if the import fails (the
    plugin should still load even if Hermes internals shift).

    Returns ``str`` to match ``hermes_constants.get_hermes_home`` callers
    that print or join paths textually. For ``Path`` operations, prefer
    :func:`hermes_home_path`.
    """
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:  # pragma: no cover — defensive
        return os.path.expanduser("~/.hermes")


def hermes_home_path() -> Path:
    """The active ``HERMES_HOME`` as a :class:`~pathlib.Path`.

    Convenience wrapper for sub-routers that build filesystem paths from
    ``HERMES_HOME``. Multiple sub-routers used to roll their own
    ``Path(hermes_home())`` shim — this consolidates them.
    """
    return Path(hermes_home())
