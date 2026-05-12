"""Myah platform plugin entry point.

Registers Myah-specific tools, hooks, and platform adapter.

This is the canonical Hermes plugin entry point — declared in
``pyproject.toml`` as ``hermes_agent.plugins -> myah-platform``. Hermes'
``PluginManager`` calls :func:`register` once at startup with a
``PluginContext`` instance.

Phase 4d (2026-05-04) added the ``ctx.register_platform(...)`` wiring that
moves the Myah platform adapter out of upstream Hermes core into this
plugin. Earlier phases (4b/4c) bootstrapped the package skeleton and the
secrets tool. Phase 4f will follow with cron/status_hint/boot_md hooks.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from .. import sentry_init
from ..myah_tools import secrets_tool
from .pre_dispatch_hook import myah_pre_gateway_dispatch

log = logging.getLogger(__name__)


def _bootstrap_user_id() -> None:
    """Discover ``MYAH_USER_ID`` via the platform's ``/whoami`` endpoint.

    Idempotent: a no-op if ``MYAH_USER_ID`` is already set (hosted Myah
    injects it per-container at spawn time, so this branch is the OSS
    self-hosted case where the user runs ``hermes gateway start`` on
    their host without container spawning).

    Without ``MYAH_USER_ID`` the cron→Myah webhook payload's ``user_id``
    field is empty, which the platform's ``/api/v1/processes/webhook/run-complete``
    handler rejects with HTTP 400. Auto-discovering it removes the manual
    "copy your user_id from the platform UI to ~/.hermes/.env" friction.

    Auth: uses ``MYAH_AGENT_BEARER_TOKEN`` (or its alias
    ``MYAH_AGENT_TOKEN``). The OSS user pastes this once into both
    ``platform/.env`` and ``~/.hermes/.env`` — same secret, two consumers.

    Failure modes (all silent + logged):
    - ``MYAH_PLATFORM_BASE_URL`` unset → cannot reach platform at all.
    - Bearer token unset → platform refuses with 503.
    - Network error → platform may be starting up; retry next plugin load.
    - ``/whoami`` 404 → no users registered yet; user signs up first.
    """
    if os.environ.get("MYAH_USER_ID"):
        return

    base_url = os.environ.get("MYAH_PLATFORM_BASE_URL", "").strip().rstrip("/")
    bearer = (
        os.environ.get("MYAH_PLATFORM_BEARER")
        or os.environ.get("MYAH_AGENT_BEARER_TOKEN")
        or os.environ.get("MYAH_AGENT_TOKEN")
        or ""
    ).strip()

    if not base_url:
        log.info(
            "MYAH_USER_ID unset and MYAH_PLATFORM_BASE_URL not configured; "
            "cron webhook will be skipped until both are set"
        )
        return
    if not bearer:
        log.info(
            "MYAH_USER_ID unset and no platform bearer token in env "
            "(MYAH_AGENT_BEARER_TOKEN); /whoami bootstrap skipped"
        )
        return

    url = f"{base_url}/api/v1/myah/whoami"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {bearer}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        log.warning(
            "MYAH_USER_ID bootstrap: /whoami returned HTTP %s — "
            "set MYAH_USER_ID manually if cron deliveries are needed",
            exc.code,
        )
        return
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning("MYAH_USER_ID bootstrap: could not reach %s: %s", url, exc)
        return

    user_id = (data.get("user_id") or "").strip() if isinstance(data, dict) else ""
    if user_id:
        os.environ["MYAH_USER_ID"] = user_id
        log.info("Discovered MYAH_USER_ID=%s via /whoami", user_id)
        # NOTE: The platform's /whoami endpoint handles default_model sync
        # itself (see platform/.../routers/myah.py). Plugin used to POST to
        # /api/v1/users/user/default-model directly, but that endpoint
        # requires JWT auth via get_verified_user — the plugin only has the
        # agent bearer token. Sync now happens platform-side as a side
        # effect of the /whoami call we just made.
    else:
        log.warning(
            "MYAH_USER_ID bootstrap: /whoami returned no user_id; "
            "the platform may have no registered users yet"
        )

# Platform-hint string injected by ``agent.prompt_builder.get_platform_hint``
# when the agent is running on the Myah platform. This text used to live
# inside core's ``PLATFORM_HINTS`` dict; Phase 4d moved it onto the
# ``PlatformEntry`` so the plugin owns its own prompt copy.
_MYAH_PLATFORM_HINT = (
    "User is interacting via the Myah web platform. "
    "Full markdown rendering is supported including code blocks, tables, "
    "images (via URL), and links. The user can see tool call progress "
    "and reasoning in real time."
)


def _validate_myah_config(config: Any) -> bool:
    """Reject empty/invalid Myah configurations early in adapter creation.

    Currently a permissive check — the adapter handles missing auth keys
    by serving requests unauthenticated (single-tenant local dev path).
    Returns True so the registry always proceeds to instantiation; the
    adapter itself surfaces specific errors if it cannot start.
    """
    return True


def _wire_secret_capture_callback() -> None:
    """Register a global secret-capture callback with vanilla upstream.

    Vanilla ``tools/skills_tool.set_secret_capture_callback`` accepts a
    single-arg callable invoked with ``(name, prompt, metadata)``. The
    plugin's adapter handles secrets per-stream via
    ``MyahAdapter._secret_capture_callback`` — but that's a bound method
    needing ``stream_id`` and an adapter instance.

    This wrapper looks up the active adapter via the late-bound
    ``adapter._LATEST_ADAPTER`` module attribute (set by
    :meth:`MyahAdapter.start`) and routes to the right stream by
    consulting the adapter's ``_session_streams`` mapping with the
    contextvar-recorded session key. Single-adapter / single-process
    assumption documented in :func:`register`.

    Silent no-op if upstream's ``tools.skills_tool`` is unavailable
    (e.g. older Hermes builds) — secrets simply won't prompt, which
    matches the pre-Phase-5.1 behavior.
    """
    try:
        from tools.skills_tool import set_secret_capture_callback
    except ImportError:
        log.debug(
            "tools.skills_tool.set_secret_capture_callback unavailable; "
            "secret prompts will not fire"
        )
        return

    from . import adapter as _adapter_module

    def _global_secret_callback(name: str, prompt: str, metadata: Any = None) -> dict:
        """Vanilla-shaped wrapper. Routes to the active adapter+stream."""
        adapter_inst = getattr(_adapter_module, "_LATEST_ADAPTER", None)
        if adapter_inst is None:
            log.warning(
                "secret_capture_callback fired but no MyahAdapter is active; "
                "auto-skipping secret %r", name
            )
            return {
                "success": True,
                "skipped": True,
                "stored_as": name,
                "validated": False,
                "message": "No active Myah adapter to prompt for secret.",
            }

        # Resolve stream_id from the active session_key contextvar that
        # vanilla approvals set inside the agent worker thread. Falls
        # back to the most recently activated stream if multiple are
        # tracked.
        try:
            from tools.approval import get_current_session_key
        except ImportError:
            session_key = ""
        else:
            session_key = get_current_session_key() or ""

        stream_id = (
            adapter_inst._session_streams.get(session_key, "")
            if session_key
            else ""
        )
        if not stream_id and adapter_inst._session_streams:
            # Fallback: pick any active stream. Single-tenant assumption
            # makes this safe; in multi-tenant we'd misroute.
            stream_id = next(iter(adapter_inst._session_streams.values()))

        return adapter_inst._secret_capture_callback(
            name, prompt, metadata=metadata, stream_id=stream_id
        )

    set_secret_capture_callback(_global_secret_callback)
    log.info("Myah plugin: registered secret_capture_callback with tools.skills_tool")


def register(ctx: Any) -> None:
    """Register Myah platform extensions with the Hermes runtime.

    Wires three things:

    1. **Secrets tool** (Phase 4c): the ``secrets`` tool under the
       ``hermes-myah`` toolset.
    2. **Platform adapter** (Phase 4d): the Myah web platform via
       ``ctx.register_platform(...)`` with capability fields supported by
       upstream's ``PlatformEntry`` (``allowed_users_env``,
       ``allow_all_env``, ``platform_hint``).
    3. **Phase 4f (TODO)**: cron status_hint plumbing, boot_md hook,
       offline cron delivery — all currently no-ops in upstream Hermes.

    Phase 4d (TODO follow-up): the secrets tool's ``request`` action
    needs the platform adapter to wire a session-keyed
    ``secrets_request`` callback. The adapter already provides the
    HTTP transport (``POST /myah/v1/secret/{stream_id}``). The
    callback registration belongs here and will land in a follow-up
    PR alongside the cron hooks.
    """
    # ── Sentry / TelemetryHook initialization (Tier 2A Task 2A.7) ──────
    # Idempotent — silently no-ops when SENTRY_DSN_AGENT is unset (the
    # OSS-user case). For hosted Myah agent containers SENTRY_DSN_AGENT
    # is injected at spawn time and this call wires up Sentry SDK +
    # registers the SentryHook adapter so Hermes runtime telemetry calls
    # (which only see agent.telemetry.TelemetryHook) route through it.
    sentry_init.setup_sentry()

    # ── Sentry observability hooks (Phase 2) ───────────────────────────
    # Register pre/post_api_request + pre/post_tool_call hooks that emit
    # Myah-context breadcrumbs alongside the OpenAIIntegration's raw
    # HTTP capture. The breadcrumbs are no-ops when SENTRY_DSN_AGENT is
    # unset (sentry_sdk.add_breadcrumb on an uninitialized SDK is a
    # silent return), so OSS-mode incurs zero cost. Best-effort import
    # — older plugin builds without the observability/ package degrade
    # to no-op rather than break the rest of register().
    try:
        from myah_hermes_plugin.observability import register_sentry_hooks

        register_sentry_hooks(ctx)
    except Exception:
        log.exception("Failed to register Sentry observability hooks")
    # ───────────────────────────────────────────────────────────────────

    # ── MYAH_USER_ID bootstrap (Phase 8.2 OSS) ──────────────────────────
    # Hosted Myah injects MYAH_USER_ID per-container; OSS users would
    # otherwise need to paste it manually. Auto-discover via /whoami
    # if missing. Idempotent: no-op if MYAH_USER_ID is already set.
    _bootstrap_user_id()

    # ── F6 cron→chat delivery on stock vanilla (Phase E) ───────────
    # Vanilla cron/scheduler.py doesn't call build_delivery_metadata,
    # so MyahAdapter never sees job_id and never routes cron output to
    # the platform webhook. The watcher observes the on-disk output
    # convention as a workaround. The watcher start is wired as a
    # pre_gateway_dispatch hook (lazy start) to avoid the
    # asyncio.get_event_loop() deprecation in Python 3.12+ — the
    # gateway's event loop is guaranteed running by the time any
    # dispatch fires. Idempotent — silent no-op if
    # MYAH_PLATFORM_BASE_URL is unset.
    try:
        from myah_hermes_plugin.runtime_extensions.cron_watcher import (
            register_cron_watcher,
        )
        register_cron_watcher(ctx)
    except Exception:
        log.exception("Failed to register cron output watcher hook")
    # ───────────────────────────────────────────────────────────────

    # ── Phase F: structured streaming workaround for stock vanilla ─
    # Vanilla _run_agent doesn't have the polymorphic
    # get_structured_callbacks dispatch the fork carries, so MyahAdapter's
    # structured streaming callbacks never wire up by default. The
    # pre_llm_call hook below mutates AIAgent callbacks just-in-time
    # before each LLM call to install them. Removable when upstream
    # U-CB PR lands.
    try:
        from myah_hermes_plugin.runtime_extensions.streaming_callbacks import (
            register_streaming_hook,
        )
        register_streaming_hook(ctx)
    except Exception:
        log.exception("Failed to register Phase F streaming hook")
    # ───────────────────────────────────────────────────────────────

    # ── Secret-capture global callback (Phase 5.1 — F4 vanilla support) ─
    # Vanilla upstream's tools/skills_tool exposes
    # set_secret_capture_callback(fn) — a single global registration
    # point invoked when a tool needs the user to provide a secret.
    # Without registering here, the agent silently auto-skips secret
    # prompts because no callback is wired.
    #
    # Single-adapter assumption: the plugin runs at most one MyahAdapter
    # per process (single-user agent container in hosted Myah; single-
    # tenant in OSS). The wrapper below dispatches to the active
    # adapter via _LATEST_ADAPTER which is set in MyahAdapter.start().
    # If we ever ship multi-tenant in-process, replace this with a
    # contextvar-keyed lookup.
    _wire_secret_capture_callback()

    # ── pre_gateway_dispatch hook (Tier 2A Task 2A.4) ──────────────────
    # Replaces skip_user_authorization semantics that PR #20 removed.
    # Currently a no-op-allow for Myah-platform messages; reserved as an
    # extension point for future Myah-specific routing logic that must
    # NOT live in upstream gateway/run.py.
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_gateway_dispatch", myah_pre_gateway_dispatch)

    # ── Secrets tool registration (Phase 4c) ───────────────────────────
    ctx.register_tool(
        name="secrets",
        toolset="secrets",
        schema=secrets_tool.SCHEMA,
        handler=secrets_tool.handle,
        emoji="🔐",
        description=(
            "Securely manage API keys and other credentials without exposing "
            "values to the model."
        ),
    )

    # ── Platform adapter registration (Phase 4d) ───────────────────────
    # Local imports avoid pulling aiohttp at module-import time so the
    # plugin still loads cleanly when aiohttp is missing (the adapter's
    # check_fn handles that case below).
    from .adapter import MyahAdapter, check_myah_requirements

    ctx.register_platform(
        name="myah",
        label="🌐 Myah",
        adapter_factory=lambda cfg: MyahAdapter(cfg),
        check_fn=check_myah_requirements,
        validate_config=_validate_myah_config,
        required_env=["MYAH_ADAPTER_AUTH_KEY"],
        install_hint="pip install aiohttp",
        # Capability fields supported by upstream's PlatformEntry. Auth
        # bypass for the platform's authenticated upstream users flows
        # through ``allow_all_env`` (set via ``MYAH_ALLOW_ALL_USERS=true``
        # in the agent container's entrypoint).
        allowed_users_env="MYAH_ALLOWED_USERS",
        allow_all_env="MYAH_ALLOW_ALL_USERS",
        platform_hint=_MYAH_PLATFORM_HINT,
        # Tier 2C Issue 3: opt myah into plugin-aware cron delivery
        # validation. cron/scheduler.py:_is_known_delivery_platform()
        # consults the platform registry for plugin platforms with this
        # field set. Without this, the JOBS API rejects 'myah' origin
        # with HTTP 400.
        cron_deliver_env_var="MYAH_HOME_CHAT",
    )

    # ── PLATFORMS bridge (Tier 2C Issue 2 — workaround) ──────────────────
    # Phase 4d moved the Myah platform out of upstream's static
    # hermes_cli/platforms.py registry. Code paths in upstream's
    # hermes_cli/tools_config.py do `PLATFORMS["myah"]` direct lookup or
    # iterate `PLATFORMS.values()` — those don't see plugin-registered
    # platforms.
    #
    # WORKAROUND: mutate tools_config.PLATFORMS at register time so direct
    # lookups succeed. This is fragile (relies on tools_config.PLATFORMS
    # being a plain dict; if upstream changes it to a derived view, this
    # silently stops working). Long-term fix is U-PLAT upstream PR
    # (deferred per spec §5). Sentinel test in
    # plugins/myah-hermes-plugin/tests/test_myah_platform_bridge.py
    # catches the dict-type change.
    try:
        import hermes_cli.tools_config as _tc
        _tc.PLATFORMS["myah"] = {
            "label": "Myah",
            "default_toolset": "hermes-myah",
        }
    except ImportError:
        # tools_config is CLI-only; in pure-gateway runtime, skip silently.
        pass
    # ────────────────────────────────────────────────────────────────────
