"""
Tests for the Myah web platform gateway adapter.

Tests cover:
- Adapter lifecycle (init, connect, disconnect)
- Dual session mapping (_session_streams + _chat_id_streams)
- Structured callback event formatting (_format_tool_event)
- Thread-safe queue operations (call_soon_threadsafe)
- Stream management (creation, cleanup, orphan sweep)
- Auth validation (_check_auth returns Optional[web.Response])
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch

from gateway.config import Platform, PlatformConfig


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_adapter(auth_key: str = "", **extra_kwargs):
    """Construct a MyahAdapter with register_pre_setup_hook mocked out."""
    extra = dict(extra_kwargs)
    if auth_key:
        extra["auth_key"] = auth_key
    config = PlatformConfig(enabled=True, extra=extra)

    with patch("gateway.platforms.api_server.register_pre_setup_hook"):
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
        return MyahAdapter(config)


# ── check_myah_requirements ─────────────────────────────────────────────────

class TestCheckRequirements:
    def test_requirements_available(self):
        from myah_hermes_plugin.myah_platform.adapter import check_myah_requirements
        assert check_myah_requirements() is True


# ── Init ────────────────────────────────────────────────────────────────────

class TestMyahAdapterInit:
    def test_default_config(self):
        adapter = _make_adapter()
        # Platform.MYAH was removed from the core enum in Phase 4d.
        # Plugin-registered platforms resolve via Platform("myah") through
        # the enum's _missing_ hook (cached pseudo-member, identity-stable).
        assert adapter.platform == Platform("myah")
        assert adapter.platform.value == "myah"
        assert adapter._auth_key == ""
        assert adapter._streams == {}
        assert adapter._session_streams == {}
        assert adapter._chat_id_streams == {}
        assert adapter._stream_sessions == {}

    def test_auth_key_from_extra(self):
        adapter = _make_adapter(auth_key="test-key-123")
        assert adapter._auth_key == "test-key-123"


# ── Auth ────────────────────────────────────────────────────────────────────
# _check_auth() returns Optional[web.Response]:
#   None       → auth passed
#   web.Response → auth failed (401)


class TestMyahAdapterAuth:
    def test_no_key_configured_allows_all(self):
        """No auth key configured — all requests pass (returns None)."""
        adapter = _make_adapter()
        request = MagicMock()
        request.headers = {}
        assert adapter._check_auth(request) is None

    def test_valid_bearer_token(self):
        """Valid bearer token — returns None (success)."""
        adapter = _make_adapter(auth_key="secret-key")
        request = MagicMock()
        request.headers = {"Authorization": "Bearer secret-key"}
        assert adapter._check_auth(request) is None

    def test_invalid_bearer_token(self):
        """Wrong bearer token — returns 401 response."""
        adapter = _make_adapter(auth_key="secret-key")
        request = MagicMock()
        request.headers = {"Authorization": "Bearer wrong-key"}
        result = adapter._check_auth(request)
        assert result is not None
        assert result.status == 401

    def test_missing_auth_header(self):
        """No Authorization header — returns 401 response."""
        adapter = _make_adapter(auth_key="secret-key")
        request = MagicMock()
        request.headers = {}
        result = adapter._check_auth(request)
        assert result is not None
        assert result.status == 401

    def test_non_bearer_scheme(self):
        """Basic auth scheme instead of Bearer — returns 401."""
        adapter = _make_adapter(auth_key="secret-key")
        request = MagicMock()
        request.headers = {"Authorization": "Basic secret-key"}
        result = adapter._check_auth(request)
        assert result is not None
        assert result.status == 401


# ── Dual session/chat_id mapping ────────────────────────────────────────────

class TestDualMapping:
    def test_independent_mappings(self):
        """session_key and chat_id both resolve to the same stream_id."""
        adapter = _make_adapter()

        stream_id = "stream-001"
        session_key = "agent:main:myah:dm:chat-uuid-1"
        chat_id = "chat-uuid-1"

        adapter._session_streams[session_key] = stream_id
        adapter._chat_id_streams[chat_id] = stream_id
        adapter._streams[stream_id] = asyncio.Queue()

        # Structured callbacks use session_key
        assert adapter._session_streams.get(session_key) == stream_id
        # send() / send_typing() use chat_id
        assert adapter._chat_id_streams.get(chat_id) == stream_id

    def test_multiple_concurrent_streams(self):
        """Multiple chats can have independent active streams."""
        adapter = _make_adapter()

        for i in range(3):
            sid = f"stream-{i}"
            adapter._chat_id_streams[f"chat-{i}"] = sid
            adapter._streams[sid] = asyncio.Queue()

        assert len(adapter._streams) == 3
        assert adapter._chat_id_streams["chat-0"] != adapter._chat_id_streams["chat-1"]


# ── _format_tool_event ──────────────────────────────────────────────────────

class TestFormatToolEvent:
    """Test _format_tool_event with all 4 invocation patterns from run_agent.py."""

    def setup_method(self):
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
        self.stream_id = "test-stream-42"
        # _format_tool_event is a @staticmethod — no adapter instance needed
        self._fmt = MyahAdapter._format_tool_event

    def test_tool_started(self):
        args = ("tool.started", "web_search", "Searching for...", {"query": "test"})
        result = self._fmt(self.stream_id, args, {})
        assert result["event"] == "tool.started"
        assert result["tool"] == "web_search"
        assert result["preview"] == "Searching for..."
        assert result["args"] == {"query": "test"}
        assert result["run_id"] == self.stream_id
        assert result["stream_id"] == self.stream_id

    def test_tool_completed(self):
        args = ("tool.completed", "web_search", None, None)
        kwargs = {"duration": 1.5, "is_error": False}
        result = self._fmt(self.stream_id, args, kwargs)
        assert result["event"] == "tool.completed"
        assert result["tool"] == "web_search"
        assert result["duration"] == 1.5
        assert result["error"] is False
        assert result["run_id"] == self.stream_id

    def test_thinking(self):
        args = ("_thinking", "Let me analyze this...")
        result = self._fmt(self.stream_id, args, {})
        assert result["event"] == "reasoning.delta"
        assert result["text"] == "Let me analyze this..."
        assert result["run_id"] == self.stream_id

    def test_reasoning_available(self):
        args = ("reasoning.available", "_thinking", "Full reasoning text", None)
        result = self._fmt(self.stream_id, args, {})
        assert result["event"] == "reasoning.available"
        assert result["text"] == "Full reasoning text"
        assert result["run_id"] == self.stream_id

    def test_empty_args_fallback(self):
        result = self._fmt(self.stream_id, (), {})
        assert result["event"] == "status"
        assert result["text"] == "working"
        assert result["run_id"] == self.stream_id

    def test_unknown_event_type_fallback(self):
        args = ("unknown.event.type", "data")
        result = self._fmt(self.stream_id, args, {})
        assert result["event"] == "status"
        assert result["text"] == "unknown.event.type"
        assert result["run_id"] == self.stream_id

    def test_tool_started_non_dict_args(self):
        """Non-dict args[3] should fall back to empty dict."""
        args = ("tool.started", "terminal", "Running command", "not-a-dict")
        result = self._fmt(self.stream_id, args, {})
        assert result["args"] == {}

    def test_tool_started_with_none_preview(self):
        """None preview should become empty string."""
        args = ("tool.started", "file_read", None, {"path": "/tmp"})
        result = self._fmt(self.stream_id, args, {})
        assert result["preview"] == ""


# ── send() and send_typing() ───────────────────────────────────────────────

class TestSendMethods:
    @pytest.mark.asyncio
    async def test_send_pushes_message_delta(self):
        adapter = _make_adapter()
        q = asyncio.Queue()
        stream_id = "stream-send-test"
        adapter._chat_id_streams["chat-1"] = stream_id
        adapter._streams[stream_id] = q

        result = await adapter.send("chat-1", "Hello world")
        assert result.success is True

        event = q.get_nowait()
        assert event["event"] == "message.delta"
        assert event["delta"] == "Hello world"
        assert event["run_id"] == stream_id

    @pytest.mark.asyncio
    async def test_send_no_active_stream(self):
        adapter = _make_adapter()
        result = await adapter.send("nonexistent-chat", "Hello")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_send_typing_pushes_status(self):
        adapter = _make_adapter()
        q = asyncio.Queue()
        stream_id = "stream-typing-test"
        adapter._chat_id_streams["chat-2"] = stream_id
        adapter._streams[stream_id] = q

        await adapter.send_typing("chat-2")

        event = q.get_nowait()
        assert event["event"] == "status"
        assert event["status"] == "typing"
        assert event["run_id"] == stream_id

    @pytest.mark.asyncio
    async def test_send_typing_no_stream_is_noop(self):
        adapter = _make_adapter()
        # Should not raise
        await adapter.send_typing("nonexistent-chat")


# ── Myah: Bug A follow-on — structured action confirmation SSE event ──


class TestSendActionConfirmation:
    @pytest.mark.asyncio
    async def test_emits_tool_confirmation_required_event(self):
        """send_action_confirmation pushes a tool.confirmation_required event
        onto the stream queue so the frontend renders the interactive card."""
        adapter = _make_adapter()
        q: asyncio.Queue = asyncio.Queue()
        stream_id = "stream-conf-1"
        session_key = "agent:main:myah:dm:chat-conf"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = q

        result = await adapter.send_action_confirmation(
            session_key,
            {
                "type": "tool.confirmation_required",
                "confirmation_id": "conf-uuid-1",
                "action_type": "cron_create",
                "description": "Create recurring task: 'joke-teller' every 10m",
                "options": ["approve", "approve_session", "deny"],
                "metadata": {
                    "schedule_display": "every 10m",
                    "prompt_preview": "Tell a joke.",
                },
            },
        )

        assert result.success is True
        event = q.get_nowait()
        assert event["event"] == "tool.confirmation_required"
        assert event["confirmation_id"] == "conf-uuid-1"
        assert event["action_type"] == "cron_create"
        assert event["description"].startswith("Create recurring task")
        assert event["options"] == ["approve", "approve_session", "deny"]
        assert event["metadata"]["schedule_display"] == "every 10m"
        assert event["metadata"]["prompt_preview"] == "Tell a joke."
        assert event["stream_id"] == stream_id
        assert event["run_id"] == stream_id  # frontend uses this for the confirm POST

    @pytest.mark.asyncio
    async def test_no_stream_returns_failure_without_emitting(self):
        """When session_key has no mapped stream, return failure (caller falls
        back to text) and don't blow up."""
        adapter = _make_adapter()
        result = await adapter.send_action_confirmation(
            "session-without-stream",
            {"confirmation_id": "x", "action_type": "y", "description": "z", "options": ["approve"]},
        )
        assert result.success is False
        assert "No active stream" in (result.error or "")

    @pytest.mark.asyncio
    async def test_default_options_when_payload_missing_options(self):
        """Default to ['approve', 'deny'] when payload omits options."""
        adapter = _make_adapter()
        q: asyncio.Queue = asyncio.Queue()
        stream_id = "stream-conf-2"
        session_key = "agent:main:myah:dm:chat-default"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = q

        await adapter.send_action_confirmation(
            session_key,
            {"confirmation_id": "c", "action_type": "a", "description": "d"},
        )
        event = q.get_nowait()
        assert event["options"] == ["approve", "deny"]
        assert event["metadata"] == {}  # default when payload omits metadata


# ── get_chat_info ───────────────────────────────────────────────────────────

class TestGetChatInfo:
    @pytest.mark.asyncio
    async def test_returns_expected_shape(self):
        adapter = _make_adapter()
        info = await adapter.get_chat_info("my-chat-id")
        assert info["chat_id"] == "my-chat-id"
        assert info["platform"] == "myah"
        assert info["type"] == "dm"


# ── get_structured_callbacks ────────────────────────────────────────────────

class TestStructuredCallbacks:
    def test_returns_none_when_no_stream(self):
        adapter = _make_adapter()
        result = adapter.get_structured_callbacks("nonexistent-session-key")
        assert result is None

    def test_returns_dict_with_four_callbacks(self):
        adapter = _make_adapter()
        adapter._loop = asyncio.new_event_loop()
        stream_id = "stream-cb-test"
        session_key = "agent:main:myah:dm:chat-x"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = asyncio.Queue()

        cbs = adapter.get_structured_callbacks(session_key)
        assert cbs is not None
        assert set(cbs.keys()) == {"stream_delta", "tool_progress", "reasoning", "status"}
        adapter._loop.close()

    def test_stream_delta_pushes_event(self):
        adapter = _make_adapter()
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        q = asyncio.Queue()
        stream_id = "stream-delta-test"
        session_key = "agent:main:myah:dm:delta-chat"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = q

        cbs = adapter.get_structured_callbacks(session_key)
        # Simulate call from agent thread — call_soon_threadsafe will
        # schedule on the loop.  We run it manually.
        cbs["stream_delta"]("token text")
        loop.run_until_complete(asyncio.sleep(0))  # Process scheduled callbacks

        event = q.get_nowait()
        assert event["event"] == "message.delta"
        assert event["delta"] == "token text"
        assert event["run_id"] == stream_id
        loop.close()

    def test_stream_delta_ignores_none(self):
        """None text (tool boundary signal) should not push any event."""
        adapter = _make_adapter()
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        q = asyncio.Queue()
        stream_id = "stream-none-test"
        session_key = "agent:main:myah:dm:none-chat"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = q

        cbs = adapter.get_structured_callbacks(session_key)
        cbs["stream_delta"](None)
        loop.run_until_complete(asyncio.sleep(0))

        assert q.empty()
        loop.close()


# ── Runner self-discovery (ISSUE-001) ────────────────────────────────────────


class TestResolveRunner:
    """``_resolve_runner`` lazily discovers the gateway runner via the
    upstream-exposed weakref ``gateway.run._gateway_runner_ref``. Necessary
    because ``gateway/run.py:_create_adapter`` only sets
    ``adapter.gateway_runner`` for built-in adapters (Discord/Webhook), not
    for plugin-registered platforms — so the MyahAdapter's
    ``self.gateway_runner`` would otherwise stay ``None`` forever, silently
    disabling the Phase B model override and per-message attribution paths.
    """

    def test_resolve_runner_returns_cached_value_when_set(self):
        """If ``gateway_runner`` was set externally (built-in path or older
        hermes that auto-wires plugin adapters), use it directly without
        consulting the weakref."""
        adapter = _make_adapter()
        sentinel = object()
        adapter.gateway_runner = sentinel
        assert adapter._resolve_runner() is sentinel

    def test_resolve_runner_falls_back_to_weakref(self):
        """When ``gateway_runner`` is None, read from
        ``gateway.run._gateway_runner_ref`` and cache the result."""
        import gateway.run as _gr

        adapter = _make_adapter()
        adapter.gateway_runner = None

        sentinel = object()
        original_ref = _gr._gateway_runner_ref
        _gr._gateway_runner_ref = lambda: sentinel
        try:
            resolved = adapter._resolve_runner()
        finally:
            _gr._gateway_runner_ref = original_ref

        assert resolved is sentinel
        # Cached for the next call (gateway lifecycle = adapter lifecycle).
        assert adapter.gateway_runner is sentinel

    def test_resolve_runner_returns_none_when_no_gateway(self):
        """No gateway running → weakref dereferences to None → return None.
        Callers must handle this case gracefully."""
        import gateway.run as _gr

        adapter = _make_adapter()
        adapter.gateway_runner = None
        original_ref = _gr._gateway_runner_ref
        _gr._gateway_runner_ref = lambda: None
        try:
            assert adapter._resolve_runner() is None
        finally:
            _gr._gateway_runner_ref = original_ref
        # Must NOT cache None — a future call after the gateway starts must
        # see the runner via the weakref again.
        assert adapter.gateway_runner is None

    def test_resolve_runner_handles_weakref_import_failure(self):
        """If ``gateway.run`` can't be imported (extreme test isolation),
        return None gracefully rather than raising."""
        adapter = _make_adapter()
        adapter.gateway_runner = None

        import sys
        saved = sys.modules.get("gateway.run")

        class _Stub:
            def __getattr__(self, name):
                raise ImportError("simulated missing module")

        sys.modules["gateway.run"] = _Stub()
        try:
            assert adapter._resolve_runner() is None
        finally:
            if saved is not None:
                sys.modules["gateway.run"] = saved
            else:
                sys.modules.pop("gateway.run", None)


# ── Stream content tracking (gateway-suppression false-positive guard) ─


class TestStreamHadContent:
    """``_stream_had_content`` is a per-stream_id set that flips True the
    first time any ``message.delta`` event is pushed to the stream queue.

    The ``_dispatch_message`` finally block reads this to decide whether
    to emit the gateway-suppression-bug warning ("LLM call did not produce
    a response"). Without unified tracking at ``_push_event_sync``, slash
    commands like /model — which deliver content via ``adapter.send`` →
    ``_push_event_sync`` directly, bypassing the LLM streaming path —
    would false-positive and append the warning to every successful slash
    response.
    """

    def test_message_delta_push_marks_stream_had_content(self):
        adapter = _make_adapter()
        stream_id = "s-test-1"
        adapter._streams[stream_id] = asyncio.Queue()
        adapter._push_event_sync(stream_id, {
            "event": "message.delta",
            "delta": "hello",
        })
        assert stream_id in adapter._stream_had_content

    def test_non_delta_events_do_not_mark_stream_had_content(self):
        """Reasoning, status, tool events do NOT count as 'real content'.
        The user only sees them as 'Thinking...' or tool cards — without
        a message.delta the chat bubble stays empty."""
        adapter = _make_adapter()
        stream_id = "s-test-2"
        adapter._streams[stream_id] = asyncio.Queue()
        for ev_type in (
            "reasoning.delta",
            "status",
            "tool.started",
            "tool.completed",
            "run.completed",
            "run.failed",
        ):
            adapter._push_event_sync(stream_id, {"event": ev_type, "ts": 0})
        assert stream_id not in adapter._stream_had_content

    def test_unknown_stream_id_does_not_explode(self):
        """If the stream_id is unknown (queue not present), _push_event_sync
        must early-return without raising and without adding to the
        tracker."""
        adapter = _make_adapter()
        adapter._push_event_sync("not-a-real-stream", {
            "event": "message.delta", "delta": "x",
        })
        assert "not-a-real-stream" not in adapter._stream_had_content

    def test_repeated_pushes_idempotent(self):
        """Set semantics — multiple pushes for the same stream don't blow
        up; the stream is in the set once."""
        adapter = _make_adapter()
        stream_id = "s-test-3"
        adapter._streams[stream_id] = asyncio.Queue()
        for _ in range(5):
            adapter._push_event_sync(stream_id, {
                "event": "message.delta", "delta": "tok",
            })
        # set behavior — only one entry per stream_id
        assert adapter._stream_had_content == {stream_id}


# ── Streaming-callback content tracking (gateway-suppression false-positive) ─


class TestStreamDeltaCallbackMarksContent:
    """The LLM streaming path emits ``message.delta`` events through the
    ``_stream_delta`` closure inside ``get_structured_callbacks``, which
    pushes onto the asyncio queue via ``call_soon_threadsafe``. Until the
    2026-05-11 fix, that path did NOT mark ``_stream_had_content`` — only
    the synchronous ``_push_event_sync`` did. Result: every successful
    streamed response triggered the gateway-suppression workaround at
    ``adapter._dispatch_message`` finally:

        "⚠️ The agent's LLM call did not produce a response..."

    appended to the actually-delivered reply.

    These tests pin the contract: ANY ``message.delta`` event reaching
    a stream — sync OR threadsafe-async — marks ``_stream_had_content``.
    """

    @pytest.mark.asyncio
    async def test_stream_delta_callback_marks_stream_had_content(self):
        """End-to-end: build callbacks via ``get_structured_callbacks``,
        invoke the returned ``stream_delta`` (the closure the agent's
        ``stream_delta_callback`` is set to), then verify the stream's
        ID is in ``_stream_had_content``."""
        adapter = _make_adapter()
        session_key = "agent:main:myah:dm:session-1:user-1"
        stream_id = "stream-abc"

        # Wire the adapter's internal state the way _handle_message_endpoint
        # does just before _dispatch_message runs.
        adapter._loop = asyncio.get_running_loop()
        adapter._streams[stream_id] = asyncio.Queue()
        adapter._session_streams[session_key] = stream_id

        cbs = adapter.get_structured_callbacks(session_key)
        assert cbs is not None, (
            "get_structured_callbacks returned None despite a live stream — "
            "test setup is wrong, not the production code."
        )

        # Drive a token through the closure exactly the way AIAgent would.
        cbs["stream_delta"]("hello world")

        # Allow the queued coroutine to run so the threadsafe push lands.
        await asyncio.sleep(0)

        assert stream_id in adapter._stream_had_content, (
            "stream_delta callback did NOT mark _stream_had_content. "
            "The streaming path bypasses _push_event_sync via the local "
            "_put closure, so the BONUS-2 fix at _push_event_sync:338 "
            "never fires for streamed responses. Result: every successful "
            "stream triggers the suppression-bug warning in finally."
        )

    @pytest.mark.asyncio
    async def test_non_delta_streaming_events_do_not_mark_content(self):
        """``reasoning`` / ``status`` / ``tool_progress`` callbacks emit
        non-delta events. They MUST NOT mark ``_stream_had_content`` —
        otherwise the user-visible-content check would false-positive on
        runs that only emitted reasoning/tool activity with no real
        assistant message."""
        adapter = _make_adapter()
        session_key = "agent:main:myah:dm:session-2:user-2"
        stream_id = "stream-def"

        adapter._loop = asyncio.get_running_loop()
        adapter._streams[stream_id] = asyncio.Queue()
        adapter._session_streams[session_key] = stream_id

        cbs = adapter.get_structured_callbacks(session_key)
        assert cbs is not None

        cbs["reasoning"]("CoT token")
        cbs["status"]("working")
        cbs["tool_progress"]("tool.started", "bash", "ls -la", {"cmd": "ls"})

        await asyncio.sleep(0)

        assert stream_id not in adapter._stream_had_content, (
            "Non-message.delta events were tracked as user-visible content. "
            "The suppression-bug check expects _stream_had_content to mean "
            "'a real assistant message was delivered'."
        )
