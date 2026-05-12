"""Phase F regression tests for the plugin-side streaming workaround.

The pre_llm_call hook fires from AIAgent.run_conversation
(run_agent.py:11765 vanilla / 11066 fork) AFTER _run_agent set messaging-style callbacks
(line 14404) but BEFORE the first LLM API call begins. Tests cover:

1. Hook ignores non-myah platforms.
2. Hook resolves session_key from chat_id and finds the cached agent.
3. Hook installs structured callbacks AND marks the session for
   duplicate-send suppression.
4. Hook is a graceful no-op when adapter / runner / cache are missing.
5. CI guards: pre_llm_call is a real hook name; AIAgent has the four
   callback attributes we mutate.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_fake_agent():
    """Return a fake AIAgent with the four callback attributes."""
    agent = SimpleNamespace(
        stream_delta_callback=lambda *a, **kw: None,
        tool_progress_callback=lambda *a, **kw: None,
        status_callback=lambda *a, **kw: None,
        reasoning_callback=lambda *a, **kw: None,
    )
    return agent


def _make_fake_adapter(session_key: str):
    adapter = MagicMock()
    adapter._chat_id_session_keys = {"chat-1": session_key}
    adapter._native_streaming_used = set()
    structured = {
        "stream_delta": MagicMock(name="cb_stream_delta"),
        "tool_progress": MagicMock(name="cb_tool_progress"),
        "status": MagicMock(name="cb_status"),
        "reasoning": MagicMock(name="cb_reasoning"),
    }
    adapter.get_structured_callbacks.return_value = structured
    return adapter, structured


def test_hook_ignores_non_myah_platform():
    from myah_hermes_plugin.runtime_extensions.streaming_callbacks import (
        myah_pre_llm_call,
    )
    result = myah_pre_llm_call(session_id="chat-1", platform="telegram")
    assert result is None


def test_hook_noop_when_no_adapter():
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=None
    ):
        result = streaming_callbacks.myah_pre_llm_call(
            session_id="chat-1", platform="myah"
        )
    assert result is None


def test_hook_noop_when_session_key_unknown():
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    adapter, _ = _make_fake_adapter(session_key="agent:main:myah:dm:chat-1:user-1")
    adapter._chat_id_session_keys = {}  # platform sent unknown chat_id
    runner_mock = MagicMock()
    adapter.gateway_runner = runner_mock
    # _resolve_runner is the canonical runner accessor — wire the mock to
    # return the same runner so the test exercises both the legacy attribute
    # read AND the new lazy resolver path.
    adapter._resolve_runner.return_value = runner_mock

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        result = streaming_callbacks.myah_pre_llm_call(
            session_id="unknown-chat", platform="myah"
        )
    assert result is None
    adapter.get_structured_callbacks.assert_not_called()


def test_hook_installs_callbacks_and_marks_native_streaming():
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, structured = _make_fake_adapter(session_key=sk)

    fake_agent = _make_fake_agent()
    runner = MagicMock()
    runner._agent_cache = {sk: (fake_agent, "sig")}
    adapter.gateway_runner = runner
    adapter._resolve_runner.return_value = runner

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        result = streaming_callbacks.myah_pre_llm_call(
            session_id="chat-1", platform="myah"
        )

    assert result is None
    assert fake_agent.stream_delta_callback is structured["stream_delta"]
    assert fake_agent.tool_progress_callback is structured["tool_progress"]
    assert fake_agent.status_callback is structured["status"]
    assert fake_agent.reasoning_callback is structured["reasoning"]
    assert sk in adapter._native_streaming_used


def test_hook_handles_dict_cache_entry():
    """Some cache shapes store the agent directly (not in a tuple)."""
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, _ = _make_fake_adapter(session_key=sk)

    fake_agent = _make_fake_agent()
    runner = MagicMock()
    runner._agent_cache = {sk: fake_agent}  # not a tuple
    adapter.gateway_runner = runner
    adapter._resolve_runner.return_value = runner

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        streaming_callbacks.myah_pre_llm_call(
            session_id="chat-1", platform="myah"
        )

    assert fake_agent.stream_delta_callback is not None


def test_hook_falls_back_to_gateway_runner_attr_when_resolve_missing():
    """If MyahAdapter is older and lacks _resolve_runner, the hook must still
    work by reading the legacy ``gateway_runner`` attribute directly."""
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, structured = _make_fake_adapter(session_key=sk)

    fake_agent = _make_fake_agent()
    runner = MagicMock()
    runner._agent_cache = {sk: (fake_agent, "sig")}
    adapter.gateway_runner = runner
    # Simulate an older plugin by deleting _resolve_runner from the mock:
    # use ``spec`` to constrain the mock so missing attrs raise AttributeError
    # rather than auto-mocking.
    del adapter._resolve_runner  # MagicMock supports deletion; subsequent access raises

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        result = streaming_callbacks.myah_pre_llm_call(
            session_id="chat-1", platform="myah"
        )

    assert result is None
    assert fake_agent.stream_delta_callback is structured["stream_delta"]


# ── post_llm_call hook (gateway suppression bug workaround) ─────────


def test_post_llm_call_emits_response_when_stream_did_not_fire():
    """If stream_delta never fired but post_llm_call has assistant_response,
    the hook must emit the response via _push_event_sync so the user sees
    it (instead of 'Thinking...' forever from the gateway's suppression
    bug at gateway/run.py:14701 dropping failed=True).
    """
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, _ = _make_fake_adapter(session_key=sk)
    adapter._session_streams = {sk: "stream-abc"}
    adapter._stream_delta_invoked = set()  # stream NEVER fired
    pushed = []
    adapter._push_event_sync = lambda sid, ev: pushed.append((sid, ev))

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        result = streaming_callbacks.myah_post_llm_call(
            session_id="chat-1",
            platform="myah",
            assistant_response="API call failed after 3 retries: Insufficient credits",
        )

    assert result is None
    assert len(pushed) == 1, "expected exactly one synthetic delta to be pushed"
    sid, event = pushed[0]
    assert sid == "stream-abc"
    assert event["event"] == "message.delta"
    assert "Insufficient credits" in event["delta"]
    # And mark stream_delta_invoked so subsequent adapter.send dedup doesn't
    # cause confusion if the gateway somehow also calls send().
    assert sk in adapter._stream_delta_invoked


def test_post_llm_call_no_op_when_stream_fired():
    """If stream_delta already fired (normal streaming path), the hook
    must NOT emit a duplicate response."""
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, _ = _make_fake_adapter(session_key=sk)
    adapter._session_streams = {sk: "stream-abc"}
    adapter._stream_delta_invoked = {sk}  # stream DID fire
    pushed = []
    adapter._push_event_sync = lambda sid, ev: pushed.append((sid, ev))

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        result = streaming_callbacks.myah_post_llm_call(
            session_id="chat-1",
            platform="myah",
            assistant_response="The answer is 42.",
        )

    assert result is None
    assert pushed == [], "must not emit duplicate response when stream already fired"


def test_post_llm_call_ignores_non_myah_platform():
    """Non-myah platforms (telegram, discord) must not trigger this hook."""
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    result = streaming_callbacks.myah_post_llm_call(
        session_id="chat-1",
        platform="telegram",
        assistant_response="something",
    )
    assert result is None


def test_post_llm_call_ignores_empty_response():
    """Empty assistant_response → no-op (no content to surface)."""
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, _ = _make_fake_adapter(session_key=sk)
    adapter._session_streams = {sk: "stream-abc"}
    adapter._stream_delta_invoked = set()
    pushed = []
    adapter._push_event_sync = lambda sid, ev: pushed.append((sid, ev))

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        result = streaming_callbacks.myah_post_llm_call(
            session_id="chat-1",
            platform="myah",
            assistant_response="",
        )

    assert result is None
    assert pushed == []


def test_post_llm_call_no_op_when_no_active_stream():
    """No active stream for session → log warning, no-op."""
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, _ = _make_fake_adapter(session_key=sk)
    adapter._session_streams = {}  # No active stream
    adapter._stream_delta_invoked = set()
    pushed = []
    adapter._push_event_sync = lambda sid, ev: pushed.append((sid, ev))

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        result = streaming_callbacks.myah_post_llm_call(
            session_id="chat-1",
            platform="myah",
            assistant_response="response with no stream",
        )

    assert result is None
    assert pushed == [], "no active stream → no emission"


# ── CI guards ────────────────────────────────────────────────────────


def test_pre_llm_call_is_in_valid_hooks():
    """If upstream renames or removes pre_llm_call, this fails loudly."""
    from hermes_cli.plugins import VALID_HOOKS
    assert "pre_llm_call" in VALID_HOOKS, (
        "Upstream removed pre_llm_call hook — Phase F workaround broken"
    )


def test_post_llm_call_is_in_valid_hooks():
    """If upstream renames or removes post_llm_call, the gateway-
    suppression-bug workaround in myah_post_llm_call breaks."""
    from hermes_cli.plugins import VALID_HOOKS
    assert "post_llm_call" in VALID_HOOKS, (
        "Upstream removed post_llm_call hook — gateway-suppression "
        "workaround broken"
    )


def test_gateway_runner_ref_is_module_level_weakref():
    """Upstream API drift guard: ``gateway.run._gateway_runner_ref`` is the
    only path Phase F + ISSUE-001 have for plugin-registered platforms to
    reach the live ``GatewayRunner``.

    Upstream sets this in ``GatewayRunner.__init__`` (currently
    ``gateway/run.py:1206`` at submodule SHA 969b9876a) as
    ``_gateway_runner_ref = _weakref.ref(self)``, with the module-level
    default at ``gateway/run.py:1109`` being ``lambda: None``. Both
    ``streaming_callbacks.py`` (via ``MyahAdapter._resolve_runner``) and
    ``adapter.py`` resolve the runner through this attribute.

    If a future upstream merge renames or removes the attribute, every
    plugin call site dereferences ``None`` and Phase F silently no-ops.
    This test makes that scenario fail loudly at plugin-CI time instead
    of in production.
    """
    import gateway.run as _gr

    assert hasattr(_gr, "_gateway_runner_ref"), (
        "upstream gateway.run._gateway_runner_ref is missing — "
        "Phase F streaming hook + MyahAdapter._resolve_runner both broken. "
        "Check upstream gateway/run.py for the GatewayRunner.__init__ block "
        "and update the plugin's resolution chain if the API changed."
    )

    ref = _gr._gateway_runner_ref
    assert callable(ref), (
        f"_gateway_runner_ref must be callable (weakref.ref or compatible) "
        f"so the plugin can dereference it as ``ref()``. Got: {type(ref).__name__}"
    )

    # Calling it without an active GatewayRunner should return None (the
    # module-level default). If it raises, downstream callers in
    # _resolve_runner / streaming_callbacks would need extra try/except.
    try:
        resolved = ref()
    except Exception as exc:  # pragma: no cover
        pytest.fail(
            f"_gateway_runner_ref() raised {type(exc).__name__}: {exc}. "
            f"Plugin callers assume it dereferences cleanly to None when "
            f"no gateway is running."
        )
    # In a fresh test process the default ``lambda: None`` is in place, so
    # resolved should be None. In a process where the gateway started and
    # then GC'd the runner, resolved would also be None. Either way is fine.
    assert resolved is None or hasattr(resolved, "_agent_cache"), (
        f"_gateway_runner_ref() returned {resolved!r}; expected None or "
        f"a GatewayRunner instance (duck-typed by _agent_cache presence)."
    )


def test_runner_agent_cache_attr_exists():
    """If upstream renames _agent_cache, this fails loudly."""
    from gateway.run import GatewayRunner
    # GatewayRunner is a class; instances have _agent_cache. Test by
    # checking that the attribute is defined in the class or one of its
    # ancestors via a sentinel instance.
    inst = GatewayRunner.__new__(GatewayRunner)
    # Direct attr access would fail; use the canonical default
    cache = getattr(inst, "_agent_cache", None)
    if cache is None:
        # Some versions only set _agent_cache in __init__; still need
        # the name in source.
        import inspect
        src = inspect.getsource(GatewayRunner)
        assert "_agent_cache" in src, (
            "Upstream removed GatewayRunner._agent_cache"
        )


def test_aiagent_has_structured_callback_attrs():
    """If upstream renames any of the four callbacks, this fails."""
    from run_agent import AIAgent
    expected = [
        "stream_delta_callback",
        "tool_progress_callback",
        "status_callback",
        "reasoning_callback",
    ]
    import inspect
    src = inspect.getsource(AIAgent.__init__)
    for name in expected:
        assert name in src, (
            f"Upstream removed AIAgent.{name} — Phase F workaround broken"
        )


# ── BONUS-1 / BONUS-2: three-state interaction regression ─────────────
#
# The Phase F streaming workaround keeps three independent state sets on
# the adapter, each with a distinct role:
#
#   * ``_native_streaming_used`` (per session_key) — marked in pre_llm_call,
#     consumed in ``adapter.send()`` to suppress the gateway's duplicate
#     final-response send when our structured callbacks are wired.
#
#   * ``_stream_delta_invoked`` (per session_key) — set when the LLM stream
#     actually fires its ``stream_delta`` callback. ``myah_post_llm_call``
#     reads this to detect the gateway suppression bug (failed=True dropped
#     by ``gateway/run.py:14701``) and emit the assistant_response as a
#     synthetic delta so the user sees the error instead of "Thinking..."
#     forever (BONUS-1).
#
#   * ``_stream_had_content`` (per stream_id) — populated in
#     ``_push_event_sync`` for EVERY ``message.delta`` regardless of
#     delivery path (LLM streaming, slash command response, agent reply via
#     adapter.send, cron live-preview). The ``_dispatch_message`` finally
#     block reads this — NOT ``_stream_delta_invoked`` — to gate its
#     warning emission. Using ``_stream_delta_invoked`` here would
#     false-positive on slash commands that bypass the LLM streaming path
#     (BONUS-2).
#
# These tests pin the contract so a future refactor can't silently collapse
# the three sets into one or swap which set guards which warning gate.


def test_pre_llm_call_initializes_stream_delta_invoked_when_absent():
    """``myah_pre_llm_call`` must lazily initialize ``_stream_delta_invoked``
    to an empty set on older adapters that pre-date the BONUS-1 attribute.

    The init code lives at streaming_callbacks.py around the ``getattr(
    adapter, "_stream_delta_invoked", None)`` block. Without this, an older
    adapter loaded by a newer plugin would raise ``AttributeError`` from
    ``adapter._stream_delta_invoked.add(...)`` the first time it tries to
    record a stream invocation.
    """
    from myah_hermes_plugin.runtime_extensions import streaming_callbacks

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter, _ = _make_fake_adapter(session_key=sk)
    fake_agent = _make_fake_agent()
    runner = MagicMock()
    runner._agent_cache = {sk: (fake_agent, "sig")}
    adapter.gateway_runner = runner
    adapter._resolve_runner.return_value = runner
    # Simulate the older-adapter case: attribute simply does not exist.
    if hasattr(adapter, "_stream_delta_invoked"):
        del adapter._stream_delta_invoked

    with patch.object(
        streaming_callbacks, "_get_latest_adapter", return_value=adapter
    ):
        streaming_callbacks.myah_pre_llm_call(
            session_id="chat-1", platform="myah"
        )

    assert hasattr(adapter, "_stream_delta_invoked"), (
        "pre_llm_call must lazily initialize _stream_delta_invoked on older "
        "adapters — without this attribute, the post_llm_call workaround "
        "raises AttributeError instead of detecting the suppression bug."
    )
    assert isinstance(adapter._stream_delta_invoked, set), (
        f"_stream_delta_invoked must be a set (for O(1) ``session_key in `` "
        f"membership checks). Got: {type(adapter._stream_delta_invoked).__name__}"
    )
    assert adapter._stream_delta_invoked == set(), (
        "Initial state should be empty — the LLM call has not yet streamed "
        "any token at pre_llm_call time."
    )


def test_three_streaming_state_sets_are_distinct_collections():
    """``_native_streaming_used``, ``_stream_delta_invoked``, and
    ``_stream_had_content`` track different things and MUST remain
    independent collections — the BONUS-1/BONUS-2 bug class came from
    collapsing two of them into one and getting false-positives on slash
    commands.

    This is a contract test: it pins the adapter's three-set design so a
    refactor that 'simplifies' to a single shared set fails CI.
    """
    adapter = _make_adapter_vanilla_safe()

    assert hasattr(adapter, "_native_streaming_used"), (
        "MyahAdapter._native_streaming_used missing — Phase F duplicate-send "
        "suppression in adapter.send() broken."
    )
    assert hasattr(adapter, "_stream_delta_invoked"), (
        "MyahAdapter._stream_delta_invoked missing — BONUS-1 (gateway "
        "suppression bug detection in myah_post_llm_call) broken."
    )
    assert hasattr(adapter, "_stream_had_content"), (
        "MyahAdapter._stream_had_content missing — BONUS-2 (slash-command "
        "false-positive guard in _dispatch_message warning) broken. "
        "If you collapsed it into _stream_delta_invoked, /model and other "
        "slash commands will append a spurious 'LLM did not produce a "
        "response' warning even when they delivered correctly."
    )

    # They MUST be three independent set instances. ``id(a) == id(b)`` would
    # mean a refactor accidentally shared one underlying collection — any
    # operation on one would corrupt the others.
    sets = {
        "_native_streaming_used": adapter._native_streaming_used,
        "_stream_delta_invoked": adapter._stream_delta_invoked,
        "_stream_had_content": adapter._stream_had_content,
    }
    ids_seen: dict[int, str] = {}
    for name, value in sets.items():
        assert isinstance(value, set), (
            f"adapter.{name} must be a set; got {type(value).__name__}"
        )
        existing = ids_seen.get(id(value))
        assert existing is None, (
            f"adapter.{name} and adapter.{existing} share the same set "
            f"instance — they must be independent collections. A shared "
            f"collection means BONUS-2's slash-command false-positive is "
            f"back in play."
        )
        ids_seen[id(value)] = name


def test_push_event_sync_message_delta_marks_stream_had_content():
    """``_push_event_sync`` must add to ``_stream_had_content`` for every
    ``message.delta`` event, regardless of delivery path. This is the BONUS-2
    guard: slash commands bypass ``stream_delta_callback`` entirely, so
    ``_stream_delta_invoked`` stays empty even though the user is seeing
    tokens. ``_stream_had_content`` covers them.

    If this contract drifts (e.g. only marks the set for callback-originated
    deltas), the ``_dispatch_message`` finally block at adapter.py:807 will
    false-positive on slash commands and append a spurious warning.
    """
    adapter = _make_adapter_vanilla_safe()

    # Register a dummy queue + session mapping so _push_event_sync has a
    # destination. The exact queue contents don't matter for this assertion.
    import asyncio

    # Need a running loop for the queue.put_nowait in _push_event_sync.
    # Use a fresh loop owned by this test.
    loop = asyncio.new_event_loop()
    try:
        adapter._loop = loop
        q: asyncio.Queue = asyncio.Queue()
        adapter._streams["stream-slash-cmd"] = q

        adapter._push_event_sync("stream-slash-cmd", {
            "event": "message.delta",
            "stream_id": "stream-slash-cmd",
            "run_id": "stream-slash-cmd",
            "timestamp": 0.0,
            "delta": "/model output",
        })

        assert "stream-slash-cmd" in adapter._stream_had_content, (
            "_push_event_sync({'event': 'message.delta', ...}) must add "
            "stream_id to _stream_had_content. If it doesn't, the "
            "gateway-suppression workaround in _dispatch_message will "
            "false-positive on slash commands."
        )
        # AND — critically — must NOT touch _stream_delta_invoked, because
        # _stream_delta_invoked is keyed by session_key, not stream_id, and
        # is only marked when the actual LLM stream_delta callback fires.
        assert "stream-slash-cmd" not in adapter._stream_delta_invoked, (
            "_push_event_sync must NOT touch _stream_delta_invoked — that "
            "set is reserved for the LLM stream_delta callback path. "
            "Mixing them collapses the three-set design and revives BONUS-1/2."
        )
    finally:
        loop.close()


def test_push_event_sync_non_delta_does_not_mark_stream_had_content():
    """Only ``message.delta`` events count as 'content'. Other events
    (run.completed, tool.started, status, etc.) must NOT mark
    ``_stream_had_content`` — otherwise the suppression-workaround warning
    would never fire even on truly empty responses (those still emit
    run.completed and other lifecycle events).
    """
    adapter = _make_adapter_vanilla_safe()

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        adapter._loop = loop
        q: asyncio.Queue = asyncio.Queue()
        adapter._streams["stream-empty"] = q

        adapter._push_event_sync("stream-empty", {
            "event": "run.completed",
            "stream_id": "stream-empty",
            "run_id": "stream-empty",
            "timestamp": 0.0,
        })
        adapter._push_event_sync("stream-empty", {
            "event": "tool.started",
            "stream_id": "stream-empty",
            "tool": "search",
        })
        adapter._push_event_sync("stream-empty", {
            "event": "status",
            "stream_id": "stream-empty",
            "text": "thinking...",
        })

        assert "stream-empty" not in adapter._stream_had_content, (
            "_stream_had_content must only track stream_ids that received "
            "at least one message.delta event. Lifecycle events "
            "(run.completed, tool.*, status) do NOT count as content — "
            "marking the set on them would silence the suppression-bug "
            "warning on truly empty responses."
        )
    finally:
        loop.close()


# ── Phase F.4: duplicate-send suppression in MyahAdapter.send ────────


def _make_adapter_vanilla_safe():
    """Build a MyahAdapter for tests, tolerating both fork (which has
    register_pre_setup_hook) and vanilla (which doesn't).

    The plugin's adapter.py:206 explicitly says "no dependency on
    register_pre_setup_hook" (Tier 2A Task 2A.3 removed it). But
    existing tests in test_myah_adapter.py / test_myah_platform_contract.py
    patch the symbol defensively. On vanilla the symbol is absent, so
    we use create=True so patching synthesizes a placeholder.
    """
    from gateway.config import PlatformConfig
    from gateway.platforms import api_server as _api_server

    has_hook = hasattr(_api_server, "register_pre_setup_hook")
    if has_hook:
        cm = patch("gateway.platforms.api_server.register_pre_setup_hook")
    else:
        cm = patch.object(
            _api_server, "register_pre_setup_hook",
            create=True, new=lambda *a, **kw: None,
        )
    with cm:
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
        return MyahAdapter(
            PlatformConfig(enabled=True, extra={"auth_key": ""})
        )


@pytest.mark.asyncio
async def test_send_suppresses_gateway_final_when_native_streaming_active():
    """If the pre_llm_call hook marked this session for native streaming,
    MyahAdapter.send() must drop the gateway's final-response send.
    """
    adapter = _make_adapter_vanilla_safe()

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter._chat_id_session_keys["chat-1"] = sk
    adapter._native_streaming_used.add(sk)

    result = await adapter.send(
        "chat-1", "full response text", metadata=None
    )

    assert result.success is True
    assert result.message_id == "suppressed-native-streaming"
    # And the flag is consumed so subsequent sends pass through.
    assert sk not in adapter._native_streaming_used


@pytest.mark.asyncio
async def test_send_passes_through_when_no_native_streaming_marker():
    """Without the marker, send() takes its normal SSE-or-webhook path."""
    adapter = _make_adapter_vanilla_safe()

    sk = "agent:main:myah:dm:chat-1:user-1"
    adapter._chat_id_session_keys["chat-1"] = sk
    # Note: marker NOT added

    # No active SSE stream either — should fail with "No active stream"
    result = await adapter.send("chat-1", "some content", metadata=None)
    assert result.success is False
    assert "No active stream" in (result.error or "")
