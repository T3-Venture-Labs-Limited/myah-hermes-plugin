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
import json
import pytest
from unittest.mock import MagicMock

from gateway.config import Platform, PlatformConfig


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_adapter(auth_key: str = "", **extra_kwargs):
    """Construct a MyahAdapter.

    Tier 2A Task 2A.3 (see ``myah_platform/standalone_runner.py``) retired
    the adapter's dependency on ``gateway.platforms.api_server.register_pre_setup_hook``
    — the adapter now owns its own aiohttp ``AppRunner`` via
    ``MyahStandaloneRunner``. Older revisions of this helper patched the
    upstream symbol to keep adapter ``__init__`` side-effect free; that
    patch is now both unnecessary and impossible (the symbol no longer
    exists in upstream), so we just build the adapter directly.
    """
    extra = dict(extra_kwargs)
    if auth_key:
        extra["auth_key"] = auth_key
    config = PlatformConfig(enabled=True, extra=extra)

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
    def test_no_key_configured_fails_closed(self):
        """No auth key configured — returns 503 with actionable error.

        Previously this returned ``None`` ("allow all") which was a silent
        security footgun: anyone who could reach the adapter's port could
        call ``/myah/v1/admin/*`` without credentials. The new behavior
        keeps the routes bound (so platform probes still see the plugin)
        but refuses every authed request until the operator wires the
        bearer token via ``scripts/setup-myah-oss.sh``.
        """
        adapter = _make_adapter()
        request = MagicMock()
        request.headers = {"Authorization": "Bearer anything-at-all"}
        result = adapter._check_auth(request)
        assert result is not None
        assert result.status == 503

    def test_no_key_configured_fail_closed_message_is_actionable(self):
        """The 503 body names the env var and the remediation script."""
        import json as _json
        adapter = _make_adapter()
        request = MagicMock()
        request.headers = {}
        result = adapter._check_auth(request)
        assert result is not None
        assert result.status == 503
        body = _json.loads(result.body.decode("utf-8"))
        assert "MYAH_ADAPTER_AUTH_KEY" in body.get("detail", "")
        assert "setup-myah-oss.sh" in body.get("detail", "")

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
        assert result["event"] == "status"
        assert result["text"] == "Searching for..."
        assert result["run_id"] == self.stream_id
        assert result["stream_id"] == self.stream_id

    def test_tool_completed(self):
        args = ("tool.completed", "web_search", None, None)
        kwargs = {"duration": 1.5, "is_error": False}
        result = self._fmt(self.stream_id, args, kwargs)
        assert result["event"] == "status"
        assert result["text"] == "None"
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
        """Legacy name-keyed tool.start becomes status to avoid duplicates."""
        args = ("tool.started", "terminal", "Running command", "not-a-dict")
        result = self._fmt(self.stream_id, args, {})
        assert result["event"] == "status"
        assert result["text"] == "Running command"

    def test_tool_started_with_none_preview(self):
        """Legacy tool events should not generate duplicate function rows."""
        args = ("tool.started", "file_read", None, {"path": "/tmp"})
        result = self._fmt(self.stream_id, args, {})
        assert result["event"] == "status"
        assert result["text"] == "None"


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

    def test_returns_dict_with_structured_tool_callbacks(self):
        adapter = _make_adapter()
        adapter._loop = asyncio.new_event_loop()
        stream_id = "stream-cb-test"
        session_key = "agent:main:myah:dm:chat-x"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = asyncio.Queue()

        cbs = adapter.get_structured_callbacks(session_key)
        assert cbs is not None
        assert set(cbs.keys()) == {
            "stream_delta",
            "tool_progress",
            "tool_start",
            "tool_complete",
            "tool_start_callback",
            "tool_complete_callback",
            "reasoning",
            "status",
        }
        assert callable(cbs["tool_start"])
        assert callable(cbs["tool_complete"])
        adapter._loop.close()

    def test_get_structured_callbacks_exposes_tool_start_and_complete(self):
        adapter = _make_adapter()
        adapter._loop = asyncio.new_event_loop()
        stream_id = "stream-structured-tools"
        session_key = "agent:main:myah:dm:chat-tools"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = asyncio.Queue()

        cbs = adapter.get_structured_callbacks(session_key)

        assert cbs is not None
        assert {"tool_start", "tool_complete"} <= cbs.keys()
        assert cbs["tool_start_callback"] is cbs["tool_start"]
        assert cbs["tool_complete_callback"] is cbs["tool_complete"]
        adapter._loop.close()

    def test_tool_complete_callback_emits_real_todo_result_and_call_id(self):
        adapter = _make_adapter()
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        q = asyncio.Queue()
        stream_id = "stream-tool-complete"
        session_key = "agent:main:myah:dm:chat-tool-complete"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = q
        exact_result = '{"todos":[{"id":"1","content":"Plan work","status":"in_progress"}]}'

        cbs = adapter.get_structured_callbacks(session_key)
        cbs["tool_complete"](
            "call_todo_1",
            "todo",
            {"todos": [{"id": "1", "content": "Plan work", "status": "in_progress"}]},
            exact_result,
        )
        loop.run_until_complete(asyncio.sleep(0))

        event = q.get_nowait()
        assert event["event"] == "tool.completed"
        assert event["tool"] == "todo"
        assert event["call_id"] == "call_todo_1"
        assert event["result"] == exact_result
        assert json.loads(event["result"])["todos"][0]["content"] == "Plan work"
        loop.close()

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
    def test_structured_tool_start_pushes_real_call_id_event(self):
        adapter = _make_adapter()
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        q = asyncio.Queue()
        stream_id = "stream-tool-start-test"
        session_key = "agent:main:myah:dm:tool-start-chat"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = q

        cbs = adapter.get_structured_callbacks(session_key)
        cbs["tool_start"]("call_abc123", "search_files", {"pattern": "tool_start"})
        loop.run_until_complete(asyncio.sleep(0))

        event = q.get_nowait()
        assert event["event"] == "tool.started"
        assert event["stream_id"] == stream_id
        assert event["run_id"] == stream_id
        assert event["timestamp"]
        assert event["call_id"] == "call_abc123"
        assert event["tool"] == "search_files"
        assert event["args"] == {"pattern": "tool_start"}
        assert "preview" in event
        loop.close()

    def test_structured_tool_complete_pushes_real_call_id_event(self):
        adapter = _make_adapter()
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        q = asyncio.Queue()
        stream_id = "stream-tool-complete-test"
        session_key = "agent:main:myah:dm:tool-complete-chat"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = q

        cbs = adapter.get_structured_callbacks(session_key)
        cbs["tool_complete"](
            "call_abc123",
            "search_files",
            {"pattern": "tool_start"},
            "found 3 matches",
        )
        loop.run_until_complete(asyncio.sleep(0))

        event = q.get_nowait()
        assert event["event"] == "tool.completed"
        assert event["stream_id"] == stream_id
        assert event["run_id"] == stream_id
        assert event["timestamp"]
        assert event["call_id"] == "call_abc123"
        assert event["tool"] == "search_files"
        assert event["args"] == {"pattern": "tool_start"}
        assert event["result"] == "found 3 matches"
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
    first time user-visible stream activity is pushed to the stream queue.

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
        """Non-visible terminal/control events do not count as user-visible content."""
        adapter = _make_adapter()
        stream_id = "s-test-2"
        adapter._streams[stream_id] = asyncio.Queue()
        for ev_type in (
            "run.completed",
            "run.failed",
        ):
            adapter._push_event_sync(stream_id, {"event": ev_type, "ts": 0})
        assert stream_id not in adapter._stream_had_content

    def test_structured_activity_push_marks_stream_had_content(self):
        """Visible tool/reasoning/status events prevent fallback Working text.

        Regression guard for long runs and /steer: if a run has only
        structured activity before final text, it still has visible user
        progress and must not trigger the gateway-suppression fallback.
        """
        adapter = _make_adapter()
        stream_id = "s-test-structured"
        adapter._streams[stream_id] = asyncio.Queue()

        for ev_type in adapter.CONTENTFUL_STREAM_EVENTS - {"message.delta"}:
            adapter._stream_had_content.clear()
            adapter._push_event_sync(stream_id, {"event": ev_type, "ts": 0})
            assert stream_id in adapter._stream_had_content, ev_type

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
    async def test_visible_structured_streaming_events_mark_content(self):
        """Reasoning/status/tool callbacks are visible activity in Myah.

        They must mark ``_stream_had_content`` so a long tool-only phase or
        /steer continuation does not get overwritten by the generic
        gateway-suppression fallback while the UI already has useful timeline
        rows to show.
        """
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

        assert stream_id in adapter._stream_had_content


# ── Bug F: adapter.send must NOT misclassify regular replies as cron ──────


class TestAdapterSendCronClassification:
    """Regression tests (2026-05-21) for Bug F production root cause.

    `adapter.send`'s "Cron Path A" recovery had two signals:
      1. Session contextvar (`_recover_cron_job_id_from_session_key`)
      2. jobs.json lookup by `chat_id` ← THE BUG

    Signal #2 fired whenever the chat had ANY existing cron, regardless of
    whether the current adapter.send call was actually from the cron
    scheduler. Result: the gateway's regular final `adapter.send(chat_id,
    response)` after an LLM turn that created a cron was misclassified as
    a cron delivery — both live-preview SSE push AND webhook fired, both
    delivering the SAME content to the chat. The user saw the LLM reply
    rendered twice.

    Phase F suppression at lines ~2277 NEVER reached because the cron
    branch returns BEFORE the suppression check.

    Verified empirically 2026-05-21 14:30 UTC: chat `4cbe2103…` after
    cron creation had `content` and `output[2].message.content` both
    containing the LLM response TWICE (≈770 chars vs gateway's 374
    response).
    """

    @pytest.mark.asyncio
    async def test_regular_reply_with_existing_cron_in_chat_does_not_misclassify(
        self, monkeypatch,
    ):
        """Regular gateway reply (no metadata) must not get classified as
        cron just because the chat has a cron in jobs.json."""
        from unittest.mock import patch

        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        chat_id = "test-chat-with-existing-cron"
        session_key = "agent:main:myah:dm:test-chat-with-existing-cron"
        stream_id = "test-stream-bug-f"

        adapter._chat_id_streams[chat_id] = stream_id
        adapter._session_streams[session_key] = stream_id
        adapter._chat_id_session_keys[chat_id] = session_key
        adapter._native_streaming_used.add(session_key)  # Phase F fired
        q: asyncio.Queue = asyncio.Queue()
        adapter._streams[stream_id] = q

        # Simulate jobs.json having a cron for this chat.
        with patch(
            "myah_hermes_plugin.myah_platform.adapter._load_cron_jobs_safely",
            return_value=[
                {
                    "id": "existing-cron-id",
                    "name": "Existing cron",
                    "origin": {"platform": "myah", "chat_id": chat_id},
                    "last_run_at": "2026-05-21T06:00:00+00:00",
                }
            ],
        ):
            result = await adapter.send(
                chat_id,
                "Final LLM response confirming cron creation",
                reply_to=None,
                metadata=None,  # ← regular reply, no metadata
            )

        # Phase F suppression should engage; result.success True with the
        # specific suppression message_id. If the cron-path misclassifier
        # fires, this returns webhook results instead.
        assert result.success is True
        assert result.message_id == "suppressed-native-streaming", (
            f"Regular chat reply was misclassified as cron delivery, "
            f"bypassing Phase F suppression. Result: {result}. "
            f"Production duplicate-output bug recurs."
        )
        # And: NO message.delta should have been pushed (suppressed).
        # Drain queue to verify.
        assert q.empty(), (
            f"Phase F suppression should have prevented any push, but "
            f"queue contains: {q.qsize()} item(s) — duplicate path fired."
        )

    @pytest.mark.asyncio
    async def test_cron_scheduler_call_with_thread_id_still_routes_to_webhook(
        self, monkeypatch,
    ):
        """Sanity: when the cron scheduler calls adapter.send with cron-
        context metadata (thread_id), the recovery still fires correctly
        and the cron path runs. This preserves the "test it here"
        functionality from Bug D-v4 (2026-04-25)."""
        from unittest.mock import patch, AsyncMock

        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        chat_id = "test-chat-cron-scheduler"
        adapter._chat_id_streams[chat_id] = "test-stream-cs"
        q: asyncio.Queue = asyncio.Queue()
        adapter._streams["test-stream-cs"] = q

        # Stub webhook to avoid hitting network.
        adapter._send_via_webhook = AsyncMock(
            return_value=__import__(
                "myah_hermes_plugin.myah_platform.adapter",
                fromlist=["SendResult"],
            ).SendResult(success=True, message_id="webhook-ok")
        )

        with patch(
            "myah_hermes_plugin.myah_platform.adapter._load_cron_jobs_safely",
            return_value=[
                {
                    "id": "existing-cron-id",
                    "name": "Existing cron",
                    "origin": {"platform": "myah", "chat_id": chat_id},
                    "last_run_at": "2026-05-21T06:00:00+00:00",
                }
            ],
        ):
            result = await adapter.send(
                chat_id,
                "Cron output content",
                reply_to=None,
                metadata={"thread_id": "thread-123"},  # cron scheduler shape
            )

        # Webhook path should have engaged.
        assert result.success is True
        adapter._send_via_webhook.assert_called_once()
        # Live-preview push fires too (best-effort decoration).
        assert q.qsize() == 1, "Cron path should have pushed a live preview"

    @pytest.mark.asyncio
    async def test_regular_reply_no_cron_in_chat_still_goes_to_sse(self):
        """Baseline: when no cron exists for the chat, regular replies
        flow through the live SSE path normally."""
        from unittest.mock import patch

        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        chat_id = "test-chat-no-cron"
        session_key = "agent:main:myah:dm:test-chat-no-cron"
        stream_id = "test-stream-no-cron"

        adapter._chat_id_streams[chat_id] = stream_id
        adapter._session_streams[session_key] = stream_id
        adapter._chat_id_session_keys[chat_id] = session_key
        # No native streaming used (no Phase F for this test).
        q: asyncio.Queue = asyncio.Queue()
        adapter._streams[stream_id] = q

        with patch(
            "myah_hermes_plugin.myah_platform.adapter._load_cron_jobs_safely",
            return_value=[],  # no crons
        ):
            result = await adapter.send(
                chat_id,
                "Hello user",
                reply_to=None,
                metadata=None,
            )

        assert result.success is True
        # One push to SSE.
        assert q.qsize() == 1
        event = q.get_nowait()
        assert event["event"] == "message.delta"
        assert event["delta"] == "Hello user"
