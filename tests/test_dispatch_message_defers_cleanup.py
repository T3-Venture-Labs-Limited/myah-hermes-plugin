"""Dispatch lifecycle regression tests for Myah stream/session cleanup.

This file started as Phase 1 PR 1 §1.3 coverage for deferring
``_stream_sessions`` cleanup while approvals are pending. It now also protects
long-running dispatches: Myah must not emit ``run.completed`` or close the SSE
stream just because a hidden 10-minute adapter-side wait/sweep threshold elapsed
while Hermes is still active.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from myah_hermes_plugin import cron_approval
from myah_hermes_plugin.myah_platform.adapter import _STREAM_TTL


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
    adapter._streams_created[stream_id] = time.time()
    adapter._chat_id_streams[chat_id] = stream_id
    adapter._session_streams[session_key] = stream_id
    adapter._stream_sessions[stream_id] = session_key


def _drain_queue_nowait(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return items


def _terminal_events(items: list[Any]) -> list[dict[str, Any]]:
    return [
        item for item in items
        if isinstance(item, dict) and item.get("event") in {"run.completed", "run.failed"}
    ]


async def _run_dispatch(adapter, *, chat_id: str, session_key: str, stream_id: str) -> None:
    """Invoke _dispatch_message with no message handler to drive failure cleanup."""
    adapter._message_handler = None
    adapter._active_sessions.clear()

    msg_event = MagicMock()
    msg_event.source = MagicMock()
    msg_event.source.chat_id = chat_id

    await adapter._dispatch_message(msg_event, stream_id, chat_id, session_key)


def _message_event(chat_id: str):
    msg_event = MagicMock()
    msg_event.source = MagicMock()
    msg_event.source.platform = Platform.LOCAL
    msg_event.source.chat_type = "dm"
    msg_event.source.chat_id = chat_id
    msg_event.source.thread_id = None
    msg_event.source.user_id = None
    msg_event.source.user_id_alt = None
    return msg_event


def _built_session_key(adapter, msg_event) -> str:
    from gateway.session import build_session_key

    return build_session_key(
        msg_event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )


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
    """A pending action confirmation for this session_key blocks cleanup."""
    adapter = _make_adapter()
    chat_id, session_key, stream_id = "chat-B", "sess-B", "stream-B"
    _seed_dual_mapping(
        adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id
    )

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


@pytest.mark.asyncio
async def test_dispatch_message_no_handler_emits_failed_without_completed():
    """Gateway-not-ready is a failure terminal event, never success completion."""
    adapter = _make_adapter()
    chat_id, session_key, stream_id = "chat-no-handler", "sess-no-handler", "stream-no-handler"
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._message_handler = None

    msg_event = _message_event(chat_id)

    await adapter._dispatch_message(msg_event, stream_id, chat_id, session_key)

    items = _drain_queue_nowait(adapter._streams[stream_id])
    assert [event["event"] for event in _terminal_events(items)] == ["run.failed"]
    assert None in items


@pytest.mark.asyncio
async def test_dispatch_message_does_not_complete_when_session_still_active_after_legacy_wait(monkeypatch):
    """Crossing the old 6000-poll boundary must not mark an active run completed."""
    import myah_hermes_plugin.myah_platform.adapter as adapter_mod

    adapter = _make_adapter()
    chat_id, stream_id = "chat-long", "stream-long"
    msg_event = _message_event(chat_id)
    built_key = _built_session_key(adapter, msg_event)
    session_key = built_key
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._message_handler = MagicMock()

    async def fake_handle_message(event):
        adapter._active_sessions[built_key] = object()

    original_sleep = adapter_mod.asyncio.sleep
    polls = 0
    crossed_legacy_wait = asyncio.Event()

    async def fake_sleep(delay):
        nonlocal polls
        polls += 1
        if polls > 6000:
            crossed_legacy_wait.set()
        await original_sleep(0)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)

    task = asyncio.create_task(adapter._dispatch_message(msg_event, stream_id, chat_id, session_key))
    await asyncio.wait_for(crossed_legacy_wait.wait(), timeout=2)
    await original_sleep(0)

    items = _drain_queue_nowait(adapter._streams[stream_id])
    assert _terminal_events(items) == []
    assert None not in items
    assert not task.done()

    adapter._active_sessions.pop(built_key, None)
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_dispatch_message_completes_after_session_clears_beyond_legacy_wait(monkeypatch):
    """A >10m-equivalent run completes only after the active session clears."""
    import myah_hermes_plugin.myah_platform.adapter as adapter_mod

    adapter = _make_adapter()
    chat_id, stream_id = "chat-clear", "stream-clear"
    msg_event = _message_event(chat_id)
    built_key = _built_session_key(adapter, msg_event)
    session_key = built_key
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._message_handler = MagicMock()

    async def fake_handle_message(event):
        adapter._active_sessions[built_key] = object()

    original_sleep = adapter_mod.asyncio.sleep
    polls = 0
    cleared_at: int | None = None

    async def fake_sleep(delay):
        nonlocal polls, cleared_at
        polls += 1
        if polls == 6005:
            adapter._active_sessions.pop(built_key, None)
            cleared_at = polls
        await original_sleep(0)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)

    await asyncio.wait_for(adapter._dispatch_message(msg_event, stream_id, chat_id, session_key), timeout=2)

    assert cleared_at == 6005
    items = _drain_queue_nowait(adapter._streams[stream_id])
    assert [event["event"] for event in _terminal_events(items)] == ["run.completed"]
    assert None in items


@pytest.mark.asyncio
async def test_dispatch_message_cancelled_while_active_does_not_emit_completed(monkeypatch):
    """Shutdown/cancellation must not lie to the frontend with run.completed."""
    import myah_hermes_plugin.myah_platform.adapter as adapter_mod

    adapter = _make_adapter()
    chat_id, stream_id = "chat-cancel", "stream-cancel"
    msg_event = _message_event(chat_id)
    built_key = _built_session_key(adapter, msg_event)
    session_key = built_key
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._message_handler = MagicMock()

    async def fake_handle_message(event):
        adapter._active_sessions[built_key] = object()

    original_sleep = adapter_mod.asyncio.sleep
    entered_wait = asyncio.Event()

    async def fake_sleep(delay):
        entered_wait.set()
        await original_sleep(0)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)

    task = asyncio.create_task(adapter._dispatch_message(msg_event, stream_id, chat_id, session_key))
    await asyncio.wait_for(entered_wait.wait(), timeout=2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    items = _drain_queue_nowait(adapter._streams[stream_id])
    assert _terminal_events(items) == []
    assert None not in items
    assert adapter._chat_id_streams[chat_id] == stream_id
    assert adapter._session_streams[session_key] == stream_id
    assert adapter._stream_sessions[stream_id] == session_key


@pytest.mark.asyncio
async def test_dispatch_wait_allows_active_session_to_appear_after_handle_message_returns():
    """The post-handle grace tick prevents immediate completion before activation."""
    adapter = _make_adapter()
    chat_id, stream_id = "chat-delayed-active", "stream-delayed-active"
    msg_event = _message_event(chat_id)
    session_key = _built_session_key(adapter, msg_event)
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._message_handler = MagicMock()

    activated = asyncio.Event()

    async def delayed_activate():
        await asyncio.sleep(0)
        adapter._active_sessions[session_key] = object()
        activated.set()

    async def fake_handle_message(event):
        asyncio.create_task(delayed_activate())

    adapter.handle_message = fake_handle_message

    task = asyncio.create_task(adapter._dispatch_message(msg_event, stream_id, chat_id, session_key))
    await asyncio.wait_for(activated.wait(), timeout=2)

    assert task.done() is False
    assert _terminal_events(_drain_queue_nowait(adapter._streams[stream_id])) == []

    adapter._active_sessions.pop(session_key, None)
    await asyncio.sleep(0.11)
    await asyncio.wait_for(task, timeout=2)


def test_orphan_sweeper_skips_stream_with_active_session():
    """A long-running active dispatch is not an orphan just because it is old."""
    adapter = _make_adapter()
    chat_id, session_key, stream_id = "chat-sweep-active", "sess-sweep-active", "stream-sweep-active"
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._streams_created[stream_id] = 0
    adapter._active_sessions[session_key] = object()

    adapter._sweep_orphaned_streams_once(now=_STREAM_TTL + 1)

    assert stream_id in adapter._streams
    assert stream_id in adapter._streams_created
    assert adapter._chat_id_streams[chat_id] == stream_id
    assert adapter._session_streams[session_key] == stream_id
    assert adapter._stream_sessions[stream_id] == session_key
    assert _drain_queue_nowait(adapter._streams[stream_id]) == []


def test_orphan_sweeper_still_removes_inactive_stale_stream():
    """The active-session exemption must not disable true orphan cleanup."""
    adapter = _make_adapter()
    chat_id, session_key, stream_id = "chat-sweep-old", "sess-sweep-old", "stream-sweep-old"
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._streams_created[stream_id] = 0
    q = adapter._streams[stream_id]

    adapter._sweep_orphaned_streams_once(now=_STREAM_TTL + 1)

    assert stream_id not in adapter._streams
    assert stream_id not in adapter._streams_created
    assert chat_id not in adapter._chat_id_streams
    assert session_key not in adapter._session_streams
    assert stream_id not in adapter._stream_sessions
    assert _drain_queue_nowait(q) == [None]


def test_long_run_status_interval_prefers_explicit_config(monkeypatch):
    monkeypatch.setenv("MYAH_LONG_RUN_STATUS_INTERVAL", "10800")
    adapter = _make_adapter(long_run_status_interval=" 7200 ")

    assert adapter._get_long_run_status_interval_seconds() == 7200.0


def test_long_run_status_interval_reads_env_and_gateway_timeout(monkeypatch):
    monkeypatch.setenv("MYAH_LONG_RUN_STATUS_INTERVAL", "10800")
    assert _make_adapter()._get_long_run_status_interval_seconds() == 10800.0

    monkeypatch.delenv("MYAH_LONG_RUN_STATUS_INTERVAL")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "1800")
    assert _make_adapter()._get_long_run_status_interval_seconds() == 1800.0

    assert _make_adapter(gateway_timeout="3600")._get_long_run_status_interval_seconds() == 3600.0


def test_long_run_status_interval_invalid_values_fall_back(monkeypatch):
    monkeypatch.setenv("MYAH_LONG_RUN_STATUS_INTERVAL", "not-a-number")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")

    assert _make_adapter(long_run_status_interval=-1)._get_long_run_status_interval_seconds() == 1800.0


@pytest.mark.asyncio
async def test_live_send_still_uses_stream_after_legacy_wait_boundary(monkeypatch):
    """Final/visible sends should still hit live SSE while Hermes remains active."""
    import myah_hermes_plugin.myah_platform.adapter as adapter_mod

    adapter = _make_adapter()
    chat_id, stream_id = "chat-live-send", "stream-live-send"
    msg_event = _message_event(chat_id)
    built_key = _built_session_key(adapter, msg_event)
    session_key = built_key
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._message_handler = MagicMock()

    async def fake_handle_message(event):
        adapter._active_sessions[built_key] = object()

    original_sleep = adapter_mod.asyncio.sleep
    polls = 0
    crossed_legacy_wait = asyncio.Event()

    async def fake_sleep(delay):
        nonlocal polls
        polls += 1
        if polls > 6000:
            crossed_legacy_wait.set()
        await original_sleep(0)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)

    task = asyncio.create_task(adapter._dispatch_message(msg_event, stream_id, chat_id, session_key))
    await asyncio.wait_for(crossed_legacy_wait.wait(), timeout=2)

    send_result = await adapter.send(chat_id, "still streaming")
    items = _drain_queue_nowait(adapter._streams[stream_id])

    assert send_result.success is True
    assert chat_id in adapter._chat_id_streams
    assert session_key in adapter._session_streams
    assert stream_id in adapter._stream_sessions
    assert any(
        isinstance(item, dict)
        and item.get("event") == "message.delta"
        and item.get("delta") == "still streaming"
        for item in items
    )
    assert _terminal_events(items) == []

    adapter._active_sessions.pop(built_key, None)
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_dispatch_completion_with_pending_approval_still_defers_cleanup(monkeypatch):
    """New completion wait must preserve the existing pending-approval deferral."""
    adapter = _make_adapter()
    chat_id, stream_id = "chat-approval-complete", "stream-approval-complete"
    msg_event = _message_event(chat_id)
    session_key = _built_session_key(adapter, msg_event)
    _seed_dual_mapping(adapter, chat_id=chat_id, session_key=session_key, stream_id=stream_id)
    adapter._message_handler = MagicMock()

    async def fake_handle_message(event):
        # No active session remains by finally-time; completion path runs immediately.
        return None

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    import threading as _threading

    cron_approval._action_queues["fake-cid-complete"] = {
        "session_key": session_key,
        "action_type": "cron_create",
        "description": "x",
        "options": ["approve", "deny"],
        "event": _threading.Event(),
        "result": [],
        "metadata": {},
        "timeout": 1800,
    }

    await adapter._dispatch_message(msg_event, stream_id, chat_id, session_key)

    assert chat_id in adapter._chat_id_streams
    assert session_key in adapter._session_streams
    assert stream_id in adapter._stream_sessions
