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


# -----------------------------------------------------------------------------
# Phase 1 PR 1 (spec §5.1) — extend approval timeout + cache resolved choice.
# -----------------------------------------------------------------------------


def test_request_action_confirmation_default_timeout_is_gateway_aligned(
    fake_session, monkeypatch
):
    """The default timeout for an in-chat approval must come from
    ``_get_gateway_timeout()`` (default 1800s) rather than the legacy
    300s constant. Aligns with upstream tools/approval.py:1244.
    """
    monkeypatch.setattr(cron_approval, "_get_gateway_timeout", lambda: 1800)

    captured: List[Dict[str, Any]] = []
    _register_capturing_callback(fake_session, captured)

    def _do_request():
        cron_approval.request_action_confirmation(
            action_type="cron_create",
            description="x",
            # NOTE: omitting `timeout` to exercise the default path.
        )

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()

    for _ in range(200):
        if captured:
            break
        threading.Event().wait(0.005)
    assert captured, "notify callback was not invoked"

    cid = captured[0]["payload"]["confirmation_id"]
    entry = cron_approval._action_queues[cid]
    assert entry["timeout"] == 1800, (
        f"expected gateway-aligned default timeout=1800, got {entry.get('timeout')!r}"
    )

    # Cleanup
    cron_approval.resolve_action_confirmation(cid, "approve")
    t.join(timeout=2.0)


def test_get_gateway_timeout_reads_approvals_block(tmp_path, monkeypatch):
    """``_get_gateway_timeout`` reads ``approvals.gateway_timeout`` from
    ``~/.hermes/config.yaml``; defaults to 1800 if missing or malformed.
    """
    import yaml

    hermes_home = tmp_path / "hermes_home_cfg"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    cfg_path = hermes_home / "config.yaml"

    # 1. With approvals.gateway_timeout=600 — returns 600.
    cfg_path.write_text(yaml.safe_dump({"approvals": {"gateway_timeout": 600}}))
    assert cron_approval._get_gateway_timeout() == 600

    # 2. With approvals block absent — returns 1800 default.
    cfg_path.write_text(yaml.safe_dump({"other": {"foo": "bar"}}))
    assert cron_approval._get_gateway_timeout() == 1800

    # 3. With malformed YAML — defensive default 1800.
    cfg_path.write_text("::: not yaml :::\n  - [unclosed\n")
    assert cron_approval._get_gateway_timeout() == 1800

    # 4. Missing config.yaml — returns 1800.
    cfg_path.unlink()
    assert cron_approval._get_gateway_timeout() == 1800


def test_resolved_confirmation_cached_for_10s(fake_session, monkeypatch):
    """After ``event.set()`` resolves a confirmation, the entry stays in
    ``_action_queues`` for ~10s so an idempotent re-resolve returns the
    cached choice instead of a 404."""
    # Shrink the TTL to keep the test fast.
    monkeypatch.setattr(cron_approval, "_RESOLVED_TTL_SECONDS", 0.2)

    captured: List[Dict[str, Any]] = []
    _register_capturing_callback(fake_session, captured)

    def _do_request():
        cron_approval.request_action_confirmation(
            action_type="cron_create",
            description="x",
            options=["approve", "deny"],
            timeout=5.0,
        )

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()

    for _ in range(200):
        if captured:
            break
        threading.Event().wait(0.005)
    assert captured

    cid = captured[0]["payload"]["confirmation_id"]
    assert cron_approval.resolve_action_confirmation(cid, "approve") is True
    t.join(timeout=2.0)

    # Idempotent re-resolve returns the cached choice and reports True.
    assert cid in cron_approval._action_queues, (
        "resolved entry must remain in _action_queues until TTL expires"
    )
    entry = cron_approval._action_queues[cid]
    assert entry["result"] == ["approve"]
    assert cron_approval.resolve_action_confirmation(cid, "approve") is True

    # After the TTL fires, the cached entry is gone and re-resolve 404s.
    import time as _time

    deadline = _time.time() + 2.0
    while cid in cron_approval._action_queues and _time.time() < deadline:
        _time.sleep(0.05)
    assert cid not in cron_approval._action_queues
    assert cron_approval.resolve_action_confirmation(cid, "approve") is False
