"""SOUL.md, config, commands, and reset endpoints for the myah-admin plugin.

Phase 4e (2026-05-07): this module now lives inside the pip-installed
``myah_hermes_plugin.myah_admin.dashboard`` package. The materialized
``/opt/myah/plugins/myah-admin/dashboard/plugin_api.py`` shim imports
the real router from the pip package, so the dashboard loader's
``spec_from_file_location`` path never touches this file — clean
relative imports work in every load context.

Handlers:
    GET    /config/soul           — read SOUL.md (text/markdown + ETag)
    PUT    /config/soul           — write SOUL.md (If-Match concurrency, 32K cap)
    GET    /config/aux-resolved   — resolved provider/model per aux task
    GET    /commands              — slash command catalog (60s cache)
    POST   /config/reset/{section}— revert a config section to image defaults
    GET    /config/last-reseed    — entrypoint reseed breadcrumb

Source: legacy aiohttp handlers in
``agent/hermes/gateway/platforms/myah_management.py`` and
``agent/hermes/gateway/platforms/myah.py:_handle_list_commands``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import json as _json

import yaml
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path as PathParam,
    Request,
    Response,
    status,
)

# Phase 4e: clean relative import inside the pip-package layout.
from ._common import gateway_client, hermes_home_path, require_session_token

logger = logging.getLogger(__name__)

router = APIRouter()


# ── SOUL.md size limits (mirror legacy myah_management.py:67-69) ────────────
SOUL_SOFT_WARN_CHARS = 8_192
SOUL_HARD_CAP_CHARS = 32_768


def _soul_etag(body: str) -> str:
    """Compute sha256-based ETag for SOUL content (legacy myah_management.py:455-458)."""
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f'"sha256-{digest}"'


# ── GET /config/soul (legacy myah_management.py:461-475) ────────────────────
@router.get("/config/soul")
async def get_soul(_auth: None = Depends(require_session_token)) -> Response:
    """Read SOUL.md as text/markdown with ETag + size-limit hints in headers."""
    soul_path = hermes_home_path() / "SOUL.md"
    if not soul_path.exists():
        raise HTTPException(status_code=404, detail="SOUL.md not found")
    body = soul_path.read_text(encoding="utf-8")
    etag = _soul_etag(body)
    return Response(
        content=body,
        media_type="text/markdown",
        headers={
            "ETag": etag,
            "X-Soul-Soft-Warn-Chars": str(SOUL_SOFT_WARN_CHARS),
            "X-Soul-Hard-Cap-Chars": str(SOUL_HARD_CAP_CHARS),
        },
    )


# ── PUT /config/soul (legacy myah_management.py:478-534) ────────────────────
@router.put("/config/soul")
async def put_soul(
    request: Request,
    _auth: None = Depends(require_session_token),
) -> Response:
    """Write SOUL.md with If-Match concurrency control.

    Body: raw text/markdown (NOT JSON).
    - 428 if If-Match header missing
    - 412 if If-Match doesn't match current ETag (returns current body)
    - 413 if body exceeds SOUL_HARD_CAP_CHARS (32 KiB)
    - 200 with optional ``warning`` field if body exceeds SOUL_SOFT_WARN_CHARS (8 KiB)
    """
    if_match = request.headers.get("If-Match")
    if not if_match:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header required for SOUL writes",
        )

    raw_body = await request.body()
    new_body = raw_body.decode("utf-8")

    if len(new_body) > SOUL_HARD_CAP_CHARS:
        raise HTTPException(
            status_code=413,  # Content Too Large (FastAPI renamed the constant; literal stays valid)
            detail={
                "error": (
                    f"SOUL content exceeds {SOUL_HARD_CAP_CHARS} character limit "
                    f"(got {len(new_body)}). SOUL is injected into every turn; "
                    f"keep it focused."
                ),
                "limit": SOUL_HARD_CAP_CHARS,
                "got": len(new_body),
            },
        )

    soul_path = hermes_home_path() / "SOUL.md"
    current_body = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
    current_etag = _soul_etag(current_body)

    if if_match != current_etag:
        # 412 with current body so the frontend can present a 3-way diff.
        return Response(
            content=_json.dumps({
                "error": "precondition failed — SOUL was modified since you read it",
                "current_body": current_body,
            }),
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            media_type="application/json",
            headers={"ETag": current_etag},
        )

    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text(new_body, encoding="utf-8")
    new_etag = _soul_etag(new_body)

    payload: dict[str, Any] = {"ok": True}
    if len(new_body) > SOUL_SOFT_WARN_CHARS:
        payload["warning"] = (
            f"SOUL is {len(new_body)} chars; recommended soft limit is "
            f"{SOUL_SOFT_WARN_CHARS}. This adds to every turn."
        )
    return Response(
        content=_json.dumps(payload),
        status_code=200,
        media_type="application/json",
        headers={"ETag": new_etag},
    )


# ── GET /config/aux-resolved (legacy myah_management.py:207-279) ────────────
@router.get("/config/aux-resolved")
async def get_aux_resolved(
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Report effective resolved provider/model for each ``auxiliary.<task>``.

    Returns ``{task_name: {provider, model, source}}`` where ``source`` is one of:
        - ``config``         : auxiliary.{task} explicitly sets provider (and usually model)
        - ``config-base-url``: auxiliary.{task}.base_url forces "custom" provider
        - ``auto-main``      : provider="auto" or unset, falls through to main model
        - ``auto-chain``     : auto-detection chain (main not available)
        - ``unresolved``     : resolver couldn't determine anything

    NOTE on ``_resolve_task_provider_model``: the source-of-truth implementation
    lives at ``agent/auxiliary_client.py:2509`` (not ``myah_management.py`` as
    the task brief stated). It is importable in the dashboard process, so we
    delegate instead of copying. If the helper signature ever changes upstream
    this endpoint must be refreshed in lockstep.
    """
    try:
        from hermes_cli.config import DEFAULT_CONFIG, load_config
        from agent.auxiliary_client import _resolve_task_provider_model
    except ImportError as exc:
        logger.error(f"aux-resolved: hermes imports failed: {exc}")
        raise HTTPException(status_code=500, detail=f"hermes imports failed: {exc}") from exc

    config = load_config() or {}
    aux_tasks = list((DEFAULT_CONFIG.get("auxiliary") or {}).keys())

    main_model_cfg = config.get("model", "")
    if isinstance(main_model_cfg, dict):
        main_provider = str(main_model_cfg.get("provider", "") or "").strip()
        main_model = str(
            main_model_cfg.get("name", "") or main_model_cfg.get("default", "") or ""
        ).strip()
    else:
        main_model = str(main_model_cfg or "").strip()
        main_provider = main_model.split("/")[0] if "/" in main_model else ""

    out: dict[str, dict[str, Any]] = {}
    for task in aux_tasks:
        try:
            provider, model, base_url, _api_key, _api_mode = _resolve_task_provider_model(
                task=task
            )
        except Exception as exc:
            logger.warning(f"aux-resolved: _resolve_task_provider_model({task!r}) raised {exc}")
            out[task] = {"provider": "", "model": None, "source": "unresolved"}
            continue

        if base_url:
            source = "config-base-url"
            resolved_provider = provider  # "custom"
            resolved_model = model
        elif provider in ("auto", ""):
            if main_provider and main_model:
                source = "auto-main"
                resolved_provider = main_provider
                resolved_model = main_model
            else:
                source = "auto-chain"
                resolved_provider = provider or "auto"
                resolved_model = model
        else:
            source = "config"
            resolved_provider = provider
            resolved_model = model

        out[task] = {
            "provider": resolved_provider,
            "model": resolved_model,
            "source": source,
        }
    return out


# ── GET /commands (legacy myah.py:1013-1080) ────────────────────────────────
# Cache the assembled command list for 60 seconds. Slash commands are derived
# from static registries + a skills directory scan + plugin commands; the
# legacy aiohttp version recomputed on every call (no cache). 60s matches
# what a `functools.lru_cache(maxsize=1)`-with-TTL would give us, without
# pulling in a TTL cache library.
_commands_cache: dict[str, Any] = {"value": None, "expires_at": 0.0}
_commands_cache_lock = threading.Lock()
_COMMANDS_CACHE_TTL_SECONDS = 60.0


def _build_commands_payload() -> dict[str, list[dict[str, Any]]]:
    """Collect commands from builtin registry + skills + plugins."""
    items: list[dict[str, Any]] = []

    # 1. Builtins from COMMAND_REGISTRY
    try:
        from hermes_cli.commands import (
            COMMAND_REGISTRY,
            ACTIVE_SESSION_BYPASS_COMMANDS,
        )
        for cmd in COMMAND_REGISTRY:
            if cmd.cli_only:
                continue
            items.append({
                "name": cmd.name,
                "category": cmd.category or "misc",
                "description": cmd.description,
                "aliases": list(cmd.aliases or []),
                "args": cmd.args_hint or "",
                "bypass": cmd.name in ACTIVE_SESSION_BYPASS_COMMANDS,
                "source": "builtin",
            })
    except Exception:
        logger.exception("[myah-admin] failed to collect builtin commands")

    # 2. Skill commands
    try:
        from agent.skill_commands import get_skill_commands
        for cmd_key, info in get_skill_commands().items():
            items.append({
                "name": info.get("name", cmd_key.lstrip("/")),
                "category": "skill",
                "description": info.get("description", ""),
                "aliases": [],
                "args": "",
                "bypass": False,
                "source": "skill",
                "skill_path": info.get("skill_dir", ""),
            })
    except Exception:
        logger.exception("[myah-admin] failed to collect skill commands")

    # 3. Plugin commands
    try:
        from hermes_cli.plugins import get_plugin_commands
        for cmd_name, cmd_info in get_plugin_commands().items():
            items.append({
                "name": cmd_name,
                "category": "plugin",
                "description": cmd_info.get("description", ""),
                "aliases": [],
                "args": "",
                "bypass": False,
                "source": "plugin",
            })
    except Exception:
        logger.exception("[myah-admin] failed to collect plugin commands")

    return {"commands": items}


@router.get("/commands")
async def list_commands(
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    """List all chat-available slash commands (builtins + skills + plugins).

    Result is cached in-memory for 60s to avoid repeated filesystem scans on
    skills/plugin discovery. Cache is per-process; restart the dashboard
    process to bust it on demand (or wait 60s).
    """
    now = time.monotonic()
    with _commands_cache_lock:
        if _commands_cache["value"] is not None and now < _commands_cache["expires_at"]:
            return _commands_cache["value"]
    payload = _build_commands_payload()
    with _commands_cache_lock:
        _commands_cache["value"] = payload
        _commands_cache["expires_at"] = now + _COMMANDS_CACHE_TTL_SECONDS
    return payload


# ── POST /config/reset/{section} (legacy myah_management.py:1281-1433) ──────
# Section taxonomy mirrors the legacy ``_RESET_SECTION_KEYS`` table verbatim.
# When upstream Hermes adds new auxiliary tasks, this table needs to grow
# alongside ``DEFAULT_CONFIG.auxiliary``.
_RESET_SECTION_KEYS: dict[str, list[str]] = {
    "model": ["model"],
    "aux_vision": [
        "auxiliary.vision.provider", "auxiliary.vision.model",
        "auxiliary.vision.base_url", "auxiliary.vision.api_key",
        "auxiliary.vision.timeout",
    ],
    "aux_web_extract": [
        "auxiliary.web_extract.provider", "auxiliary.web_extract.model",
        "auxiliary.web_extract.base_url", "auxiliary.web_extract.api_key",
        "auxiliary.web_extract.timeout",
    ],
    "aux_compression": [
        "auxiliary.compression.provider", "auxiliary.compression.model",
        "auxiliary.compression.base_url", "auxiliary.compression.api_key",
        "auxiliary.compression.timeout",
    ],
    "aux_session_search": [
        "auxiliary.session_search.provider", "auxiliary.session_search.model",
        "auxiliary.session_search.base_url", "auxiliary.session_search.api_key",
        "auxiliary.session_search.timeout",
    ],
    "aux_skills_hub": [
        "auxiliary.skills_hub.provider", "auxiliary.skills_hub.model",
        "auxiliary.skills_hub.base_url", "auxiliary.skills_hub.api_key",
        "auxiliary.skills_hub.timeout",
    ],
    "aux_approval": [
        "auxiliary.approval.provider", "auxiliary.approval.model",
        "auxiliary.approval.base_url", "auxiliary.approval.api_key",
        "auxiliary.approval.timeout",
    ],
    "aux_mcp": [
        "auxiliary.mcp.provider", "auxiliary.mcp.model",
        "auxiliary.mcp.base_url", "auxiliary.mcp.api_key",
        "auxiliary.mcp.timeout",
    ],
    "aux_flush_memories": [
        "auxiliary.flush_memories.provider", "auxiliary.flush_memories.model",
        "auxiliary.flush_memories.base_url", "auxiliary.flush_memories.api_key",
        "auxiliary.flush_memories.timeout",
    ],
    "aux_title_generation": [
        "auxiliary.title_generation.provider", "auxiliary.title_generation.model",
        "auxiliary.title_generation.base_url", "auxiliary.title_generation.api_key",
        "auxiliary.title_generation.timeout",
    ],
    "aux_follow_up_generation": [
        "auxiliary.follow_up_generation.provider",
        "auxiliary.follow_up_generation.model",
        "auxiliary.follow_up_generation.base_url",
        "auxiliary.follow_up_generation.api_key",
        "auxiliary.follow_up_generation.timeout",
    ],
    "behavior": [
        "agent.reasoning_effort", "approvals.mode", "display.personality",
    ],
    "toolsets": [
        "disabled_toolsets",
    ],
    "advanced": [
        "terminal.backend", "timezone",
    ],
}


# Path to image-baked SOUL defaults (set in agent/Dockerfile).
_SOUL_DEFAULTS_PATH = Path(os.environ.get("MYAH_SOUL_DEFAULTS", "/opt/myah/defaults/SOUL.md"))


def _read_default_value(dotted_key: str) -> Any:
    """Read a value from DEFAULT_CONFIG using a dotted key path."""
    from hermes_cli.config import DEFAULT_CONFIG
    node: Any = DEFAULT_CONFIG
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@router.post("/config/reset/{section}")
async def reset_section(
    section: str = PathParam(..., description="Section name to reset"),
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Revert a config section to image defaults.

    Specials:
      - ``soul``         : copy from ``MYAH_SOUL_DEFAULTS`` (default
                           ``/opt/myah/defaults/SOUL.md``); 503 if defaults
                           file is absent (e.g. running outside container).
      - ``mcp_servers``  : clear ``mcp_servers`` config block, then evict
                           gateway caches via ``POST /cache/evict-all`` and
                           refresh MCP via ``POST /mcp/refresh``.

    All other sections are looked up in ``_RESET_SECTION_KEYS`` and each
    dotted key is reset to its ``DEFAULT_CONFIG`` value via
    ``set_config_value`` (scalars) or YAML deep-merge (dicts/lists).
    Returns 207 with per-key errors if any individual write fails.
    """
    if section == "soul":
        if not _SOUL_DEFAULTS_PATH.exists():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="image defaults not present (are you in a dev container?)",
            )
        dst = hermes_home_path() / "SOUL.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(_SOUL_DEFAULTS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        return {"ok": True, "section": "soul"}

    if section == "mcp_servers":
        try:
            from hermes_cli.config import set_config_value
        except ImportError as exc:
            raise HTTPException(status_code=500, detail=f"set_config_value unavailable: {exc}") from exc

        config_path = hermes_home_path() / "config.yaml"
        cfg = (yaml.safe_load(config_path.read_text()) or {}) if config_path.exists() else {}
        previous_names = list((cfg.get("mcp_servers") or {}).keys())

        try:
            set_config_value("mcp_servers", "{}")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Ask the gateway to evict cached agents and refresh MCP registrations.
        # Both calls are best-effort — failures are logged but do not fail the
        # overall reset (the config was already cleared on disk).
        try:
            await gateway_client.request_or_raise("POST", "/cache/evict-all")
        except HTTPException as exc:
            logger.warning(f"reset mcp_servers: cache evict failed (non-fatal): {exc.detail}")
        try:
            await gateway_client.request_or_raise("POST", "/mcp/refresh")
        except HTTPException as exc:
            logger.warning(f"reset mcp_servers: mcp refresh failed (non-fatal): {exc.detail}")

        return {"ok": True, "section": "mcp_servers", "removed": previous_names}

    if section not in _RESET_SECTION_KEYS:
        raise HTTPException(status_code=400, detail=f"unknown section: {section}")

    try:
        from hermes_cli.config import set_config_value
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"set_config_value unavailable: {exc}") from exc

    keys = _RESET_SECTION_KEYS[section]
    errors: list[dict[str, Any]] = []
    composite_resets: dict[str, Any] = {}
    for key in keys:
        default_val = _read_default_value(key)
        if default_val is None:
            logger.warning(f"reset: key {key} not found in DEFAULT_CONFIG")
            continue
        if isinstance(default_val, (dict, list)):
            # Avoid str(dict) Python-repr corruption — apply via YAML merge below.
            composite_resets[key] = default_val
            continue
        try:
            set_config_value(key, str(default_val))
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})

    if composite_resets:
        try:
            config_path = hermes_home_path() / "config.yaml"
            cfg = (yaml.safe_load(config_path.read_text()) or {}) if config_path.exists() else {}
            for dotted_key, val in composite_resets.items():
                parts = dotted_key.split(".")
                node = cfg
                for part in parts[:-1]:
                    if not isinstance(node.get(part), dict):
                        node[part] = {}
                    node = node[part]
                node[parts[-1]] = val
            config_path.write_text(
                yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)
            )
        except Exception as exc:
            errors.append({"key": list(composite_resets.keys()), "error": str(exc)})

    if errors:
        # 207 Multi-Status — some keys reset, some failed.
        return Response(  # type: ignore[return-value]
            content=_json.dumps({"ok": False, "errors": errors}),
            status_code=status.HTTP_207_MULTI_STATUS,
            media_type="application/json",
        )
    return {"ok": True, "section": section}


# ── GET /config/last-reseed (legacy myah_management.py:1439-1465) ───────────
@router.get("/config/last-reseed")
async def get_last_reseed(
    _auth: None = Depends(require_session_token),
) -> Response:
    """Return the entrypoint reseed breadcrumb.

    The breadcrumb file at ``$HERMES_HOME/.myah_last_reseed`` is written by
    ``agent/scripts/seed_config_files.sh``. Lines are ``key=value``. The
    ``files=`` line is space-separated; we normalise it to a JSON array so
    the frontend can call ``files.join(' and ')`` without crashing.

    Returns 204 with empty body if the breadcrumb is absent.
    """
    marker = hermes_home_path() / ".myah_last_reseed"
    if not marker.exists():
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    result: dict[str, Any] = {}
    for line in marker.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            key = k.strip()
            value = v.strip()
            if key == "files":
                result[key] = [part for part in value.split() if part]
            else:
                result[key] = value
    return Response(
        content=_json.dumps(result),
        media_type="application/json",
    )


# ── Config endpoints (Phase 7.7 plugin migration — loopback) ──────────────
# See docs/superpowers/specs/2026-05-12-plugin-dashboard-migration-design.md.

from ._proxy import proxy_to_native  # noqa: E402


@router.get('/config')
async def get_full_config() -> dict:
    """Plugin-namespace mirror of GET /api/config.

    Upstream's _normalize_config_for_web + _* key stripping
    (web_server.py:856) stays the source of truth — we never re-implement.
    """
    return await proxy_to_native('GET', '/api/config')


@router.put('/config')
async def put_full_config(body: dict) -> dict:
    """Plugin-namespace mirror of PUT /api/config.

    Upstream's _denormalize_config_from_web (web_server.py:1161) stays
    the source of truth. Body shape: ``{"config": <dict>}`` per upstream's
    ConfigUpdate model.
    """
    return await proxy_to_native('PUT', '/api/config', json_body=body)


@router.get('/config/schema')
async def get_config_schema() -> dict:
    """Plugin-namespace mirror of GET /api/config/schema."""
    return await proxy_to_native('GET', '/api/config/schema')
