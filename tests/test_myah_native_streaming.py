"""Regression tests for Myah native SSE streaming.

Myah wires its own ``stream_delta`` callback via ``get_structured_callbacks``,
bypassing the shared ``GatewayStreamConsumer``. Without an explicit guard, the
final response would be delivered twice:

1. Token-by-token via ``message.delta`` SSE events during the agent run
2. Again as a single ``message.delta`` event by
   ``BasePlatformAdapter._process_message_background`` after the run completes,
   because the ``_stream_consumer.final_response_sent`` flag stayed ``False``.

``_run_agent`` must flip ``already_sent=True`` on the result dict whenever
native streaming was used, so the base adapter skips the duplicate send.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class NativeStreamingAdapter(BasePlatformAdapter):
    """Mock adapter mirroring Myah's structured-callback streaming pattern."""

    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform("myah"))
        self.sent: list[dict] = []
        self.deltas: list[str] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="native-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    def get_structured_callbacks(self, session_key: str) -> dict:
        """Return callbacks that push SSE-style events instead of send()."""

        def _stream_delta(text):
            if text is not None:
                self.deltas.append(text)

        def _tool_progress(*args, **kwargs):
            return None

        def _reasoning(text):
            return None

        def _status(text):
            return None

        return {
            "stream_delta": _stream_delta,
            "tool_progress": _tool_progress,
            "reasoning": _reasoning,
            "status": _status,
        }


class StreamingAgent:
    """Fake agent that streams tokens via stream_delta_callback, then returns."""

    def __init__(self, **kwargs):
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.reasoning_callback = kwargs.get("reasoning_callback")
        self.status_callback = kwargs.get("status_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.stream_delta_callback:
            for tok in ("Hello", " ", "world", "!"):
                self.stream_delta_callback(tok)
        return {
            "final_response": "Hello world!",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter: NativeStreamingAdapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
        streaming=StreamingConfig(enabled=True),
    )
    return runner


async def _run_native_streaming(monkeypatch, tmp_path, *, streaming_enabled: bool):
    """Drive _run_agent with a Myah-style adapter and streaming config."""
    import yaml

    config_data = {"streaming": {"enabled": streaming_enabled}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config_data), encoding="utf-8")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = StreamingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = NativeStreamingAdapter()
    runner = _make_runner(adapter)
    runner.config.streaming = StreamingConfig.from_dict({"enabled": streaming_enabled})

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***"},
    )

    source = SessionSource(
        platform=Platform("myah"),
        chat_id="myah-chat-1",
        chat_type="dm",
    )
    session_key = "agent:main:myah:dm:myah-chat-1"

    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=source,
        session_id="myah-session-1",
        session_key=session_key,
    )
    return adapter, result


@pytest.mark.asyncio
async def test_myah_streaming_sets_already_sent(monkeypatch, tmp_path):
    """When structured callbacks + streaming are active, already_sent must be True.

    This prevents base._process_message_background from sending the final
    response again after the tokens already streamed.
    """
    adapter, result = await _run_native_streaming(
        monkeypatch, tmp_path, streaming_enabled=True
    )

    assert result.get("already_sent") is True, (
        f"Myah native streaming must mark already_sent=True to prevent duplicate "
        f"delivery; got {result}"
    )
    # Verify the streaming path was actually used
    assert adapter.deltas == ["Hello", " ", "world", "!"]


@pytest.mark.asyncio
async def test_myah_structured_callbacks_mark_already_sent_regardless_of_streaming_config(
    monkeypatch, tmp_path
):
    """Native SSE streaming must set already_sent even when top-level streaming is off.

    The Myah adapter's get_structured_callbacks() wiring is independent of the
    top-level `streaming:` config — agent.stream_delta_callback is assigned
    unconditionally when structured callbacks exist, and AIAgent streams from
    the LLM by default. So tokens WILL push via SSE regardless of the top-level
    flag, and the fallback send() must be suppressed to prevent duplicates.

    Previously this test asserted the opposite (that already_sent stays unset
    when streaming.enabled=False). That codified the duplicate-delivery bug:
    the mock agent still emitted tokens (because the callback was wired), and
    the fallback send() then duplicated them as a single final event.
    """
    adapter, result = await _run_native_streaming(
        monkeypatch, tmp_path, streaming_enabled=False
    )

    # Tokens DID stream — the callback is wired regardless of streaming_enabled.
    assert adapter.deltas == ["Hello", " ", "world", "!"], (
        "Myah's stream_delta callback must fire regardless of streaming.enabled "
        "because get_structured_callbacks wires it unconditionally; "
        f"got deltas={adapter.deltas}"
    )
    # Therefore already_sent must be True so the fallback send() is skipped.
    assert result.get("already_sent") is True, (
        "Myah native streaming delivered tokens via SSE; already_sent must be "
        "True to prevent base._process_message_background from sending the "
        f"full response again as a duplicate; got {result}"
    )


@pytest.mark.asyncio
async def test_myah_streaming_preserves_final_response(monkeypatch, tmp_path):
    """The final_response is still populated even when already_sent=True.

    Downstream callers (transcript persistence, session DB) rely on
    final_response being present regardless of delivery path.
    """
    _, result = await _run_native_streaming(
        monkeypatch, tmp_path, streaming_enabled=True
    )

    assert result.get("final_response") == "Hello world!"
    assert result.get("already_sent") is True
