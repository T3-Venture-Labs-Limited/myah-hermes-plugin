"""Sentry breadcrumb/tag emission for the four primary Hermes lifecycle hooks.

Each hook follows the same shape:

  1. Defensive ``**kwargs`` capture — upstream kwarg lists vary between
     versions (verified by reading the four call sites in
     ``run_agent.py`` / ``model_tools.py``), so any positional refactor
     upstream will not break us.

  2. Best-effort ``sentry_sdk`` import inside the function — when
     ``SENTRY_DSN_AGENT`` is unset the SDK is uninitialized and
     ``add_breadcrumb`` is a no-op, so we never need to gate on env
     vars here. When the import itself fails (sentry_sdk uninstalled
     in OSS-minimal mode), we swallow and return.

  3. Never raise — a buggy breadcrumb must not abort the agent run.
     The plugin manager already wraps hooks in a try/except, but we
     belt-and-suspenders ourselves so even an SDK upgrade that
     temporarily breaks ``add_breadcrumb`` semantics can't take the
     run with it.

Hook signatures match upstream as of run_agent.py @ submodule SHA
fade03f19 (parent master @ 4b211fc4f8).

  pre_api_request (run_agent.py:11543):
      task_id, session_id, platform, model, provider, base_url,
      api_mode, api_call_count, message_count, tool_count,
      approx_input_tokens, request_char_count, max_tokens

  post_api_request (run_agent.py:13361):
      task_id, session_id, platform, model, provider, base_url,
      api_mode, api_call_count, api_duration, finish_reason,
      message_count, response_model, usage,
      assistant_content_chars, assistant_tool_call_count

  pre_tool_call (model_tools.py:728 via get_pre_tool_call_block_message):
      tool_name, args, task_id, session_id, tool_call_id

  post_tool_call (model_tools.py:776):
      tool_name, args, result, task_id, session_id, tool_call_id,
      duration_ms

The plan's Task 4 verification: a hook must keep working even when
upstream adds new kwargs. The ``**kwargs`` capture below absorbs them
without raising; tests exercise that explicitly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _sentry_sdk():
    """Lazy import — returns ``sentry_sdk`` if installed, else None.

    Sentry SDK is an optional dep; OSS-minimal installs may omit it.
    Hosted Myah agent containers always have it. Returning None lets
    the four hooks below short-circuit cleanly.
    """
    try:
        import sentry_sdk

        return sentry_sdk
    except ImportError:
        return None


def _truncate(value: Any, limit: int = 200) -> str:
    """Coerce a value to a printable string and truncate for breadcrumb size.

    Sentry breadcrumb data should be small enough that it doesn't dominate
    the event payload — the SDK truncates aggressively past ~8KB, but tool
    args and provider responses can be much larger than that. Truncate
    upfront so the breadcrumb stays useful even for big payloads.
    """
    if value is None:
        return ""
    s = str(value)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…[truncated, {len(s)} chars total]"


def myah_pre_api_request(**kwargs: Any) -> Optional[dict]:
    """Emit a Sentry breadcrumb before each LLM API call.

    Captures the request context (provider, model, api_call_count) so the
    breadcrumb trail in any subsequent error includes which iteration of
    which model on which session was about to fire. Sentry's
    ``OpenAIIntegration`` / ``AnthropicIntegration`` capture the HTTP
    request itself; this breadcrumb joins it to the Myah session/task.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return None
    try:
        sdk.add_breadcrumb(
            category="hermes.api_request",
            level="info",
            message=(
                f"LLM request → provider={kwargs.get('provider') or '?'} "
                f"model={kwargs.get('model') or '?'} "
                f"iter={kwargs.get('api_call_count', 0)}"
            ),
            data={
                "platform": kwargs.get("platform", ""),
                "session_id": kwargs.get("session_id", ""),
                "task_id": kwargs.get("task_id", ""),
                "provider": kwargs.get("provider", ""),
                "model": kwargs.get("model", ""),
                "base_url": kwargs.get("base_url", ""),
                "api_mode": kwargs.get("api_mode", ""),
                "api_call_count": kwargs.get("api_call_count", 0),
                "message_count": kwargs.get("message_count", 0),
                "tool_count": kwargs.get("tool_count", 0),
                "approx_input_tokens": kwargs.get("approx_input_tokens", 0),
                "request_char_count": kwargs.get("request_char_count", 0),
                "max_tokens": kwargs.get("max_tokens"),
            },
        )
        # Tag the current scope with provider/model so subsequent
        # exceptions captured during this turn are auto-grouped by
        # which provider was in use.
        sdk.set_tag("hermes.provider", kwargs.get("provider") or "unknown")
        sdk.set_tag("hermes.model", kwargs.get("model") or "unknown")
    except Exception:  # pragma: no cover — never raise from a hook
        logger.exception("[myah-sentry] pre_api_request breadcrumb failed")
    return None


def myah_post_api_request(**kwargs: Any) -> Optional[dict]:
    """Emit a Sentry breadcrumb after each LLM API call.

    Captures latency, finish_reason, and usage so Sentry breadcrumb trails
    for downstream errors show what the LLM actually returned. ``usage`` is
    whatever shape ``_usage_summary_for_api_request_hook`` returns upstream
    (typically a dict with input/output tokens); we forward it as-is via
    ``_truncate``.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return None
    try:
        finish_reason = kwargs.get("finish_reason") or "?"
        api_duration = kwargs.get("api_duration", 0.0)
        sdk.add_breadcrumb(
            category="hermes.api_request",
            level="info" if finish_reason in ("stop", "tool_calls", "length") else "warning",
            message=(
                f"LLM response ← provider={kwargs.get('provider') or '?'} "
                f"finish={finish_reason} "
                f"duration={api_duration:.2f}s "
                f"tool_calls={kwargs.get('assistant_tool_call_count', 0)}"
            ),
            data={
                "platform": kwargs.get("platform", ""),
                "session_id": kwargs.get("session_id", ""),
                "task_id": kwargs.get("task_id", ""),
                "provider": kwargs.get("provider", ""),
                "model": kwargs.get("model", ""),
                "response_model": kwargs.get("response_model"),
                "api_call_count": kwargs.get("api_call_count", 0),
                "api_duration": api_duration,
                "finish_reason": finish_reason,
                "usage": _truncate(kwargs.get("usage"), limit=400),
                "assistant_content_chars": kwargs.get("assistant_content_chars", 0),
                "assistant_tool_call_count": kwargs.get("assistant_tool_call_count", 0),
            },
        )
    except Exception:  # pragma: no cover
        logger.exception("[myah-sentry] post_api_request breadcrumb failed")
    return None


def myah_pre_tool_call(**kwargs: Any) -> Optional[dict]:
    """Emit a Sentry breadcrumb before each tool dispatch.

    The ``args`` payload can be large (e.g. ``execute_code`` may pass a
    multi-KB script); ``_truncate`` keeps the breadcrumb compact. We do
    NOT add the tool name to the scope as a tag — tool calls are nested
    inside an LLM turn, so the outer provider/model tag from
    pre_api_request is the appropriate grouping key.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return None
    try:
        tool_name = kwargs.get("tool_name") or "?"
        sdk.add_breadcrumb(
            category="hermes.tool_call",
            level="info",
            message=f"Tool → {tool_name}",
            data={
                "tool_name": tool_name,
                "tool_call_id": kwargs.get("tool_call_id", ""),
                "task_id": kwargs.get("task_id", ""),
                "session_id": kwargs.get("session_id", ""),
                "args": _truncate(kwargs.get("args"), limit=400),
            },
        )
    except Exception:  # pragma: no cover
        logger.exception("[myah-sentry] pre_tool_call breadcrumb failed")
    return None


def myah_post_tool_call(**kwargs: Any) -> Optional[dict]:
    """Emit a Sentry breadcrumb after each tool dispatch.

    Captures duration and a truncated result so the breadcrumb trail in
    a downstream error shows what the tool actually returned. Tools that
    return JSON error payloads (``{"error": ...}``) are NOT promoted to
    Sentry-level errors here — that's the calling code's responsibility,
    and ``transform_tool_result`` plugins may rewrite the result anyway.
    The breadcrumb level stays ``info`` even on error payloads so we
    don't double-count what may be handled upstream.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return None
    try:
        tool_name = kwargs.get("tool_name") or "?"
        duration_ms = kwargs.get("duration_ms", 0)
        sdk.add_breadcrumb(
            category="hermes.tool_call",
            level="info",
            message=f"Tool ← {tool_name} ({duration_ms}ms)",
            data={
                "tool_name": tool_name,
                "tool_call_id": kwargs.get("tool_call_id", ""),
                "task_id": kwargs.get("task_id", ""),
                "session_id": kwargs.get("session_id", ""),
                "duration_ms": duration_ms,
                "result": _truncate(kwargs.get("result"), limit=400),
            },
        )
    except Exception:  # pragma: no cover
        logger.exception("[myah-sentry] post_tool_call breadcrumb failed")
    return None


def register_sentry_hooks(ctx: Any) -> None:
    """Wire the four observability hooks into the plugin context.

    Idempotent — silently no-op when ``ctx`` lacks ``register_hook``
    (older Hermes builds, or a test context that mocks the API).

    Note on ordering: this should be called AFTER ``setup_sentry()`` so
    that the SDK is initialized by the time the first breadcrumb fires.
    In practice both fire at plugin-register time and the SDK is
    process-global, so ordering is academic — but documenting the
    intent here makes future refactors safer.
    """
    if not hasattr(ctx, "register_hook"):
        return

    ctx.register_hook("pre_api_request", myah_pre_api_request)
    ctx.register_hook("post_api_request", myah_post_api_request)
    ctx.register_hook("pre_tool_call", myah_pre_tool_call)
    ctx.register_hook("post_tool_call", myah_post_tool_call)
    logger.info(
        "Myah plugin: registered 4 Sentry observability hooks "
        "(pre/post_api_request, pre/post_tool_call)"
    )
