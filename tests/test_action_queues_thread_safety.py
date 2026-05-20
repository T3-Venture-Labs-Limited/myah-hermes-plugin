"""Phase 1 PR 1 §1.4 — lock-snapshot iteration over ``_action_queues``.

Per plan-review C-2, every call site that iterates
``cron_approval._action_queues`` must acquire ``_lock`` and snapshot
the dict (``dict(_action_queues)``) before iterating. Otherwise a
concurrent resolve/expire from another thread can mutate the dict
mid-iteration and raise ``RuntimeError: dictionary changed size during
iteration``.

This is a soak test: 100 paired iter/mutate cycles across two threads.
With the unsynchronized version of ``_has_pending_approvals`` (or
``resolve_action_confirmation_by_session``) the race fires reliably
within a handful of iterations.
"""

from __future__ import annotations

import threading
import time

import pytest

from myah_hermes_plugin import cron_approval, dispatcher


@pytest.fixture(autouse=True)
def _clean_state():
    dispatcher._registered_callbacks.clear()
    cron_approval._action_queues.clear()
    yield
    dispatcher._registered_callbacks.clear()
    cron_approval._action_queues.clear()


def _make_entry(session_key: str) -> dict:
    return {
        "session_key": session_key,
        "action_type": "x",
        "description": "x",
        "options": ["approve", "deny"],
        "event": threading.Event(),
        "result": [],
        "metadata": {},
        "timeout": 1800,
    }


def test_concurrent_resolve_and_iterate():
    """Spawn a thread that adds/removes entries while the main thread
    iterates via ``_has_pending_approvals``. Over 100 iterations, no
    ``RuntimeError: dictionary changed size during iteration`` may
    escape."""

    errors: list[BaseException] = []
    stop = threading.Event()

    def _churner():
        try:
            i = 0
            while not stop.is_set():
                cid = f"cid-{i}"
                with cron_approval._lock:
                    cron_approval._action_queues[cid] = _make_entry("sess-X")
                # Random-ish delete to keep both sides busy.
                if i % 2 == 0:
                    with cron_approval._lock:
                        cron_approval._action_queues.pop(cid, None)
                i += 1
                if i > 5000:
                    return
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    churner = threading.Thread(target=_churner, daemon=True)
    churner.start()

    try:
        for _ in range(100):
            # The iterator is inside _has_pending_approvals — this is the
            # function under test. It MUST lock+snapshot before iterating.
            cron_approval._has_pending_approvals("sess-X")
            # A second sweep over a different session_key exercises the
            # filter branch as well.
            cron_approval._has_pending_approvals("sess-Y")
            time.sleep(0.0005)
    finally:
        stop.set()
        churner.join(timeout=2.0)

    assert not errors, f"thread-safety violations: {errors!r}"
