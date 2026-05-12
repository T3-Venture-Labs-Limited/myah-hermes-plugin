"""Sentry observability hooks for the Myah agent runtime.

This package wires Sentry breadcrumbs and tags into the four primary
lifecycle hooks the Hermes plugin surface exposes:

  - ``pre_api_request`` / ``post_api_request`` — LLM round trips
  - ``pre_tool_call`` / ``post_tool_call`` — tool invocations

Sentry itself is initialized in ``myah_hermes_plugin.sentry_init`` at
plugin-register time. The hooks below are pure observers — they emit
breadcrumbs and tags, never mutate the agent state, and never raise.

Why hooks instead of monkey-patching ``AIAgent``: hooks are the
upstream-supported plugin surface (declared in
``hermes_cli/plugins.py:VALID_HOOKS``), so they survive every upstream
merge without code churn. The Sentry SDK's own ``OpenAIIntegration`` /
``AnthropicIntegration`` already capture the raw HTTP round trip; what
the hooks add is **Myah-level context** (session_id, platform, task_id,
tool_name, duration) so a single Sentry trace tells the operator both
"the OpenAI request took 4.2 s and returned 200" and "that request was
for chat 0193-xxx running model X on session Y at iteration 3".

Removable when the SDK adds first-class agent-loop instrumentation
(tracked as upstream issue ``S-AGENT`` — no firm timeline).
"""

from __future__ import annotations

from .sentry_hooks import register_sentry_hooks

__all__ = ["register_sentry_hooks"]
