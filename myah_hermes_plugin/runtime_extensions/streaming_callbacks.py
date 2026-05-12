"""Phase F: plugin-side structured-streaming workaround for stock vanilla.

Why this exists
---------------

The fork's ``gateway/run.py:_run_agent`` (origin/main:14062-14088) has a
polymorphic dispatch that calls ``adapter.get_structured_callbacks(
session_key)`` and uses the returned dict to wire AIAgent's structured
callbacks (``stream_delta_callback``, ``tool_progress_callback``,
``reasoning_callback``, ``status_callback``) directly to the SSE event
queue. It also tracks a ``_native_streaming_used`` flag that prevents
the gateway's "send full response after streaming" path from
duplicating the message at end-of-turn.

Vanilla NousResearch upstream's ``_run_agent`` (upstream/main:14404-14411)
has neither — it sets gateway-defined messaging-style callbacks
unconditionally and always calls ``adapter.send(full_response)`` after
streaming completes. Result on vanilla without this workaround:

- Tool calls render as inline text "🔧 running bash..." in the
  assistant message buffer (gateway's progress_callback emits these
  via adapter.edit_message).
- Reasoning is silently lost (reasoning_callback is never wired).
- Final assistant message duplicates after streaming completes
  (gateway calls adapter.send with the full response).

Strategy
--------

The vanilla ``pre_llm_call`` plugin hook fires from inside
``AIAgent.run_conversation`` at ``run_agent.py:11765`` (vanilla 44cdf555a) — verified
present in upstream/main. It fires AFTER ``_run_agent`` set
messaging-style callbacks (line 14404) but BEFORE the first LLM API
call begins. Mutating ``agent.stream_delta_callback`` and friends from
inside this hook means the imminent LLM call's tokens fire OUR
callbacks, not the gateway's.

We resolve the active agent by:

1. Filtering on ``platform == "myah"`` (hook receives this kwarg).
2. Looking up the active MyahAdapter via the ``_LATEST_ADAPTER``
   module-attribute pattern already established for F4 secret-capture
   wiring (myah_platform/adapter.py:_LATEST_ADAPTER).
3. Resolving session_key from chat_id via the adapter's
   ``_chat_id_session_keys`` map (Phase F.1).
4. Looking up the cached agent in ``runner._agent_cache[session_key]``
   — same private-attribute access pattern as
   ``runner._session_model_overrides`` used by ``set_session_override_direct``
   (Tier 2B.0 established this pattern; see adapter.py).

Risk profile
------------

- Uses ``runner._agent_cache`` (private attribute). Same risk class as
  ``runner._session_model_overrides`` already used in production. If
  upstream renames either, fix in one file.
- Uses the ``pre_llm_call`` hook name. Vanilla VALID_HOOKS includes it
  (verified hermes_cli/plugins.py:137). CI guard test catches removal.
- Uses ``runner._agent_cache.get(session_key)`` returning a tuple
  ``(agent, sig)`` (verified upstream/main:gateway/run.py around the
  agent caching block). If shape changes, the dict-cache-entry test
  catches it.

This is removable when upstream merges the optional U-CB PR adding
``get_structured_callbacks`` polymorphism to ``_run_agent``. Until
then the workaround works.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _get_latest_adapter():
    """Indirection so tests can monkeypatch this."""
    try:
        from myah_hermes_plugin.myah_platform import adapter as _adapter_module
    except ImportError:
        return None
    return getattr(_adapter_module, "_LATEST_ADAPTER", None)


def myah_pre_llm_call(
    *,
    session_id: str = "",
    platform: str = "",
    **kwargs: Any,
) -> Optional[dict]:
    """Swap gateway-defined messaging-style callbacks for MyahAdapter's
    structured SSE callbacks.

    Hook contract (from upstream/main@44cdf555a:run_agent.py:11765):

        invoke_hook(
            "pre_llm_call",
            session_id=...,
            user_message=...,
            conversation_history=...,
            is_first_turn=...,
            model=...,
            platform=...,
            sender_id=...,
        )

    Hooks may return a ``{"context": str}`` dict to inject extra context
    into the user message, but we don't use that — we return ``None``.
    """
    if platform != "myah":
        return None

    adapter = _get_latest_adapter()
    if adapter is None:
        logger.debug(
            "[myah-streaming] pre_llm_call fired but no MyahAdapter active"
        )
        return None

    # Use the adapter's lazy runner self-discovery instead of reading
    # ``adapter.gateway_runner`` directly. On stock hermes (upstream and this
    # branch) plugin-registered platforms never get ``gateway_runner`` set;
    # ``_resolve_runner`` falls back to ``gateway.run._gateway_runner_ref``.
    runner = None
    _resolve = getattr(adapter, "_resolve_runner", None)
    if callable(_resolve):
        try:
            runner = _resolve()
        except Exception:
            runner = None
    else:
        # Older MyahAdapter without _resolve_runner — fall back to attr read
        # so a partially upgraded plugin still degrades gracefully.
        runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        logger.debug("[myah-streaming] no GatewayRunner available")
        return None

    chat_session_keys = getattr(adapter, "_chat_id_session_keys", None) or {}
    session_key = chat_session_keys.get(session_id)
    if not session_key:
        # Likely a session that wasn't initiated through
        # _handle_message_endpoint (e.g. internal subagent run). Skip.
        return None

    cache = getattr(runner, "_agent_cache", {}) or {}
    cached = cache.get(session_key)
    if not cached:
        logger.debug(
            "[myah-streaming] no cached agent for session_key=%s", session_key,
        )
        return None
    # The cache stores (agent, sig) tuples in vanilla. Be defensive:
    # if shape ever changes to a bare agent, handle that too.
    agent = cached[0] if isinstance(cached, tuple) else cached

    try:
        cbs = adapter.get_structured_callbacks(session_key)
    except Exception:
        logger.exception(
            "[myah-streaming] adapter.get_structured_callbacks raised for %s",
            session_key,
        )
        return None
    if not cbs:
        return None

    # Mutate the agent's callback attributes. AIAgent reads these at
    # call time (not at construction time), so the imminent LLM call
    # uses these.
    if "stream_delta" in cbs:
        agent.stream_delta_callback = cbs["stream_delta"]
    if "tool_progress" in cbs:
        agent.tool_progress_callback = cbs["tool_progress"]
    if "status" in cbs:
        agent.status_callback = cbs["status"]
    if "reasoning" in cbs and hasattr(agent, "reasoning_callback"):
        agent.reasoning_callback = cbs["reasoning"]

    # Mark this session for duplicate-send suppression in
    # MyahAdapter.send(). Without this the gateway's final
    # "adapter.send(chat_id, full_response)" call after streaming would
    # duplicate the assistant message.
    adapter._native_streaming_used.add(session_key)

    # Track that we've installed callbacks but haven't seen any stream yet.
    # Used by the post_llm_call hook below to detect the
    # "gateway suppressed the response" bug — see myah_post_llm_call docstring.
    _stream_invoked = getattr(adapter, "_stream_delta_invoked", None)
    if _stream_invoked is None:
        adapter._stream_delta_invoked = set()

    logger.info(
        "[myah-streaming] structured callbacks installed for session=%s",
        session_key,
    )
    return None


def myah_post_llm_call(
    *,
    session_id: str = "",
    platform: str = "",
    assistant_response: str = "",
    **kwargs: Any,
) -> Optional[dict]:
    """Surface the assistant response when the gateway's suppression bug fires.

    Fires from ``AIAgent.run_conversation`` (run_agent.py:14307) AFTER the
    tool-calling loop completes and ``final_response`` is non-empty. Hook
    contract::

        invoke_hook(
            "post_llm_call",
            session_id=...,
            user_message=...,
            assistant_response=final_response,
            conversation_history=...,
            model=...,
            platform=...,
        )

    Why this exists
    ---------------

    Gateway ``_run_agent`` (gateway/run.py:14081) sets
    ``_native_streaming_used[0] = True`` *optimistically* — as soon as our
    ``get_structured_callbacks`` returns a non-None ``stream_delta``,
    regardless of whether any token actually streams. When the LLM call
    fails (e.g. provider 402 / fallback exhausted), the agent returns a
    response dict with ``failed=True`` and ``final_response="API call
    failed after N retries: ..."``.

    But the gateway's response-dict reconstruction at gateway/run.py:14701
    DOES NOT preserve the ``failed`` field. Downstream, the suppression
    check at gateway/run.py:15326 reads ``response.get("failed")`` → ``None``
    → treats the run as successful → sees ``native_streamed=True`` → sets
    ``already_sent=True`` → ``_process_message_background`` skips
    ``adapter.send(chat_id, response)`` entirely.

    Result: user sees "Thinking..." forever; no token streamed, no error
    message rendered, no indication anything happened.

    Plugin-side fix
    ---------------

    On ``post_llm_call``, check if the plugin's structured stream actually
    fired for this session (via ``adapter._stream_delta_invoked``). If
    NOT, the gateway is about to suppress the response — beat it to the
    punch and emit ``assistant_response`` as a single ``message.delta``
    event via the adapter's SSE pump. This makes the user see the error
    message inline (even if it's just "API call failed after 3 retries:
    Insufficient credits..."), instead of an empty Thinking spinner.

    On the happy path (streaming worked), this is a no-op — the response
    has already been delivered token-by-token.

    Returns None so this hook never affects ``final_response``.
    """
    if platform != "myah":
        return None
    if not assistant_response:
        return None

    adapter = _get_latest_adapter()
    if adapter is None:
        return None

    chat_session_keys = getattr(adapter, "_chat_id_session_keys", None) or {}
    session_key = chat_session_keys.get(session_id)
    if not session_key:
        return None

    stream_invoked = getattr(adapter, "_stream_delta_invoked", None)
    if stream_invoked is None:
        # _stream_delta_invoked not initialized — likely a partially
        # upgraded plugin. Skip the surface-response logic; existing
        # streaming behavior unchanged.
        return None

    if session_key in stream_invoked:
        # Streaming actually fired — the user has already seen the
        # response token-by-token. Nothing to do.
        return None

    # No streaming happened. The gateway is about to suppress the
    # final send. Surface the response ourselves so the user sees
    # SOMETHING (typically an error message).
    stream_id = adapter._session_streams.get(session_key)
    if not stream_id:
        # No active stream for this session — possibly already torn
        # down. Log for diagnostics; nothing more to do.
        logger.warning(
            "[myah-streaming] post_llm_call: no active stream for session=%s "
            "but stream_delta never fired (response would be lost). "
            "Response: %s",
            session_key, assistant_response[:200],
        )
        return None

    try:
        import uuid as _uuid
        adapter._push_event_sync(stream_id, {
            "event": "message.delta",
            "run_id": stream_id,
            "ts": __import__("time").time(),
            "delta": assistant_response,
            "message_id": _uuid.uuid4().hex[:12],
        })
        logger.info(
            "[myah-streaming] post_llm_call: emitted suppressed response "
            "for session=%s (%d chars)",
            session_key, len(assistant_response),
        )
        # Mark stream_invoked so the duplicate-send check in adapter.send()
        # doesn't fire if the gateway somehow does also call send().
        stream_invoked.add(session_key)
    except Exception:
        logger.exception(
            "[myah-streaming] post_llm_call: failed to emit suppressed "
            "response for session=%s",
            session_key,
        )

    return None


def register_streaming_hook(ctx: Any) -> None:
    """Register the pre/post_llm_call hooks with the plugin context.

    Idempotent if ctx supports double-registration; a no-op if
    ctx.register_hook is unavailable (e.g. older Hermes builds).
    """
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", myah_pre_llm_call)
        ctx.register_hook("post_llm_call", myah_post_llm_call)
        logger.info(
            "Myah plugin: registered pre_llm_call + post_llm_call streaming hooks"
        )
