"""Surface upstream's smart-approve DEBUG audit trail as INFO.

Why this exists (2026-05-21)
----------------------------

The Myah agent's ``config.yaml`` sets ``approvals.mode: smart``, which
invokes an LLM "guardian" subagent
(``tools.approval._smart_approve``) for every dangerous-pattern-matched
terminal command. The guardian decides:

- ``approve`` — auto-approves and grants session-level approval
- ``deny`` — blocks
- ``escalate`` — falls through to manual approval card

Upstream Hermes logs auto-approve at ``DEBUG`` (``tools/approval.py``
line 1025) and the LLM-call failure at ``DEBUG`` too (line 786). Both
are below production's INFO threshold so operators see nothing in
``agent.log`` when a dangerous command was auto-approved silently.

The plugin-side audit filter (this module) surfaces those records by
mutating the original DEBUG record in place: it promotes the
``levelno`` to ``INFO`` and prefixes the message with
``[smart-approval-audit]`` so downstream handlers (production's
INFO-level file handler in particular) accept and render it. The
mechanism:

1. Lower the ``tools.approval`` logger level to DEBUG so the records
   are actually created (by default the logger inherits root's
   threshold and the upstream ``logger.debug(...)`` calls are no-ops).
2. Attach a ``logging.Filter`` to ``tools.approval`` that, for every
   DEBUG record matching a smart-approve prefix, promotes the record's
   ``levelno`` + ``levelname`` to ``INFO`` and prefixes the message so
   it's identifiable in logs. The filter returns ``True`` so the
   (now-INFO) record continues through the handler chain.
3. Production's INFO-level handler accepts the mutated record →
   appears in ``agent.log`` alongside other plugin diagnostics.

Why not emit a parallel INFO record on a separate logger
--------------------------------------------------------

Python 3.12+ adds a thread-local re-entrancy guard at
``logging.Logger._tls.in_progress``. The flag is a class attribute
(``threading.local()`` shared across ALL ``Logger`` instances in the
same thread), set to ``True`` inside ``Logger.handle()`` and reset on
exit. Any ``logger.info(...)`` call from inside a filter callback —
even on a *different* logger — sees the flag and silently drops the
record via ``_is_disabled()`` → ``isEnabledFor()`` → ``False``. That
ruled out the cleaner "parallel emit on dedicated audit logger"
design. Mutating ``levelno`` in place bypasses the guard because we
ride the existing in-progress record through to ``callHandlers``.

Future
------

If upstream PR ever changes ``logger.debug(...)`` → ``logger.info(...)``
in ``tools/approval.py``, this filter becomes redundant (the original
record will surface natively at INFO). At that point, drop this module
and its installation call from the plugin entry point.

See ``docs/gotchas/2026-05-21-plugin-cron-tool-not-loaded.md`` (hosted
repo) for related background on which approvals surface where.
"""
from __future__ import annotations

import logging

# Public for tests + ``__init__.py`` re-export. The constant exists for
# log-format clarity (operators grep this prefix in ``agent.log``).
_AUDIT_PREFIX = "[smart-approval-audit]"


class _SmartApprovalAuditFilter(logging.Filter):
    """Promote smart-approve DEBUG records from ``tools.approval`` to
    INFO so the production handler chain accepts them.

    Returns True unconditionally — when the filter matches, it mutates
    the record's ``levelno``/``levelname``/``msg`` so the (now-INFO)
    record continues through ``callHandlers`` unchanged in identity.
    Non-matching DEBUG records pass through untouched at their original
    level.

    Matches two upstream message prefixes:

    - ``"Smart approval: auto-approved"`` (line 1025) — the LLM judge
      said yes; user did NOT see an approval card.
    - ``"Smart approvals: LLM call failed"`` (line 786) — guardian
      subagent failed; command fell through to the manual approval
      prompt.

    Other ``tools.approval`` DEBUG records (e.g.
    ``request_action_confirmation: no gateway callback...``) are NOT
    surfaced — they're not smart-approve events.
    """

    _MATCH_PREFIXES = ("Smart approval:", "Smart approvals:")

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if record.levelno == logging.DEBUG:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 — never break logging
                message = ""
            if any(message.startswith(p) for p in self._MATCH_PREFIXES):
                # Promote in place so the handler-level check downstream
                # sees INFO. Replace ``msg``+``args`` with the
                # already-rendered message so the prefix appears in the
                # formatter output even if a downstream handler does its
                # own ``record.getMessage()`` rendering.
                record.levelno = logging.INFO
                record.levelname = "INFO"
                record.msg = f"{_AUDIT_PREFIX} {message}"
                record.args = ()
        return True


def install() -> None:
    """Configure the ``tools.approval`` logger so smart-approve events
    surface in production INFO logs.

    Safe to call multiple times — the filter is attached at most once.
    Idempotent across plugin reloads and double-imports.

    Called from the plugin entry point's ``register()`` function;
    not exposed as a public API to other plugins.
    """
    approval_logger = logging.getLogger("tools.approval")

    # Ensure the upstream ``logger.debug()`` calls actually produce
    # records. Without this they're no-ops under root's INFO threshold.
    if approval_logger.level > logging.DEBUG or approval_logger.level == logging.NOTSET:
        approval_logger.setLevel(logging.DEBUG)

    # Attach the filter at most once.
    for existing in approval_logger.filters:
        if isinstance(existing, _SmartApprovalAuditFilter):
            return
    approval_logger.addFilter(_SmartApprovalAuditFilter())
