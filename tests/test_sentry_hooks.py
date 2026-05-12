"""Tests for the Sentry observability hooks (Phase 2).

Four hooks are registered via ``register_sentry_hooks(ctx)``:

  - ``pre_api_request`` / ``post_api_request``
  - ``pre_tool_call`` / ``post_tool_call``

Each hook is a pure observer — emits a Sentry breadcrumb, sets tags on
the current scope, never mutates agent state, never raises. Tests verify
the hook contract:

  1. ``register_sentry_hooks(ctx)`` wires all four into a recording
     ``PluginContext`` stand-in.
  2. Each hook emits exactly one breadcrumb with the expected category +
     level + message shape.
  3. Defensive kwarg absorption — hooks must accept arbitrary new kwargs
     without raising (upstream kwarg lists evolve between Hermes versions).
  4. Hooks are no-ops when ``sentry_sdk`` is unavailable / uninitialized.
  5. ``add_breadcrumb`` raising must not propagate out of the hook.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── Registration contract ─────────────────────────────────────────────


class _RecordingContext:
    """Minimal ``PluginContext`` stand-in that records ``register_hook`` calls."""

    def __init__(self) -> None:
        self.hooks: list[tuple[str, Any]] = []

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))


def test_register_sentry_hooks_wires_all_four():
    """``register_sentry_hooks(ctx)`` must register exactly the four
    hooks documented in the module docstring. If a future refactor
    drops one, the breadcrumb trail in Sentry loses that lifecycle
    point.
    """
    from myah_hermes_plugin.observability import register_sentry_hooks

    ctx = _RecordingContext()
    register_sentry_hooks(ctx)

    names = [name for name, _ in ctx.hooks]
    assert "pre_api_request" in names
    assert "post_api_request" in names
    assert "pre_tool_call" in names
    assert "post_tool_call" in names
    assert len(ctx.hooks) == 4, (
        f"register_sentry_hooks must register exactly 4 hooks; "
        f"got {len(ctx.hooks)}: {names}"
    )


def test_register_sentry_hooks_silently_skips_when_register_hook_missing():
    """Older Hermes builds (and unit-test contexts) may lack
    ``ctx.register_hook``. The function must degrade to a clean no-op
    rather than raise — Sentry observability is best-effort, never
    blocking.
    """
    from myah_hermes_plugin.observability import register_sentry_hooks

    class _BareContext:
        pass

    register_sentry_hooks(_BareContext())  # Should not raise.


def test_hooks_are_valid_hermes_hook_names():
    """Defense-in-depth: ensure the hooks we register are actually
    accepted by upstream's plugin manager. If upstream renames any of
    these, the plugin breaks silently (the call gets logged and the
    hook never fires), so we catch the drift here.
    """
    from hermes_cli.plugins import VALID_HOOKS

    expected = {"pre_api_request", "post_api_request", "pre_tool_call", "post_tool_call"}
    missing = expected - VALID_HOOKS
    assert not missing, (
        f"Upstream VALID_HOOKS missing {missing} — Phase 2 observability "
        f"hooks would silently fail to register."
    )


# ── pre_api_request hook ──────────────────────────────────────────────


def test_pre_api_request_emits_breadcrumb_and_sets_scope_tags():
    """The hook must add a breadcrumb under the ``hermes.api_request``
    category with provider/model/iter visible in the message, and tag
    the current Sentry scope with provider + model so subsequent
    exceptions captured during this turn are grouped by them.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        sentry_hooks.myah_pre_api_request(
            task_id="task-1",
            session_id="agent:main:myah:dm:chat-1:user-1",
            platform="myah",
            model="gpt-5-mini",
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_mode="chat_completions",
            api_call_count=3,
            message_count=12,
            tool_count=5,
            approx_input_tokens=1234,
            request_char_count=4567,
            max_tokens=2048,
        )

    fake_sdk.add_breadcrumb.assert_called_once()
    kwargs = fake_sdk.add_breadcrumb.call_args.kwargs
    assert kwargs["category"] == "hermes.api_request"
    assert kwargs["level"] == "info"
    assert "openai" in kwargs["message"]
    assert "gpt-5-mini" in kwargs["message"]
    assert "iter=3" in kwargs["message"]
    data = kwargs["data"]
    assert data["session_id"] == "agent:main:myah:dm:chat-1:user-1"
    assert data["provider"] == "openai"
    assert data["model"] == "gpt-5-mini"
    assert data["api_call_count"] == 3
    assert data["approx_input_tokens"] == 1234

    # Scope tags — provider + model so subsequent exceptions group correctly.
    tag_calls = {c.args[0]: c.args[1] for c in fake_sdk.set_tag.call_args_list}
    assert tag_calls.get("hermes.provider") == "openai"
    assert tag_calls.get("hermes.model") == "gpt-5-mini"


def test_pre_api_request_absorbs_unknown_kwargs():
    """Upstream may add new kwargs to the ``pre_api_request`` invoke site
    between versions. The hook must accept them without raising — that's
    the whole point of the ``**kwargs`` signature.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        # No exception even though we pass kwargs not in the documented
        # signature.
        result = sentry_hooks.myah_pre_api_request(
            future_upstream_kwarg="surprise",
            another_one={"nested": True},
            provider="anthropic",
            model="claude",
        )

    assert result is None
    fake_sdk.add_breadcrumb.assert_called_once()


def test_pre_api_request_noop_when_sentry_sdk_unavailable():
    """When ``sentry_sdk`` is uninstalled (OSS-minimal mode),
    ``_sentry_sdk`` returns None and the hook must short-circuit cleanly.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    with patch.object(sentry_hooks, "_sentry_sdk", return_value=None):
        # Should not raise; should not even attempt to call any SDK method.
        result = sentry_hooks.myah_pre_api_request(
            provider="openai", model="gpt-5-mini"
        )

    assert result is None


def test_pre_api_request_swallows_sdk_exceptions():
    """If ``add_breadcrumb`` itself raises (e.g. SDK upgrade temporarily
    breaks serialization), the hook must NOT propagate the exception.
    A broken breadcrumb is far better than a crashed agent turn.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    fake_sdk.add_breadcrumb.side_effect = RuntimeError("sdk internal failure")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        # Must NOT raise.
        result = sentry_hooks.myah_pre_api_request(
            provider="openai", model="gpt-5-mini"
        )

    assert result is None


# ── post_api_request hook ─────────────────────────────────────────────


def test_post_api_request_emits_info_breadcrumb_on_normal_finish():
    """Normal finish reasons (stop, tool_calls, length) → info level."""
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        sentry_hooks.myah_post_api_request(
            task_id="t1",
            session_id="s1",
            platform="myah",
            provider="openai",
            model="gpt-5-mini",
            api_call_count=3,
            api_duration=4.2,
            finish_reason="stop",
            usage={"input_tokens": 1234, "output_tokens": 567},
            assistant_content_chars=2000,
            assistant_tool_call_count=0,
        )

    kwargs = fake_sdk.add_breadcrumb.call_args.kwargs
    assert kwargs["level"] == "info"
    assert "stop" in kwargs["message"]
    assert "4.20s" in kwargs["message"] or "4.2" in kwargs["message"]
    assert kwargs["data"]["api_duration"] == 4.2


def test_post_api_request_emits_warning_breadcrumb_on_abnormal_finish():
    """Unrecognized finish reasons (timeout, content_filter, etc.) →
    warning level so Sentry's UI flags them visually."""
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        sentry_hooks.myah_post_api_request(
            provider="openai",
            finish_reason="content_filter",
            api_duration=1.0,
        )

    kwargs = fake_sdk.add_breadcrumb.call_args.kwargs
    assert kwargs["level"] == "warning"


def test_post_api_request_truncates_large_usage_payloads():
    """Usage payloads can be deeply nested (Anthropic returns granular
    cache token counters). The hook must truncate so the breadcrumb
    stays under Sentry's 8KB cap even on rich provider responses.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    huge_usage = {"key": "x" * 5000, "other": list(range(1000))}
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        sentry_hooks.myah_post_api_request(
            provider="anthropic",
            finish_reason="stop",
            usage=huge_usage,
            api_duration=2.5,
        )

    usage_str = fake_sdk.add_breadcrumb.call_args.kwargs["data"]["usage"]
    assert len(usage_str) <= 500, (
        f"Truncated usage should be <= ~500 chars (limit + ellipsis "
        f"marker); got {len(usage_str)}"
    )
    assert "truncated" in usage_str


# ── pre_tool_call hook ────────────────────────────────────────────────


def test_pre_tool_call_emits_breadcrumb_with_truncated_args():
    """The hook must record the tool dispatch with a truncated args
    payload — ``execute_code`` arguments can be many KB of script.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    huge_args = {"command": "echo " + "x" * 5000}
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        sentry_hooks.myah_pre_tool_call(
            tool_name="execute_code",
            args=huge_args,
            task_id="t1",
            session_id="s1",
            tool_call_id="call-abc",
        )

    kwargs = fake_sdk.add_breadcrumb.call_args.kwargs
    assert kwargs["category"] == "hermes.tool_call"
    assert kwargs["level"] == "info"
    assert "execute_code" in kwargs["message"]
    args_str = kwargs["data"]["args"]
    assert len(args_str) <= 500
    assert kwargs["data"]["tool_call_id"] == "call-abc"


def test_pre_tool_call_does_not_set_tool_name_as_scope_tag():
    """Tool calls are nested inside an LLM turn; the outer
    provider/model tag from ``pre_api_request`` is the correct grouping
    key. Tagging the scope with tool_name would clobber the parent tag
    or generate confusingly fine-grained issue groups in Sentry.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        sentry_hooks.myah_pre_tool_call(
            tool_name="execute_code", args={"command": "ls"}, tool_call_id="c1"
        )

    # No set_tag with tool name. The hook may still call set_tag for
    # other reasons in the future; this test pins the current contract.
    tag_keys = [c.args[0] for c in fake_sdk.set_tag.call_args_list]
    assert "hermes.tool_name" not in tag_keys
    assert "hermes.tool" not in tag_keys


# ── post_tool_call hook ───────────────────────────────────────────────


def test_post_tool_call_emits_breadcrumb_with_duration_and_result():
    """Captures duration_ms and a truncated result. Even when the tool
    returns a JSON error payload, the breadcrumb level stays ``info``
    — this hook is observational, not an error reporter."""
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    error_result = '{"error": "permission denied"}'
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        sentry_hooks.myah_post_tool_call(
            tool_name="execute_code",
            args={"command": "rm -rf /"},
            result=error_result,
            duration_ms=42,
            tool_call_id="c1",
        )

    kwargs = fake_sdk.add_breadcrumb.call_args.kwargs
    assert kwargs["level"] == "info", (
        "post_tool_call breadcrumb must stay 'info' even on error "
        "payloads — Sentry-level error reporting is the caller's job."
    )
    assert "42ms" in kwargs["message"]
    assert kwargs["data"]["duration_ms"] == 42
    assert "permission denied" in kwargs["data"]["result"]


def test_post_tool_call_absorbs_unknown_kwargs():
    """Defensive kwarg absorption — same contract as pre_api_request.
    Upstream may add ``output_chars``, ``tool_version``, etc. in
    future versions."""
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        result = sentry_hooks.myah_post_tool_call(
            tool_name="search",
            new_upstream_kwarg=42,
            duration_ms=100,
        )

    assert result is None
    fake_sdk.add_breadcrumb.assert_called_once()


# ── Cross-hook contract ───────────────────────────────────────────────


def test_all_hooks_return_none():
    """Hermes invoke_hook supports return values to influence runtime
    behavior (e.g. ``transform_tool_result`` returns a replacement
    string). Our observability hooks are pure observers and MUST
    return None so they never accidentally alter agent state.
    """
    from myah_hermes_plugin.observability import sentry_hooks

    fake_sdk = MagicMock(name="sentry_sdk")
    with patch.object(sentry_hooks, "_sentry_sdk", return_value=fake_sdk):
        assert sentry_hooks.myah_pre_api_request(provider="openai") is None
        assert sentry_hooks.myah_post_api_request(provider="openai") is None
        assert sentry_hooks.myah_pre_tool_call(tool_name="search") is None
        assert sentry_hooks.myah_post_tool_call(tool_name="search", duration_ms=1) is None


def test_truncate_helper_short_strings_passthrough():
    """The internal _truncate helper must pass small values through
    unchanged so we don't waste cycles on the hot path."""
    from myah_hermes_plugin.observability.sentry_hooks import _truncate

    assert _truncate("short") == "short"
    assert _truncate("") == ""
    assert _truncate(None) == ""
    assert _truncate(42) == "42"
    assert _truncate({"k": "v"}) == "{'k': 'v'}"


def test_truncate_helper_caps_long_strings():
    """Long values are truncated with a clear marker so a future
    reader of a breadcrumb knows they're seeing a tail-clipped
    payload."""
    from myah_hermes_plugin.observability.sentry_hooks import _truncate

    out = _truncate("x" * 1000, limit=100)
    assert len(out) <= 200  # 100 + ellipsis suffix
    assert "truncated" in out
    assert "1000 chars total" in out
