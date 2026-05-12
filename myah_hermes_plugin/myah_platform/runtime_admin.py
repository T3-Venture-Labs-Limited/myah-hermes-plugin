"""Runtime-control admin surface for the Myah platform adapter.

This module exposes the small set of admin operations that **must** run in the
gateway process because they touch the live ``GatewayRunner`` (session model
overrides, agent cache eviction, busy-check). Everything else (file-system
admin: SOUL, skills, plugins, MCP CRUD, providers, reset) lives in the
``myah_hermes_plugin.myah_admin.dashboard`` plugin which runs in the
``hermes dashboard`` process. Phase 4e moved that dashboard out of the
fork-side ``plugins/myah-admin/`` directory and into this pip package.

Mounting:
    Routes are added under ``/myah/v1/admin/*`` via
    ``register_runtime_admin_routes(app, *, runner, auth_key)`` from
    :mod:`myah_hermes_plugin.myah_platform.adapter`'s
    ``MyahAdapter._register_routes_on_app``.

Auth:
    Same Bearer-token model as the rest of the Myah adapter. The platform
    backend forwards every request with ``Authorization: Bearer <MYAH_ADAPTER_AUTH_KEY>``.
    The ``myah-admin`` dashboard plugin reaches this surface via the
    MyahStandaloneRunner's port (``MYAH_GATEWAY_PORT`` env, default 8643)
    using the same key — read from ``MYAH_ADAPTER_AUTH_KEY`` env var
    inside the container. NOTE: this is NOT the FastAPI ``api_server``
    port (``API_SERVER_PORT``, default 8642), which hosts ``/v1/*`` chat
    completions only. Tier 2A Task 2A.3 (2026-05-07) split these two
    surfaces; targeting the api_server for ``/myah/v1/admin/*`` returns
    404 (B2 production regression, 2026-05-11).

Why a separate module:
    Keeps the adapter focused on chat I/O. Phase 4d (2026-05-04) moved this
    file out of the core hermes-agent repo and into ``myah-hermes-plugin``;
    upstream Hermes sees zero Myah-specific admin code now.
"""

from __future__ import annotations

import hmac
import logging
import shutil
import subprocess
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aiohttp import web

    from gateway.run import GatewayRunner

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore

from ._runner_state import (
    evict_session_agent_direct,
    get_session_override_direct,
    iter_cached_session_keys_direct,
    iter_running_session_keys_direct,
    set_session_override_direct,
)

logger = logging.getLogger(__name__)


# ── Auth helper ─────────────────────────────────────────────────────────────


def _check_auth(
    request: "web.Request", auth_key: Optional[str]
) -> Optional["web.Response"]:
    """Same Bearer-token model as MyahAdapter._check_auth."""
    if not auth_key:
        return None
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header[7:].strip()
        if hmac.compare_digest(token, auth_key):
            return None
    return web.json_response({"error": "Invalid or missing auth token"}, status=401)


# ── Handlers ────────────────────────────────────────────────────────────────


def _make_handlers(runner: "GatewayRunner", auth_key: Optional[str]):
    """Build closure-captured handlers bound to a specific runner + auth key.

    The runner is captured at registration time so handlers don't depend on a
    module-level global (which made testing ``myah_management`` ugly).
    """

    async def get_session_override(request: "web.Request") -> "web.Response":
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        session_key = request.match_info["session_key"]
        override = get_session_override_direct(runner, session_key)
        return web.json_response({"override": override})

    async def put_session_override(request: "web.Request") -> "web.Response":
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        session_key = request.match_info["session_key"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Body must be an object"}, status=400)
        # The override dict shape is whatever ``GatewayRunner.SessionOverride``
        # accepts — typically {model, provider, base_url?}. Pass through.
        try:
            set_session_override_direct(runner, session_key, body)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("[myah-admin] set_session_override failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "session_key": session_key})

    async def delete_session_override(request: "web.Request") -> "web.Response":
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        session_key = request.match_info["session_key"]
        # No public API for "remove"; setting to empty dict is the closest we
        # can do without exposing internals. The convention is that an empty
        # override means "no override active".
        set_session_override_direct(runner, session_key, {})
        return web.json_response({"ok": True})

    async def get_active_sessions(request: "web.Request") -> "web.Response":
        """List the keys of sessions with an in-flight run.

        Used by the dashboard plugin's gateway-restart endpoint to enforce a
        busy-check before issuing ``supervisorctl restart hermes``.
        """
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        keys = iter_running_session_keys_direct(runner)
        return web.json_response({"active_session_keys": keys, "count": len(keys)})

    async def evict_all_caches(request: "web.Request") -> "web.Response":
        """Evict every cached agent.

        Called by the plugin after writes that change agent assembly (global
        model change, MCP add/remove, toolset toggle). Idempotent.
        """
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        evicted = 0
        for key in iter_cached_session_keys_direct(runner):
            if evict_session_agent_direct(runner, key):
                evicted += 1
        return web.json_response({"ok": True, "evicted": evicted})

    async def evict_session_cache(request: "web.Request") -> "web.Response":
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        session_key = request.match_info["session_key"]
        evicted = evict_session_agent_direct(runner, session_key)
        return web.json_response({"ok": True, "evicted": evicted})

    async def reload_mcp(request: "web.Request") -> "web.Response":
        """Re-read MCP servers from config.yaml and re-register them.

        Called by the plugin after writing to ``mcp_servers`` in config.yaml.
        """
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        try:
            from agent.mcp_registry import register_mcp_servers
        except Exception:  # pragma: no cover — module path may shift upstream
            logger.exception("[myah-admin] failed to import register_mcp_servers")
            return web.json_response(
                {"error": "MCP registry module not available"}, status=500
            )
        try:
            register_mcp_servers()
        except Exception as exc:
            logger.exception("[myah-admin] register_mcp_servers failed")
            return web.json_response({"error": str(exc)}, status=500)
        # Evict caches so next message picks up the new toolset.
        evicted = 0
        for key in iter_cached_session_keys_direct(runner):
            if evict_session_agent_direct(runner, key):
                evicted += 1
        return web.json_response({"ok": True, "evicted": evicted})

    async def disconnect_mcp(request: "web.Request") -> "web.Response":
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        name = request.match_info["name"]
        try:
            # Phase 5 (B1 follow-up): the original import targeted
            # ``agent.mcp_registry`` — a module that does not exist anywhere
            # in the repo. Every request hit this fallback returning 500
            # "MCP registry module not available", and the dashboard's
            # ``DELETE /mcp/<name>`` chain silently failed to actually
            # disconnect the server from ``tools.mcp_tool._servers``.
            #
            # The real plugin-side helper lives at
            # ``myah_hermes_plugin.runtime_extensions.mcp_disconnect`` and
            # has been shipped since PR #106 (Phase E). It uses the upstream
            # ``tools.mcp_tool`` private state + ``_run_on_mcp_loop`` bridge
            # exactly as the upstream "shutdown all servers" code does.
            from myah_hermes_plugin.runtime_extensions.mcp_disconnect import (
                disconnect_mcp_server,
            )
        except Exception:  # pragma: no cover
            logger.exception("[myah-admin] failed to import disconnect_mcp_server")
            return web.json_response(
                {"error": "MCP disconnect helper not available"}, status=500
            )
        try:
            disconnect_mcp_server(name)
        except Exception as exc:
            logger.exception("[myah-admin] disconnect_mcp_server failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "name": name})

    async def get_provider_catalog(request: "web.Request") -> "web.Response":
        """Return the merged provider catalog with credential status.

        Same shape as the dashboard's ``/api/plugins/myah-admin/providers``
        endpoint but enriched with a ``has_credential`` field, and
        reachable on the gateway adapter port using the standard adapter
        auth (no separate dashboard session token needed).

        Used by the platform's ``fetch_hermes_provider_catalog`` to
        auto-import providers into the user's ``UserProviderStatuses``
        rows at /whoami time (ISSUE-003 follow-up). On stock hermes
        ``has_credential`` is determined by:
        - ``api_key`` providers: env var named ``env_var`` is set
        - ``oauth_*`` providers: auth.json has an entry for the provider

        Response shape (a list — platform's helper expects this):

            [
              {"id": "openrouter", "display_name": "OpenRouter",
               "auth_type": "api_key", "env_var": "OPENROUTER_API_KEY",
               "has_credential": true, "v1_visible": true, ...},
              ...
            ]
        """
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        try:
            from myah_hermes_plugin.myah_admin.dashboard._providers import (
                _build_catalog,
            )
        except Exception:
            logger.exception(
                "[myah-admin] providers endpoint: failed to import _build_catalog"
            )
            return web.json_response({"providers": []}, status=200)

        try:
            catalog = await _build_catalog()
        except Exception:
            logger.exception(
                "[myah-admin] providers endpoint: _build_catalog() raised"
            )
            return web.json_response({"providers": []}, status=200)

        # Enrich with has_credential — check env vars, auth.json
        # ``providers`` (per-provider OAuth tokens), and
        # ``credential_pool`` (the pool of all configured credentials
        # the user has registered via ``hermes auth`` / setup wizard).
        # The credential_pool is the canonical source of "user has this
        # provider configured" — it includes both API-key and OAuth
        # providers, populated whenever the user runs setup or adds a
        # credential. The legacy ``providers`` key only carries OAuth
        # tokens; the env-var heuristic still applies for api_key types.
        import os as _os
        import json as _json
        _auth_providers: set[str] = set()
        _auth_pool: set[str] = set()
        try:
            from hermes_constants import get_hermes_home
            _auth_path = get_hermes_home() / "auth.json"
            if _auth_path.exists():
                with open(_auth_path, encoding="utf-8") as f:
                    _auth = _json.load(f) or {}
                if isinstance(_auth, dict):
                    _providers_dict = _auth.get("providers") or {}
                    if isinstance(_providers_dict, dict):
                        _auth_providers = set(_providers_dict.keys())
                    _pool_dict = _auth.get("credential_pool") or {}
                    if isinstance(_pool_dict, dict):
                        _auth_pool = set(_pool_dict.keys())
        except Exception:
            pass

        providers_list = []
        for slug, entry in (catalog.items() if isinstance(catalog, dict) else []):
            if not isinstance(entry, dict):
                continue
            auth_type = entry.get("auth_type") or ""
            env_var = entry.get("env_var") or ""
            has_credential = False
            # credential_pool is the canonical "configured" signal — covers
            # both API-key and OAuth providers that the user explicitly
            # set up via `hermes auth` or the setup wizard.
            if slug in _auth_pool:
                has_credential = True
            # Legacy OAuth path: top-level `providers` entry
            elif auth_type.startswith("oauth") and slug in _auth_providers:
                has_credential = True
            # API-key fallback: env var is set in the process environment
            elif auth_type == "api_key" and env_var and _os.environ.get(env_var):
                has_credential = True

            providers_list.append({
                "id": slug,
                "display_name": entry.get("display_name", slug),
                "label": entry.get("display_name", slug),  # alias for compat
                "description": entry.get("description", ""),
                "auth_type": auth_type,
                "env_var": env_var,
                "inference_base_url": entry.get("inference_base_url", ""),
                "v1_visible": bool(entry.get("v1_visible", False)),
                "has_credential": has_credential,
                "models": entry.get("curated_models", []),
            })

        return web.json_response({"providers": providers_list})

    async def get_hermes_config(request: "web.Request") -> "web.Response":
        """Return the hermes ``config.yaml`` model block.

        Used by the platform's ``fetch_hermes_default_model`` helper to
        discover the user's hermes-side configured default (provider +
        model). Replaces the previous dashboard ``:9119/api/config`` call
        which required a separate ``HERMES_WEB_SESSION_TOKEN`` that OSS
        users typically don't configure.

        Returns just the ``model`` sub-dict — narrower surface than the
        full config (no API keys, no plugin settings).

        Response shape::

            {"model": {"provider": "opencode-go", "default": "mimo-v2.5",
                       "base_url": "https://...", "api_mode": "..."}}

        On any read failure (file missing, YAML invalid, etc.) returns
        ``{"model": {}}`` with status 200 — callers degrade gracefully.
        """
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        try:
            import yaml
            from hermes_constants import get_hermes_home
        except Exception:
            logger.exception("[myah-admin] config endpoint: failed to import deps")
            return web.json_response({"model": {}}, status=200)

        cfg_path = get_hermes_home() / "config.yaml"
        if not cfg_path.exists():
            return web.json_response({"model": {}}, status=200)

        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            logger.exception("[myah-admin] config endpoint: failed to read config.yaml")
            return web.json_response({"model": {}}, status=200)

        model_block = cfg.get("model")
        if not isinstance(model_block, dict):
            return web.json_response({"model": {}}, status=200)

        # Whitelist fields — never echo a key called "api_key" or similar
        # secret-ish name. The hermes config.yaml model block currently
        # has provider/default/base_url/api_mode only, all non-secret.
        safe_model = {
            "provider": str(model_block.get("provider") or ""),
            "default": str(model_block.get("default") or ""),
            "base_url": str(model_block.get("base_url") or ""),
            "api_mode": str(model_block.get("api_mode") or ""),
        }
        return web.json_response({"model": safe_model})

    async def gateway_restart(request: "web.Request") -> "web.Response":
        """Busy-check + ``supervisorctl restart hermes``.

        Returns 409 if any session has an in-flight run. The frontend can
        retry after the run completes. Equivalent to the legacy
        ``handle_gateway_restart`` endpoint, kept on the gateway because the
        busy check requires runner state.
        """
        if (resp := _check_auth(request, auth_key)) is not None:
            return resp
        active = iter_running_session_keys_direct(runner)
        if active:
            return web.json_response(
                {
                    "error": "Cannot restart while runs are in flight",
                    "active_session_keys": active,
                },
                status=409,
            )
        supervisorctl = shutil.which("supervisorctl")
        if not supervisorctl:
            return web.json_response(
                {"error": "supervisorctl not available in container"}, status=503
            )
        try:
            result = subprocess.run(
                [supervisorctl, "restart", "hermes"],
                capture_output=True,
                timeout=30,
                check=False,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return web.json_response({"error": "Restart timed out"}, status=504)
        if result.returncode != 0:
            return web.json_response(
                {
                    "error": "supervisorctl restart failed",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                status=500,
            )
        return web.json_response({"ok": True, "stdout": result.stdout})

    return {
        "get_session_override": get_session_override,
        "put_session_override": put_session_override,
        "delete_session_override": delete_session_override,
        "get_active_sessions": get_active_sessions,
        "evict_all_caches": evict_all_caches,
        "evict_session_cache": evict_session_cache,
        "reload_mcp": reload_mcp,
        "disconnect_mcp": disconnect_mcp,
        "gateway_restart": gateway_restart,
        "get_hermes_config": get_hermes_config,
        "get_provider_catalog": get_provider_catalog,
    }


# ── Public registrar ────────────────────────────────────────────────────────


def register_runtime_admin_routes(
    app: "web.Application",
    *,
    runner: "GatewayRunner",
    auth_key: Optional[str],
) -> None:
    """Add ``/myah/v1/admin/*`` routes to the shared aiohttp app.

    Called from ``MyahAdapter._register_routes_on_app`` as part of the
    pre-setup hook (i.e. before the router is frozen).
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for runtime admin routes")

    handlers = _make_handlers(runner, auth_key)

    app.router.add_get(
        "/myah/v1/admin/sessions/{session_key}/override",
        handlers["get_session_override"],
    )
    app.router.add_put(
        "/myah/v1/admin/sessions/{session_key}/override",
        handlers["put_session_override"],
    )
    app.router.add_delete(
        "/myah/v1/admin/sessions/{session_key}/override",
        handlers["delete_session_override"],
    )
    app.router.add_get(
        "/myah/v1/admin/sessions/active",
        handlers["get_active_sessions"],
    )
    app.router.add_post(
        "/myah/v1/admin/cache/evict-all",
        handlers["evict_all_caches"],
    )
    app.router.add_post(
        "/myah/v1/admin/cache/evict/{session_key}",
        handlers["evict_session_cache"],
    )
    app.router.add_post(
        "/myah/v1/admin/mcp/refresh",
        handlers["reload_mcp"],
    )
    app.router.add_post(
        "/myah/v1/admin/mcp/disconnect/{name}",
        handlers["disconnect_mcp"],
    )
    app.router.add_post(
        "/myah/v1/admin/gateway/restart",
        handlers["gateway_restart"],
    )
    app.router.add_get(
        "/myah/v1/admin/config",
        handlers["get_hermes_config"],
    )
    app.router.add_get(
        "/myah/v1/admin/providers",
        handlers["get_provider_catalog"],
    )

    logger.info(
        "[myah-admin] runtime-control routes registered (11 endpoints under /myah/v1/admin/)"
    )
