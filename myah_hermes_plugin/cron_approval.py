"""Plugin-owned action confirmation primitives.

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
    timeout: float = 300.0,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Block until the user confirms or denies a proposed action.

    Synchronous, threading-based — designed to be called from a tool
    handler running in the agent's tool-runner threadpool.

    Auto-approves immediately if no notify callback is registered for
    the current session (non-interactive contexts: tests, CLI, cron
    sub-agent).

    Returns
    -------
    str
        The chosen option (e.g. ``"approve"``, ``"deny"``,
        ``"approve_session"``).  ``"deny"`` on timeout.
    """
    if options is None:
        options = ["approve", "deny"]

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
    with _lock:
        _action_queues.pop(confirmation_id, None)

    if not resolved:
        log.warning(
            "request_action_confirmation timed out after %.0fs for %s",
            timeout,
            action_type,
        )
        return "deny"

    choice = result_holder[0] if result_holder else "deny"
    log.info(
        "request_action_confirmation resolved: action=%s choice=%s",
        action_type,
        choice,
    )
    return choice


def resolve_action_confirmation(confirmation_id: str, choice: str) -> bool:
    """Resolve a pending action confirmation by id.

    Called by the gateway's confirm endpoint.  Returns ``True`` if the
    confirmation was resolved, ``False`` if the id is unknown or already
    resolved.
    """
    with _lock:
        entry = _action_queues.get(confirmation_id)
    if entry is None:
        return False
    entry["result"].append(choice)
    entry["event"].set()
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

    Returns the number of confirmations resolved (0 means nothing
    pending for this session).
    """
    with _lock:
        # Insertion order is FIFO (Python 3.7+ dict guarantee).
        matching = [
            (cid, entry)
            for cid, entry in _action_queues.items()
            if entry.get("session_key") == session_key
        ]
        if not matching:
            return 0
        targets = matching if resolve_all else matching[:1]
        for cid, _ in targets:
            _action_queues.pop(cid, None)

    for _cid, entry in targets:
        entry["result"].append(choice)
        entry["event"].set()
    return len(targets)


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
