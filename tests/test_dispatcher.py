"""Tests for the plugin-vendored approval-notify dispatcher.

Mirrors upstream's ``gateway/run.py:_dispatch_approval_notify`` semantics:
variadic arity dispatch, silent no-op when no callback is registered,
last-writer-wins on re-registration.
"""

from __future__ import annotations

import pytest

from myah_hermes_plugin import dispatcher


@pytest.fixture(autouse=True)
def _clear_registry():
    """Reset the dispatcher's per-session registry between tests."""
    dispatcher._registered_callbacks.clear()
    yield
    dispatcher._registered_callbacks.clear()


def test_single_arity_callback_receives_just_the_request() -> None:
    """A callback declaring one positional param is invoked with the
    payload only — matches the legacy single-payload upstream shape."""
    captured: list = []

    def cb(request):
        captured.append(request)

    dispatcher.register_gateway_notify("session-1", cb)
    dispatcher._dispatch_approval_notify(
        "session-1", {"action": "confirm", "id": "abc"}
    )

    assert captured == [{"action": "confirm", "id": "abc"}]


def test_three_arity_callback_receives_session_request_and_metadata() -> None:
    """A callback declaring three positional params receives
    (session_key, request, metadata=...) — used for richer adapter
    callbacks that surface metadata on the approval card."""
    captured: list = []

    def cb(session_key, request, metadata=None):
        captured.append((session_key, request, metadata))

    dispatcher.register_gateway_notify("session-2", cb)
    dispatcher._dispatch_approval_notify(
        "session-2",
        {"action": "confirm", "id": "xyz"},
        metadata={"job_id": "j1", "schedule": "0 9 * * *"},
    )

    assert captured == [
        (
            "session-2",
            {"action": "confirm", "id": "xyz"},
            {"job_id": "j1", "schedule": "0 9 * * *"},
        )
    ]


def test_unregistered_session_is_silent_noop() -> None:
    """Dispatching to a session with no registered callback must NOT
    raise. This mirrors upstream's auto-approve-on-no-callback contract
    in ``request_action_confirmation``."""
    # Should not raise.
    dispatcher._dispatch_approval_notify(
        "never-registered", {"action": "confirm"}
    )


def test_re_register_replaces_prior_callback() -> None:
    """Last-writer-wins: re-registering a session_key drops the previous
    callback (matches upstream `_gateway_notify_cbs[session_key] = cb`).
    """
    captured: list = []

    dispatcher.register_gateway_notify(
        "session-3", lambda req: captured.append("first")
    )
    dispatcher.register_gateway_notify(
        "session-3", lambda req: captured.append("second")
    )

    dispatcher._dispatch_approval_notify("session-3", {"action": "confirm"})

    assert captured == ["second"]
