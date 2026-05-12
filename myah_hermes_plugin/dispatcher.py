"""Plugin-vendored approval-notify dispatcher.

Vendored from upstream's ``gateway/run.py:_dispatch_approval_notify``
(~lines 334-460). The plugin owns its own copy because upstream's
notify chain is the only transport from ``request_action_confirmation``
back to the platform adapter; if the plugin doesn't vendor this, the
agent auto-approves silently when the platform's notify callback isn't
reachable.

Lifecycle:

1. Plugin's adapter calls :func:`register_gateway_notify` (session_key, fn)
   when a session starts.
2. The agent calls
   ``myah_hermes_plugin.cron_approval.request_action_confirmation(...)``.
3. That function invokes
   ``myah_hermes_plugin.dispatcher._dispatch_approval_notify(session_key,
   payload)`` which looks up the registered fn and invokes it with the
   right arity.
4. ``fn`` (the adapter's notify callback) translates the request into a
   platform SSE event → user sees the confirmation card → user clicks
   Approve → adapter posts to ``/myah/v1/admin/confirm`` →
   ``cron_approval.resolve_action_confirmation`` →
   ``threading.Event.set()`` → agent thread unblocks.

Spec: ``docs/superpowers/specs/2026-05-06-myah-oss-completion-design.md``
§3 Task 2A.2 (vendor cron approval primitives).
"""

from __future__ import annotations

import inspect
import logging
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)


# Module-level registry of session-keyed notify callbacks.
# Last-writer-wins on re-registration (matches upstream semantics in
# tools/approval.py where `_gateway_notify_cbs[session_key] = cb`).
_registered_callbacks: Dict[str, Callable[..., Any]] = {}
_lock = threading.Lock()


def register_gateway_notify(session_key: str, fn: Callable[..., Any]) -> None:
    """Register or replace the notify callback for a session.

    Re-registering for the same ``session_key`` REPLACES the prior callback
    (matches upstream's last-writer-wins semantics).
    """
    with _lock:
        _registered_callbacks[session_key] = fn


def unregister_gateway_notify(session_key: str) -> None:
    """Drop a session's notify callback. No-op if not registered.

    Idempotent — the adapter's session-cleanup path may invoke this multiple
    times (run completion, run failure, gateway shutdown).
    """
    with _lock:
        _registered_callbacks.pop(session_key, None)


def _dispatch_approval_notify(
    session_key: str,
    request: Dict[str, Any],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Dispatch an approval request to the session's registered callback.

    Variadic dispatch — the callback may have one of three signatures:

    * ``cb(request)`` — single-arity (legacy single-payload form)
    * ``cb(session_key, request)`` — two-arity (modern form used by
      :func:`myah_hermes_plugin.cron_approval.request_action_confirmation`)
    * ``cb(session_key, request, metadata=...)`` — three-arity with
      optional metadata kwarg

    The dispatcher inspects the callback's signature and routes
    appropriately. Silent no-op if no callback is registered for the
    ``session_key`` — this matches upstream's behavior, where
    ``request_action_confirmation`` auto-approves when no callback is
    bound (e.g. CLI, cron sub-agent).

    Never raises — exceptions in the callback are logged at WARNING
    level and swallowed.  The original upstream caller in
    ``tools/approval.py`` also swallows exceptions; preserving that
    contract avoids surprising behavior at the call site.
    """
    with _lock:
        fn = _registered_callbacks.get(session_key)

    if fn is None:
        log.debug(
            "_dispatch_approval_notify: no callback for session_key=%r", session_key
        )
        return

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Builtins / C-extensions may refuse signature introspection.
        # Default to the modern two-arity shape.
        try:
            fn(session_key, request)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "_dispatch_approval_notify: callback for session=%r raised: %s",
                session_key,
                exc,
            )
        return

    # Count positional/keyword params we can supply (drop 'self' on
    # bound methods automatically — inspect does that).
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]

    try:
        if len(positional) <= 1:
            fn(request)
        elif len(positional) == 2:
            fn(session_key, request)
        else:
            # 3+ positional → also pass metadata.
            fn(session_key, request, metadata=metadata)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_dispatch_approval_notify: callback for session=%r raised: %s",
            session_key,
            exc,
        )
