"""
Sentry integration for the Myah agent container.

Call setup_sentry() at container startup before other Hermes imports.
Uses stdlib logging exclusively — no Loguru dependency.

This module is the *only* place in the Hermes fork that imports
``sentry_sdk`` directly.  Runtime telemetry calls in
:mod:`gateway.run` and :mod:`gateway.platforms.api_server` go through
the abstract :class:`agent.telemetry.TelemetryHook` registered here.
"""

import logging
import os

_SERVICE = 'myah-agent'

log = logging.getLogger(__name__)


def setup_sentry() -> None:
    """Initialize Sentry if SENTRY_DSN_AGENT is set.

    Enables error capture, distributed tracing, profiling, and log forwarding.
    Safe to call when Sentry is not configured — returns silently.

    On success, also registers a ``SentryHook`` adapter as the process-wide
    telemetry hook so Hermes runtime code (which only sees the abstract
    :class:`agent.telemetry.TelemetryHook` protocol) routes spans, tags,
    breadcrumbs, and exceptions through this Sentry SDK.
    """
    dsn = os.environ.get('SENTRY_DSN_AGENT', '')
    if not dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.openai import OpenAIIntegration
        from sentry_sdk.integrations.anthropic import AnthropicIntegration

        # AsyncioIntegration auto-spans every ``asyncio.create_task`` /
        # ``asyncio.gather`` call. Without it, async exceptions raised
        # inside spawned tasks lose their parent span context and Sentry
        # treats them as bare crashes instead of trace children. The agent
        # runtime is heavily async (gateway adapter loop, cron scheduler,
        # the SSE event queue), so this captures the bulk of background
        # work. Optional integration — silently no-ops if ``asyncio`` is
        # unavailable, which can't happen on supported Python versions
        # but keeps the call defensive.
        try:
            from sentry_sdk.integrations.asyncio import AsyncioIntegration
            _asyncio_integration = [AsyncioIntegration()]
        except ImportError:  # pragma: no cover — sentry-sdk should always ship this
            _asyncio_integration = []

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get('ENV', 'production'),
            release=os.environ.get('SENTRY_RELEASE'),
            traces_sample_rate=1.0,
            profile_session_sample_rate=1.0,
            profile_lifecycle='trace',
            send_default_pii=True,
            enable_logs=True,
            integrations=[
                LoggingIntegration(
                    level=logging.WARNING,
                    event_level=logging.ERROR,
                ),
                # Capture prompts and responses so Sentry traces show full
                # conversation content alongside token counts and latency.
                OpenAIIntegration(include_prompts=True),
                AnthropicIntegration(include_prompts=True),
                *_asyncio_integration,
            ],
        )
        sentry_sdk.set_tag('user_id', os.environ.get('MYAH_USER_ID', 'unknown'))
        sentry_sdk.set_tag('service', _SERVICE)

        # Wire the Sentry SDK into the abstract TelemetryHook so the rest
        # of Hermes runtime code can emit telemetry without importing
        # sentry_sdk directly.
        from agent.telemetry import register_telemetry_hook
        register_telemetry_hook(_SentryHook(sentry_sdk))

        log.info('Sentry error tracking, tracing and logging enabled for agent container')
    except Exception as e:
        log.warning('Sentry init failed: %s', e)


class _SentryHook:
    """Adapter that forwards :class:`agent.telemetry.TelemetryHook` calls
    to a live ``sentry_sdk`` module.

    Kept private to ``logging_setup`` because this is the natural Sentry
    bootstrap site — agent containers initialize Sentry here, and that
    same call site registers the hook.  Other downstream consumers
    (e.g. the platform backend, the ``myah-admin`` plugin) are free to
    construct their own adapter and call ``register_telemetry_hook`` —
    this class is one valid implementation, not the only allowed one.

    ``start_span`` returns the Sentry span/transaction object directly:
    Sentry's spans already satisfy the :class:`TelemetrySpan` protocol
    (context manager + ``set_data`` + ``finish``).
    """

    def __init__(self, sentry_module: object) -> None:
        self._sentry = sentry_module

    def capture_exception(self, exc, **kwargs):
        return self._sentry.capture_exception(exc, **kwargs)

    def add_breadcrumb(self, *, category, message, level='info', data=None):
        return self._sentry.add_breadcrumb(
            category=category,
            message=message,
            level=level,
            data=data,
        )

    def start_span(self, *, op, description='', **kwargs):
        # Distributed-trace continuation: if the caller passed
        # ``sentry_trace`` and ``baggage`` headers, use ``continue_trace``
        # to attach this span to the upstream trace.  Otherwise this is a
        # plain span.
        sentry_trace = kwargs.pop('sentry_trace', None)
        baggage = kwargs.pop('baggage', None)
        if sentry_trace is not None:
            transaction = self._sentry.continue_trace(
                {'sentry-trace': sentry_trace, 'baggage': baggage or ''},
                op=op,
                name=description or op,
            )
            return self._sentry.start_transaction(transaction, **kwargs)
        # sentry_sdk.start_span returns a Span; we forward all kwargs so
        # callers can pass sampling, name, parent_span, etc.
        return self._sentry.start_span(op=op, description=description, **kwargs)

    def set_tag(self, key, value):
        return self._sentry.set_tag(key, value)

    def set_context(self, name, value):
        return self._sentry.set_context(name, value)
