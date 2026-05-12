"""SentryHook implementation for the myah-admin plugin.

Provides a :class:`SentryHook` adapter that satisfies
``agent.telemetry.TelemetryHook`` by forwarding every method to the live
``sentry_sdk``.  The plugin's :mod:`dashboard.plugin_api` calls
``register_sentry_hook()`` at import time so all Hermes runtime spans,
breadcrumbs, tags, and contexts route through the Sentry SDK that
``logging_setup.setup_sentry()`` already initialized in the agent
container.

Note (2026-04-25): The agent container's :mod:`logging_setup` already
constructs an internal ``_SentryHook`` adapter and registers it as the
process-wide hook on ``setup_sentry()``.  Re-registration from this
plugin is idempotent — the last writer wins, and both adapters delegate
to the same ``sentry_sdk`` module.  Keeping the plugin-side registration
makes the hook installation explicit at the plugin boundary, which is
where future telemetry-related plugin behavior (custom tags, scoped
breadcrumbs, etc.) will live.
"""

from __future__ import annotations

from typing import Any, Optional


class SentryHook:
    """Adapter from :class:`agent.telemetry.TelemetryHook` to ``sentry_sdk``.

    Mirrors the private ``_SentryHook`` in
    ``agent/hermes/logging_setup.py``.  Kept in the plugin so the
    plugin's lifecycle (load / unload / configure) can own its own
    telemetry surface without depending on the agent's bootstrap
    module.
    """

    def __init__(self) -> None:
        # Imported lazily so this module is importable even in environments
        # that don't have sentry_sdk installed (the plugin can degrade to a
        # warning rather than an ImportError).
        import sentry_sdk

        self._sentry = sentry_sdk

    def capture_exception(self, exc: BaseException, **kwargs: Any) -> None:
        return self._sentry.capture_exception(exc, **kwargs)

    def add_breadcrumb(
        self,
        *,
        category: str,
        message: str,
        level: str = 'info',
        data: Optional[dict] = None,
    ) -> None:
        return self._sentry.add_breadcrumb(
            category=category,
            message=message,
            level=level,
            data=data,
        )

    def start_span(self, *, op: str, description: str = '', **kwargs: Any):
        # Distributed-trace continuation: if the caller passed
        # ``sentry_trace`` and ``baggage`` headers, build a Sentry
        # transaction with continue_trace; otherwise return a plain span.
        sentry_trace = kwargs.pop('sentry_trace', None)
        baggage = kwargs.pop('baggage', None)
        if sentry_trace is not None:
            transaction = self._sentry.continue_trace(
                {'sentry-trace': sentry_trace, 'baggage': baggage or ''},
                op=op,
                name=description or op,
            )
            return self._sentry.start_transaction(transaction, **kwargs)
        return self._sentry.start_span(op=op, description=description, **kwargs)

    def set_tag(self, key: str, value: Any) -> None:
        return self._sentry.set_tag(key, value)

    def set_context(self, name: str, value: dict) -> None:
        return self._sentry.set_context(name, value)


def register_sentry_hook() -> None:
    """Construct a :class:`SentryHook` and register it as the process-wide
    telemetry hook.

    Safe to call multiple times — registration is idempotent.  Returns
    silently if ``sentry_sdk`` or ``agent.telemetry`` is not importable
    so this module can be imported in dev environments without Sentry
    installed.
    """
    try:
        from agent.telemetry import register_telemetry_hook
        register_telemetry_hook(SentryHook())
    except ImportError:
        # Either sentry_sdk or agent.telemetry is unavailable.  In both
        # cases the right behavior is to no-op — the existing default
        # telemetry hook stays in place.
        return
