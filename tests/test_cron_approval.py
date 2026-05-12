"""Tests for the plugin-vendored action confirmation primitives.

Mirrors the upstream ``tools/approval.py`` semantics with the same
synchronous, ``threading.Event``-based contract that
``cronjob_tools._execute()`` depends on.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest

from myah_hermes_plugin import cron_approval, dispatcher


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset module state between tests."""
    dispatcher._registered_callbacks.clear()
    cron_approval._action_queues.clear()
    yield
    dispatcher._registered_callbacks.clear()
    cron_approval._action_queues.clear()


@pytest.fixture
def fake_session(monkeypatch):
    """Force a stable session_key from the helper so tests don't depend
    on whatever upstream's contextvar resolves to in CI."""
    monkeypatch.setattr(cron_approval, "_get_current_session_key", lambda: "sess-1")
    return "sess-1"


def _register_capturing_callback(
    session_key: str, captured: List[Dict[str, Any]]
) -> None:
    def cb(_session_key, payload, metadata=None):
        captured.append({"session_key": _session_key, "payload": payload, "metadata": metadata})

    dispatcher.register_gateway_notify(session_key, cb)


def test_request_action_confirmation_returns_id_and_stages_entry(fake_session):
    """A pending entry is created keyed on the returned confirmation_id."""
    captured: List[Dict[str, Any]] = []
    _register_capturing_callback(fake_session, captured)

    # Run the request in a worker thread (it blocks on event.wait); the
    # main thread inspects state and resolves it.
    cid_holder: List[str] = []

    def _do_request():
        cid = cron_approval.request_action_confirmation(
            action_type="cron_create",
            description="x",
            options=["approve", "deny"],
            metadata={"job_id": "abc"},
            timeout=2.0,
        )
        cid_holder.append(cid)

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()

    # Wait until the entry is staged on _action_queues. The notify
    # callback fires before event.wait(), so we can use the captured
    # payload to grab the confirmation_id.
    deadline = threading.Event()

    def _wait_for_capture():
        for _ in range(200):
            if captured:
                deadline.set()
                return
            threading.Event().wait(0.005)

    _wait_for_capture()
    assert captured, "notify callback was not invoked"
    confirmation_id = captured[0]["payload"]["confirmation_id"]
    assert confirmation_id in cron_approval._action_queues
    entry = cron_approval._action_queues[confirmation_id]
    assert entry["session_key"] == fake_session
    assert entry["metadata"] == {"job_id": "abc"}

    # Resolve to let the worker thread terminate cleanly. The function
    # returns the choice string, so cid_holder ends up holding the
    # resolved choice (we just want to confirm the worker unblocked).
    assert cron_approval.resolve_action_confirmation(confirmation_id, "approve") is True
    t.join(timeout=2.0)
    assert cid_holder == ["approve"]


def test_resolve_action_confirmation_unblocks_waiter(fake_session):
    """resolve_action_confirmation puts the choice on the entry and
    signals the event so the awaiting tool wakes up."""
    captured: List[Dict[str, Any]] = []
    _register_capturing_callback(fake_session, captured)

    result_holder: List[str] = []

    def _do_request():
        choice = cron_approval.request_action_confirmation(
            action_type="cron_create",
            description="x",
            options=["approve", "deny"],
            timeout=2.0,
        )
        result_holder.append(choice)

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()

    # Spin until the callback fires.
    for _ in range(200):
        if captured:
            break
        threading.Event().wait(0.005)
    assert captured, "notify callback was not invoked"

    cid = captured[0]["payload"]["confirmation_id"]
    assert cron_approval.resolve_action_confirmation(cid, "approve") is True
    t.join(timeout=2.0)
    assert result_holder == ["approve"]


def test_resolve_action_confirmation_by_session_resolves_oldest(fake_session):
    """resolve_action_confirmation_by_session resolves the most recent
    pending request for the session (FIFO)."""
    captured: List[Dict[str, Any]] = []
    _register_capturing_callback(fake_session, captured)

    results: List[str] = []

    def _do_request():
        results.append(
            cron_approval.request_action_confirmation(
                action_type="cron_create",
                description="x",
                options=["approve", "deny"],
                timeout=2.0,
            )
        )

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()

    for _ in range(200):
        if captured:
            break
        threading.Event().wait(0.005)
    assert captured

    n = cron_approval.resolve_action_confirmation_by_session(fake_session, "deny")
    assert n == 1
    t.join(timeout=2.0)
    assert results == ["deny"]


def test_metadata_is_preserved_on_payload(fake_session):
    """Metadata passed to request_action_confirmation is attached to
    both the queue entry AND the dispatched payload."""
    captured: List[Dict[str, Any]] = []
    _register_capturing_callback(fake_session, captured)

    def _do_request():
        cron_approval.request_action_confirmation(
            action_type="cron_create",
            description="x",
            options=["approve", "deny"],
            metadata={"job_id": "abc", "schedule": "0 9 * * *"},
            timeout=2.0,
        )

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()

    for _ in range(200):
        if captured:
            break
        threading.Event().wait(0.005)
    assert captured

    payload = captured[0]["payload"]
    assert payload["metadata"] == {"job_id": "abc", "schedule": "0 9 * * *"}
    cid = payload["confirmation_id"]
    assert cron_approval._action_queues[cid]["metadata"] == {
        "job_id": "abc",
        "schedule": "0 9 * * *",
    }

    # Cleanup so the worker thread terminates.
    cron_approval.resolve_action_confirmation(cid, "approve")
    t.join(timeout=2.0)


# -----------------------------------------------------------------------------
# Sync tests for the plugin's cron_tool shadow (Task 2A.2.2).
# These will FAIL until cron_tool.py is added.
# -----------------------------------------------------------------------------


def test_plugin_cron_tool_imports_request_action_confirmation_from_plugin():
    """The plugin's cron_tool MUST import ``request_action_confirmation``
    from ``myah_hermes_plugin.cron_approval`` (not
    ``tools.approval``).
    """
    from myah_hermes_plugin.myah_tools import cron_tool

    src = open(cron_tool.__file__, encoding="utf-8").read()
    assert "from myah_hermes_plugin.cron_approval" in src, (
        "cron_tool.py must import request_action_confirmation from the "
        "plugin-vendored module, not tools.approval"
    )
    # No survivors of the upstream import path.
    assert "from tools.approval import" not in src


def test_plugin_cron_tool_exports_callable():
    """The plugin's cron_tool must register the ``cronjob`` action tool
    via ``tools.registry.registry`` so importing the plugin module
    shadows upstream's tool of the same name (last-writer-wins).
    """
    # Importing the module triggers registry.register side-effects.
    from myah_hermes_plugin.myah_tools import cron_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("cronjob")
    assert entry is not None, "plugin's cron_tool did not register 'cronjob'"
    assert callable(entry.handler), "registered cronjob handler is not callable"
