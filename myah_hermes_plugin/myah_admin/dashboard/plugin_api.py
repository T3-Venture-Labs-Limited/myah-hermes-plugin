"""Myah-admin plugin — backend API routes.

Mounted at ``/api/plugins/myah-admin/`` by the dashboard plugin loader inside
each per-user Hermes container. The platform backend reaches these routes
through the loopback ``hermes dashboard`` server (port 9119) using the
per-container session token (see ``platform/backend/open_webui/utils/hermes_web.py``).

What this plugin owns:
    Every Myah-specific admin operation that does NOT require live
    ``GatewayRunner`` state:
      * SOUL CRUD
      * Skill discovery + CRUD
      * Plugin CRUD
      * MCP server CRUD
      * Provider catalog + credentials
      * Session title / append-message ops
      * Config aux-resolved + last-reseed + reset
      * Slash-command discovery

What this plugin does NOT own:
    Runner-coupled admin (session model overrides, cache eviction, MCP
    refresh, gateway restart busy-check). Those live on the gateway under
    ``/myah/v1/admin/*`` (see ``gateway/platforms/myah_runtime_admin.py``).
    The plugin reaches the gateway via the localhost HTTP client in
    ``_common.gateway_client``.

Sub-router layout:
    ``_soul_and_config.py``      — SOUL, aux-resolved, commands, reset, last-reseed
    ``_skills_plugins_mcp.py``   — Skills CRUD, Plugins CRUD, MCP CRUD, toolset toggle
    ``_providers.py``            — Provider catalog, credentials, models
    ``_sessions_and_lifecycle.py`` — Session ops, global model, session model overrides

Telemetry registration:
    On import, register Myah's ``SentryHook`` with the ``agent.telemetry``
    registry so Hermes runtime instrumentation routes through Sentry.
    Idempotent + silent on import failure.

Phase 4e (2026-05-07): the dashboard plugin lives inside the pip-installed
``myah_hermes_plugin`` package. The dashboard loader's filesystem scan is
satisfied by a tiny shim materialized by the ``myah-hermes-plugin install
--dashboard-only`` console script that reads ``router`` from this module.
Because this file is loaded as a real package member (never via
``spec_from_file_location``), clean relative imports work throughout.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    _common,  # noqa: F401  — registered for side effects (sub-routers depend on its module-level singletons)
    _env,
    _providers,
    _sessions_and_lifecycle,
    _skills_plugins_mcp,
    _soul_and_config,
)
from ..myah_hook import register_sentry_hook

# ── Telemetry: register SentryHook (best-effort, idempotent) ────────────────
# ``register_sentry_hook`` is itself a no-op if ``sentry_sdk`` or
# ``agent.telemetry`` is not importable, so this call is safe in any
# environment.
register_sentry_hook()


# ── Top-level router (mount point for the dashboard plugin loader) ──────────

router = APIRouter()


# Liveness probe — the platform's `hermes_web.py::web_call` against this path
# is the canonical reachability check used by the platform's
# `/api/v1/containers/{user_id}/web-health` endpoint.
@router.get('/health')
async def health() -> dict:
    """Liveness probe for the platform's hermes_web client."""
    return {'status': 'ok', 'plugin': 'myah-admin'}


# Mount sub-routers. Order is informational; FastAPI dispatches on path,
# not registration order, so collisions would be a bug at design time.
router.include_router(_soul_and_config.router)
router.include_router(_skills_plugins_mcp.router)
router.include_router(_providers.router)
router.include_router(_sessions_and_lifecycle.router)
router.include_router(_env.router)
