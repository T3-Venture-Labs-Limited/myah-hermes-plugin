"""Phase 1 PR 1 §1.3 — defer ``_stream_sessions`` cleanup while approvals pending.

When ``_dispatch_message`` exits with a pending action confirmation
still in ``cron_approval._action_queues`` for the dispatch's
``session_key``, the dual-mapping pop in the ``finally`` block must be
skipped so the eventual ``/myah/v1/confirm/{stream_id}`` request can
still resolve back to a live session/stream_id and reach the cron
approval primitive.

If we pop unconditionally (current bug), the user clicks Approve in
the chat, the confirm endpoint looks up ``_stream_sessions[stream_id]``
to find the session_key, gets ``None``, and returns 404. The cron tool
then times out (after the new 1800s default from §1.1) and effectively
denies the action.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from myah_hermes_plugin import cron_approval


def _make_adapter(**extra_kwargs):
    extra = dict(extra_kwargs)
    extra.setdefault("auth_key", "test-key")
    config = PlatformConfig(enabled=True, extra=extra)
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter

    return MyahAdapter(config)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset cron_approval module state between tests."""
    from myah_hermes_plugin import dispatcher

    dispatcher._registered_callbacks.clear()
    cron_approval._action_queues.clear()
    yield
    dispatcher._registered_callbacks.clear()
    cron_approval._action_queues.clear()


def _seed_dual_mapping(adapter, *, chat_id: str, session_key: str, stream_id: str) -> None:
    """Pre-populate the dispatch state as if _handle_message_endpoint ran."""
    adapter._streams[stream_id] = asyncio.Queue()
    adapter._chat_id_streams[chat_id] = stream_id
    adapter._session_streams[session_key] = stream_id
    adapter._stream_sessions[stream_id] = session_key


async def _run_dispatch(adapter, *, chat_id: str, session_key: str, stream_id: str) -> None:
    """Invoke _dispatch_message with a noop message handler to drive the finally
    block (which is what we actually want to test)."""
    # MessageEvent fields are minimal — _dispatch_message only forwards to
    # handle_message and then runs the finally block. We stub the handler so
    # the function returns immediately.
    adapter._message_handler = None  # forces the early-return run.failed path
    # short-circuit the 6000-iteration active_sessions sleep loop
    adapter._active_sessions = {}

    msg_event = MagicMock()
    msg_event.source = MagicMock()
    msg_event.source.chat_id = chat_id

    await adapter._dispatch_message(msg_event, stream_id, chat_id, session_key)


@pytest.mark.asyncio
async def test_dispatch_message_pops_when_no_pending():
    """Baseline: no pending approval → finally pops all three maps."""
    adapter = _make_adapter()
    chat_id, session_key, stream_id = "chat-A", "sess-A", "stream-A"
    _seed_dual_mapping(
        adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id
    )

    await _run_dispatch(
        adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id
    )

    assert chat_id not in adapter._chat_id_streams
    assert session_key not in adapter._session_streams
    assert stream_id not in adapter._stream_sessions


@pytest.mark.asyncio
async def test_dispatch_message_defers_when_pending(monkeypatch):
    """A pending action confirmation for this session_key blocks the
    cleanup so the eventual /confirm POST can still resolve."""
    adapter = _make_adapter()
    chat_id, session_key, stream_id = "chat-B", "sess-B", "stream-B"
    _seed_dual_mapping(
        adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id
    )

    # Stage a "pending" approval for sess-B. We don't go through
    # request_action_confirmation here (it would block); we just inject
    # the entry shape _has_pending_approvals inspects.
    import threading as _threading

    cron_approval._action_queues["fake-cid"] = {
        "session_key": session_key,
        "action_type": "cron_create",
        "description": "x",
        "options": ["approve", "deny"],
        "event": _threading.Event(),
        "result": [],  # NOT yet resolved
        "metadata": {},
        "timeout": 1800,
    }

    await _run_dispatch(
        adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id
    )

    assert chat_id in adapter._chat_id_streams, "deferral must keep chat→stream live"
    assert session_key in adapter._session_streams, (
        "deferral must keep session→stream live so confirm can resolve"
    )
    assert stream_id in adapter._stream_sessions, (
        "deferral must keep stream→session live so confirm endpoint can find the session"
    )
