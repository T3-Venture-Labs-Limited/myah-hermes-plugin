"""Tests for the Smart-Approvals audit-logging filter.

Background (2026-05-21): the Myah agent's ``config.yaml`` sets
``approvals.mode: smart`` which invokes an upstream LLM "guardian"
subagent (``tools.approval._smart_approve``) that auto-approves
dangerous-pattern-matched terminal commands the LLM judges as safe.
Upstream logs the auto-approval at ``DEBUG`` level (``tools/approval.py``
line 1025), which is below production's INFO threshold — so operators
had ZERO visibility into what got auto-approved.

The audit filter promotes those DEBUG records to INFO in place
(without emitting a parallel record — Python 3.12's per-logger
re-entrancy guard makes parallel emission impossible from within a
filter callback). Tests below pin the contract:
- Install lowers ``tools.approval`` to DEBUG so the records are created.
- Install is idempotent.
- Matching DEBUG records are promoted to INFO with the audit prefix.
- Non-matching DEBUG records pass through untouched.
- The filter never drops a record.
"""
from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_approval_logger():
    """Snapshot + restore the ``tools.approval`` logger config so tests
    don't bleed state into each other."""
    logger = logging.getLogger("tools.approval")
    prev_level = logger.level
    prev_filters = list(logger.filters)
    yield
    for f in list(logger.filters):
        if f not in prev_filters:
            logger.removeFilter(f)
    logger.setLevel(prev_level)


class TestSmartApprovalAuditInstall:
    """Smoke + idempotency tests for the installer."""

    def test_install_lowers_tools_approval_logger_to_debug(self):
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        approval_logger.setLevel(logging.WARNING)

        smart_approval_audit.install()

        assert approval_logger.level == logging.DEBUG

    def test_install_is_idempotent(self):
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()
        first_filter_count = len(approval_logger.filters)

        smart_approval_audit.install()
        smart_approval_audit.install()

        assert len(approval_logger.filters) == first_filter_count

    def test_install_attaches_audit_filter_class(self):
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()

        matching = [
            f
            for f in approval_logger.filters
            if isinstance(f, smart_approval_audit._SmartApprovalAuditFilter)
        ]
        assert matching, "audit filter not attached"


class TestSmartApprovalAuditBehavior:
    """The filter promotes matching DEBUG records to INFO in place."""

    def test_filter_promotes_smart_approve_debug_to_info(self):
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()

        matching_filter = next(
            f for f in approval_logger.filters
            if isinstance(f, smart_approval_audit._SmartApprovalAuditFilter)
        )

        record = logging.LogRecord(
            name="tools.approval",
            level=logging.DEBUG,
            pathname=__file__,
            lineno=0,
            msg="Smart approval: auto-approved %r (%s)",
            args=("rm -f /tmp/file.jpg", "delete in root path"),
            exc_info=None,
        )

        result = matching_filter.filter(record)

        assert result is True, "filter must never drop the record"
        assert record.levelno == logging.INFO, (
            "smart-approve DEBUG must be promoted to INFO so production "
            "INFO-level handlers accept it"
        )
        assert record.levelname == "INFO"
        # The args were used to render the message; subsequent
        # getMessage() must NOT re-render with %s placeholders.
        msg = record.getMessage()
        assert "rm -f /tmp/file.jpg" in msg
        assert "delete in root path" in msg
        assert "%s" not in msg, "lazy format args must be pre-rendered"

    def test_filter_promotes_smart_approvals_llm_failure_debug_to_info(self):
        """LLM-failure path (line 786 upstream) — operators need to see
        when the guardian is degraded."""
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()

        matching_filter = next(
            f for f in approval_logger.filters
            if isinstance(f, smart_approval_audit._SmartApprovalAuditFilter)
        )

        record = logging.LogRecord(
            name="tools.approval",
            level=logging.DEBUG,
            pathname=__file__,
            lineno=0,
            msg="Smart approvals: LLM call failed (%s), escalating",
            args=("Connection refused",),
            exc_info=None,
        )

        result = matching_filter.filter(record)

        assert result is True
        assert record.levelno == logging.INFO
        assert "Connection refused" in record.getMessage()

    def test_filter_prepends_audit_prefix(self):
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()

        matching_filter = next(
            f for f in approval_logger.filters
            if isinstance(f, smart_approval_audit._SmartApprovalAuditFilter)
        )

        record = logging.LogRecord(
            name="tools.approval",
            level=logging.DEBUG,
            pathname=__file__,
            lineno=0,
            msg="Smart approval: auto-approved 'x' (y)",
            args=(),
            exc_info=None,
        )

        matching_filter.filter(record)

        assert smart_approval_audit._AUDIT_PREFIX in record.getMessage(), (
            "prefix lets operators grep for audit entries in agent.log"
        )

    def test_filter_ignores_unrelated_debug_records(self):
        """Other DEBUG records from tools.approval (e.g. the
        ``request_action_confirmation`` no-callback note) must NOT be
        promoted — would add noise."""
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()

        matching_filter = next(
            f for f in approval_logger.filters
            if isinstance(f, smart_approval_audit._SmartApprovalAuditFilter)
        )

        record = logging.LogRecord(
            name="tools.approval",
            level=logging.DEBUG,
            pathname=__file__,
            lineno=0,
            msg="request_action_confirmation: no gateway callback for %r, auto-approve",
            args=("session-xyz",),
            exc_info=None,
        )

        result = matching_filter.filter(record)

        assert result is True
        assert record.levelno == logging.DEBUG, (
            "unrelated DEBUG records must NOT be promoted"
        )
        assert record.levelname == "DEBUG"

    def test_filter_ignores_non_debug_records(self):
        """If something already logs at INFO/WARNING with a matching
        prefix, leave it alone — the filter only promotes DEBUG."""
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()

        matching_filter = next(
            f for f in approval_logger.filters
            if isinstance(f, smart_approval_audit._SmartApprovalAuditFilter)
        )

        for original_level in (logging.INFO, logging.WARNING, logging.ERROR):
            record = logging.LogRecord(
                name="tools.approval",
                level=original_level,
                pathname=__file__,
                lineno=0,
                msg="Smart approval: auto-approved 'x' (y)",
                args=(),
                exc_info=None,
            )

            matching_filter.filter(record)

            assert record.levelno == original_level, (
                f"filter must only act on DEBUG records, not "
                f"{logging.getLevelName(original_level)}"
            )

    def test_filter_always_returns_true(self):
        """Returning False would drop the record from the handler chain
        entirely. The filter must never do that — its only effect is
        the side mutation."""
        from myah_hermes_plugin.observability import smart_approval_audit

        approval_logger = logging.getLogger("tools.approval")
        smart_approval_audit.install()

        matching_filter = next(
            f for f in approval_logger.filters
            if isinstance(f, smart_approval_audit._SmartApprovalAuditFilter)
        )

        cases = [
            ("Smart approval: auto-approved 'x' (y)", logging.DEBUG),
            ("Smart approvals: LLM call failed", logging.DEBUG),
            ("unrelated", logging.DEBUG),
            ("Smart approval: ...", logging.INFO),
        ]
        for msg, level in cases:
            record = logging.LogRecord(
                name="tools.approval",
                level=level,
                pathname=__file__,
                lineno=0,
                msg=msg,
                args=(),
                exc_info=None,
            )
            assert matching_filter.filter(record) is True, (
                f"filter returned False for ({msg!r}, "
                f"{logging.getLevelName(level)})"
            )


class TestRegisterCallsInstall:
    """Drift detector: ``myah_platform.register()`` must call
    ``smart_approval_audit.install()``. Without this, the filter is
    inert in production no matter how good the unit tests are."""

    def test_register_calls_smart_approval_audit_install(self, monkeypatch):
        from myah_hermes_plugin import myah_platform
        from myah_hermes_plugin.observability import smart_approval_audit

        calls = {"install": 0}

        def _spy():
            calls["install"] += 1

        monkeypatch.setattr(smart_approval_audit, "install", _spy)

        class _RecordingCtx:
            def __init__(self):
                self.hooks = []

            def register_hook(self, name, fn, **_kwargs):
                self.hooks.append((name, fn))

            def register_tool(self, **_kwargs):
                pass

            def register_command(self, *_a, **_kwargs):
                pass

            def register_skill(self, *_a, **_kwargs):
                pass

        # ``register()`` does a lot more than wire this filter — we
        # tolerate everything else but assert install() was called
        # at least once.
        try:
            myah_platform.register(_RecordingCtx())
        except Exception:
            # Other unrelated bootstrap steps may raise in the test
            # environment (e.g. MYAH_USER_ID resolution against a
            # missing gateway). They are wrapped in try/except in
            # register() anyway. What we care about is whether the
            # install spy was hit before any failure.
            pass

        assert calls["install"] >= 1, (
            "myah_platform.register() did not call "
            "smart_approval_audit.install() — the filter will be "
            "INERT in production. Re-wire it in __init__.py."
        )


class TestSmartApprovalAuditEndToEnd:
    """End-to-end: a real ``tools.approval.debug(...)`` call after
    install() ends up captured at INFO level (the level production's
    handler accepts)."""

    def test_emit_promotes_to_info_in_capture(self, caplog):
        from myah_hermes_plugin.observability import smart_approval_audit

        smart_approval_audit.install()

        with caplog.at_level(logging.DEBUG):
            logging.getLogger("tools.approval").debug(
                "Smart approval: auto-approved %r (%s)",
                "rm -f /tmp/x",
                "delete in root path",
            )

        relevant = [
            r for r in caplog.records
            if r.name == "tools.approval"
            and smart_approval_audit._AUDIT_PREFIX in r.getMessage()
        ]
        assert relevant, (
            "caplog did not capture the audit-promoted record; the "
            "filter's level-promotion isn't reaching the handler"
        )
        assert relevant[0].levelno == logging.INFO
        assert relevant[0].levelname == "INFO"
        assert "rm -f /tmp/x" in relevant[0].getMessage()
