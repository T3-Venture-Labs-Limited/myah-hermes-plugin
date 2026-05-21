"""Plugin-owned action confirmation primitives.

⚠️ **DEAD CODE in production as of 2026-05-21.**

This module is only called by ``myah_hermes_plugin.myah_tools.cron_tool``,
which is itself not loaded in production (the plugin's entry point
never imports it). Upstream's ``tools/cronjob_tools.py`` runs the
cron tool in production and has no approval mechanism — so this
confirmation primitive is currently inert.

See ``docs/gotchas/2026-05-21-plugin-cron-tool-not-loaded.md``
(in the hosted repo) for the full root-cause analysis and
re-activation recipe.

------------------------------------------------------------------------

Vendored from upstream's ``tools/approval.py`` (lines ~470-606).  The
plugin maintains its own copy because upstream's ``cronjob_tools.py``
imports ``request_action_confirmation`` directly from
``tools.approval``; intercepting that import would require
monkey-patching ``tools.approval`` (rejected in spec
2026-05-06 §3 Task 2A.2 Option A as fragile, and forbidden by the
Teknium May 2026 plugin rule).

The plugin's ``cron_tool.py`` (Task 2A.2.2) imports
``request_action_confirmation`` from THIS module and shadows the
upstream cron tool by registering itself last on the same tool name.

**Why ``threading.Event`` and not ``asyncio.Queue``:**
``cronjob_tools._execute()`` is invoked from the agent's tool-runner
threadpool; it calls ``request_action_confirmation`` synchronously and
blocks the worker thread on ``event.wait(timeout)``.  Mirroring that
contract here avoids rewriting the cron tool and keeps the
plugin/upstream surfaces interchangeable.

Lifecycle:

1. The agent calls a plugin-registered cron tool (Task 2A.2.2).
2. That tool calls :func:`request_action_confirmation` (this module).
3. A pending entry is created keyed on a generated ``confirmation_id``
   carrying a ``threading.Event`` and a ``result`` slot.
4. The plugin's dispatcher
   (``myah_hermes_plugin.dispatcher._dispatch_approval_notify``) routes
   a ``tool.confirmation_required`` payload to the per-session notify
   callback, which emits an SSE event the platform UI renders as a
   confirmation card.
5. User clicks Approve / Deny → platform posts to
   ``/myah/v1/admin/confirm`` (or ``/approve`` ``/deny`` in chat).
6. The platform's HTTP handler calls
   :func:`resolve_action_confirmation` (or
   :func:`resolve_action_confirmation_by_session`) which fills in the
   choice and signals the event.
7. The cron tool's ``event.wait()`` returns and the tool proceeds.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from myah_hermes_plugin.dispatcher import (
    _dispatch_approval_notify,
    _registered_callbacks,
)

log = logging.getLogger(__name__)


# Module-level state. Per the isolation invariant in spec §3 Task 2A.5,
# the plugin assumes one process per user (myah-agent-<user_id>
# container), so module state is per-user state. If the deployment
# topology changes, re-key these dicts by user_id.
_lock = threading.Lock()
_action_queues: Dict[str, Dict[str, Any]] = {}  # confirmation_id → entry

# How long a resolved confirmation entry lingers in ``_action_queues``
# before its expiry Timer pops it. Window for idempotent re-clicks /
# slow network retries so the confirm endpoint returns the cached
# choice instead of 404. Per spec
# docs/superpowers/specs/2026-05-19-oss-post-launch-reliability-design.md
# §5.1.
_RESOLVED_TTL_SECONDS: float = 10.0


def _get_gateway_timeout() -> int:
    """Read ``approvals.gateway_timeout`` from ``~/.hermes/config.yaml``.

    Per spec-review HIGH-3: align with upstream's
    ``tools/approval.py:1244`` which reads
    ``_get_approval_config().get('gateway_timeout', 300)``. Default
    1800 here so the in-chat approval card stays valid for a
    long-running turn even without operator config.

    Defensive: any read/parse failure (missing file, malformed YAML,
    unexpected type) returns the 1800 default rather than raising —
    the plugin must never break message dispatch over a config quirk.
    """
    try:
        from hermes_constants import get_hermes_home
        import yaml

        cfg_path = get_hermes_home() / "config.yaml"
        if not cfg_path.exists():
            return 1800
        with cfg_path.open() as f:
            cfg = yaml.safe_load(f) or {}
        if not isinstance(cfg, dict):
            return 1800
        approvals = cfg.get("approvals", {})
        if not isinstance(approvals, dict):
            return 1800
        return int(approvals.get("gateway_timeout", 1800))
    except Exception:  # noqa: BLE001 — defensive default
        return 1800


def _get_current_session_key() -> str:
    """Look up the current thread's session_key.

    Mirrors upstream's ``tools.approval.get_current_session_key`` which
    reads from a contextvar set by the agent runner.  The plugin
    delegates to upstream for this — the contextvar lives in the same
    process and the read is purely informational.
    """
    try:
        # Imported lazily so this module stays importable in environments
        # where tools/approval.py isn't on the path (e.g. unit tests).
        from tools.approval import get_current_session_key  # type: ignore

        return get_current_session_key()
    except Exception:  # noqa: BLE001
        return ""


def request_action_confirmation(
    action_type: str,
    description: str,
    options: Optional[List[str]] = None,
    timeout: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Block until the user confirms or denies a proposed action.

    Synchronous, threading-based — designed to be called from a tool
    handler running in the agent's tool-runner threadpool.

    Auto-approves immediately if no notify callback is registered for
    the current session (non-interactive contexts: tests, CLI, cron
    sub-agent).

    ``timeout``
        When omitted (``None``), resolved at request time from
        :func:`_get_gateway_timeout`. Resolving lazily (rather than as
        a function default) lets tests monkeypatch the helper after the
        module is imported.

    Returns
    -------
    str
        The chosen option (e.g. ``"approve"``, ``"deny"``,
        ``"approve_session"``).  ``"deny"`` on timeout.
    """
    if options is None:
        options = ["approve", "deny"]

    if timeout is None:
        timeout = float(_get_gateway_timeout())

    session_key = _get_current_session_key()

    # If no callback is bound, auto-approve and bail early.
    with _registered_callbacks_lock_proxy():
        callback = _registered_callbacks.get(session_key)
    if callback is None:
        log.debug(
            "request_action_confirmation: no gateway callback for %r, auto-approve",
            session_key,
        )
        return "approve"

    confirmation_id = str(uuid.uuid4())
    event = threading.Event()
    result_holder: List[str] = []

    with _lock:
        _action_queues[confirmation_id] = {
            "session_key": session_key,
            "action_type": action_type,
            "description": description,
            "options": options,
            "event": event,
            "result": result_holder,
            "metadata": metadata or {},
            # Stored for observability + so tests can assert the default
            # timeout was sourced from _get_gateway_timeout().
            "timeout": timeout,
        }

    # Notify the gateway adapter so it can emit an SSE event to the
    # frontend.  Build the payload with the modern shape the plugin's
    # adapter expects.
    payload: Dict[str, Any] = {
        "type": "tool.confirmation_required",
        "confirmation_id": confirmation_id,
        "action_type": action_type,
        "description": description,
        "options": options,
    }
    if metadata:
        payload["metadata"] = metadata

    _dispatch_approval_notify(session_key, payload, metadata=metadata)

    # Block until the gateway resolves the confirmation or we time out.
    resolved = event.wait(timeout=timeout)

    if not resolved:
        # Timeout — no cached choice to keep; pop immediately so the
        # entry doesn't linger.
        with _lock:
            _action_queues.pop(confirmation_id, None)
        log.warning(
            "request_action_confirmation timed out after %.0fs for %s",
            timeout,
            action_type,
        )
        return "deny"

    # Resolved by an external thread (resolve_action_confirmation* set
    # the event). The choice is already in result_holder; we leave the
    # entry in _action_queues for _RESOLVED_TTL_SECONDS so a duplicate
    # /confirm request (slow network, double-click) finds the cached
    # choice instead of 404.
    _schedule_resolved_expiry(confirmation_id)

    choice = result_holder[0] if result_holder else "deny"
    log.info(
        "request_action_confirmation resolved: action=%s choice=%s",
        action_type,
        choice,
    )
    return choice


def _schedule_resolved_expiry(confirmation_id: str) -> None:
    """Pop ``confirmation_id`` from ``_action_queues`` after the TTL.

    Uses a daemon ``threading.Timer`` so the agent can shut down
    cleanly even if the timer is still pending.
    """

    def _expire() -> None:
        with _lock:
            _action_queues.pop(confirmation_id, None)

    timer = threading.Timer(_RESOLVED_TTL_SECONDS, _expire)
    timer.daemon = True
    timer.start()


def resolve_action_confirmation(confirmation_id: str, choice: str) -> bool:
    """Resolve a pending action confirmation by id.

    Called by the gateway's confirm endpoint.

    Returns ``True`` if the confirmation was resolved OR was already
    resolved within the cache window (``_RESOLVED_TTL_SECONDS``). An
    idempotent re-resolve is treated as a successful no-op so a
    duplicate user click doesn't 404 the second request.

    Returns ``False`` only if the id is unknown (never staged, or
    already expired past the cache window).
    """
    with _lock:
        entry = _action_queues.get(confirmation_id)
        if entry is None:
            return False
        if entry["result"]:
            # Already resolved and still inside the TTL window. Treat as
            # idempotent success — the original choice stands.
            log.debug(
                "resolve_action_confirmation: id=%s already resolved to %r (idempotent no-op)",
                confirmation_id,
                entry["result"][0],
            )
            return True
        entry["result"].append(choice)
        event = entry["event"]
    event.set()
    return True


def resolve_action_confirmation_by_session(
    session_key: str,
    choice: str,
    resolve_all: bool = False,
) -> int:
    """Resolve the oldest pending action confirmation for ``session_key``.

    When ``resolve_all`` is ``True`` resolves every pending confirmation
    for the session.  Used by gateway ``/approve``/``/deny`` handlers
    so chat-based approvals work for action confirmations without
    requiring the user to know the confirmation_id.

    Entries that have already been resolved (still in the TTL cache
    from §5.1) are skipped — by-session resolution targets only
    truly-pending requests.

    Returns the number of confirmations resolved (0 means nothing
    pending for this session).
    """
    with _lock:
        # Snapshot inside the lock per plan-review C-2. Insertion order
        # is FIFO (Python 3.7+ dict guarantee). Skip entries already in
        # the resolved-TTL cache.
        snapshot = list(_action_queues.items())
        matching = [
            (cid, entry)
            for cid, entry in snapshot
            if entry.get("session_key") == session_key and not entry.get("result")
        ]
        if not matching:
            return 0
        targets = matching if resolve_all else matching[:1]
        for _cid, entry in targets:
            entry["result"].append(choice)

    for _cid, entry in targets:
        entry["event"].set()
        _schedule_resolved_expiry(_cid)
    return len(targets)


def _has_pending_approvals(session_key: str) -> bool:
    """Return True iff any entry in ``_action_queues`` belongs to
    ``session_key`` and has NOT yet been resolved.

    Used by ``adapter._dispatch_message`` (Task 1.3) to decide whether
    cleaning up the dual session→stream mapping is safe yet. If a
    pending approval exists the mapping must stay live so the eventual
    ``POST /myah/v1/confirm/{stream_id}`` can resolve back to a
    session_key and reach the threading.Event.

    Belt-and-braces (plan-review H-3): we also peek at upstream's
    ``tools.approval._gateway_queues`` in case an upstream-vendored
    approval flow staged an entry there. Import failures are logged at
    INFO and tolerated — the plugin must continue working when run
    against a hermes build that refactors the internal queue name.

    All dict iteration is performed against an in-lock snapshot
    (plan-review C-2).
    """
    with _lock:
        snapshot = dict(_action_queues)

    for entry in snapshot.values():
        if entry.get("session_key") == session_key and not entry.get("result"):
            return True

    # Belt-and-braces: also check upstream's queue. Import + read
    # failures are tolerated.
    try:
        from tools.approval import _gateway_queues as _upstream_gateway_queues  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.info(
            "_has_pending_approvals: upstream _gateway_queues unavailable (%s); "
            "relying on plugin queue only",
            exc,
        )
        return False

    try:
        upstream_snapshot = dict(_upstream_gateway_queues)
    except Exception as exc:  # noqa: BLE001
        log.info(
            "_has_pending_approvals: failed to snapshot upstream _gateway_queues (%s); "
            "treating as empty",
            exc,
        )
        return False

    for entry in upstream_snapshot.values():
        # Upstream entry shape is a dict with at least 'session_key'.
        # If the contract drifts we silently skip — better to miss a
        # belt-and-braces signal than to crash the dispatcher.
        try:
            if (
                isinstance(entry, dict)
                and entry.get("session_key") == session_key
                and not entry.get("result")
            ):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _NoLockProxy:
    """Re-export the dispatcher's lock without leaking the import.

    The dispatcher already serializes its registry mutations with its
    own lock.  We just want to hold THAT lock briefly when peeking at
    ``_registered_callbacks`` so the read is consistent.
    """

    def __enter__(self):  # noqa: D401 - context-manager protocol
        from myah_hermes_plugin.dispatcher import _lock as dispatcher_lock

        self._lock = dispatcher_lock
        self._lock.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._lock.__exit__(exc_type, exc, tb)


def _registered_callbacks_lock_proxy() -> _NoLockProxy:
    return _NoLockProxy()
