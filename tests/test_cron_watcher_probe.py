"""Task 1.6 — cron_watcher loud-failure + bearer fallback tests.

Per Myah reliability spec §6.2 + spec-review HIGH-5 + CRIT-3:

* **Bearer env-var fallback (Path C):** Hosted containers inject
  ``MYAH_PLATFORM_BEARER`` (containers.py); OSS host writes
  ``MYAH_AGENT_BEARER_TOKEN`` (setup-myah-oss.sh). The watcher
  previously read only the latter and silently no-op'd in hosted prod.
  ``_get_bearer()`` now reads canonical-first, legacy as fallback.

* **Startup probe (Path B):** ``_probe_platform()`` does a GET to
  ``MYAH_PLATFORM_BASE_URL/health`` (root, NOT ``/api/v1/health``;
  GET, NOT HEAD — FastAPI ``@app.get`` is GET-only and would 405 a HEAD).

* **Loud failure:** ``_verify_platform_reachable_or_log()`` logs ERROR
  + Sentry breadcrumb when unreachable; the watcher loop retries every
  30 s indefinitely instead of returning a silent no-op.

* **Per-POST escalation:** consecutive POST failures count up; the 3rd
  consecutive failure triggers ``sentry_sdk.capture_message`` for
  critical-alert escalation. Successful POST resets the counter.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


# ── Task 1.6.A — bearer env var fallback ─────────────────────────────


class TestBearerFallback:
    def test_canonical_takes_priority(self, monkeypatch):
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", "canonical-bearer")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "legacy-bearer")
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        assert cron_watcher._get_bearer() == "canonical-bearer"

    def test_legacy_used_when_canonical_unset(self, monkeypatch):
        monkeypatch.delenv("MYAH_PLATFORM_BEARER", raising=False)
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "legacy-only")
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        assert cron_watcher._get_bearer() == "legacy-only"

    def test_canonical_used_when_legacy_unset(self, monkeypatch):
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", "canonical-only")
        monkeypatch.delenv("MYAH_AGENT_BEARER_TOKEN", raising=False)
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        assert cron_watcher._get_bearer() == "canonical-only"

    def test_empty_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("MYAH_PLATFORM_BEARER", raising=False)
        monkeypatch.delenv("MYAH_AGENT_BEARER_TOKEN", raising=False)
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        assert cron_watcher._get_bearer() == ""


# ── Task 1.6.B — startup probe uses GET, not HEAD ────────────────────


class TestProbeUsesGet:
    def test_probe_uses_get_not_head(self, monkeypatch):
        """FastAPI app.get registers GET only — a HEAD would 405. The
        probe must use GET so it actually verifies reachability."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")

        from myah_hermes_plugin.runtime_extensions import cron_watcher

        captured_urls: list[str] = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(url_or_req, timeout=None):
            # urllib.request.urlopen accepts a str url and uses GET
            # by default. We just record what it was called with.
            captured_urls.append(str(url_or_req))
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = cron_watcher._probe_platform()

        assert ok is True
        assert captured_urls, "probe must call urlopen"
        assert captured_urls[0].endswith("/health"), (
            f"probe URL must end with /health (root), got: {captured_urls[0]!r}"
        )
        assert "/api/v1/health" not in captured_urls[0], (
            "probe must hit /health (root), NOT /api/v1/health "
            "— the FastAPI route lives at the root"
        )

    def test_probe_returns_false_on_no_base_url(self, monkeypatch):
        monkeypatch.delenv("MYAH_PLATFORM_BASE_URL", raising=False)
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        assert cron_watcher._probe_platform() is False

    def test_probe_returns_false_on_exception(self, monkeypatch):
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        from myah_hermes_plugin.runtime_extensions import cron_watcher

        with patch(
            "urllib.request.urlopen", side_effect=OSError("connection refused")
        ):
            assert cron_watcher._probe_platform() is False


# ── Task 1.6.C — verify-or-log: loud failure on unreachable ─────────


class TestVerifyReachable:
    def test_logs_error_when_base_url_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("MYAH_PLATFORM_BASE_URL", raising=False)
        from myah_hermes_plugin.runtime_extensions import cron_watcher

        with caplog.at_level(
            logging.ERROR,
            logger="myah_hermes_plugin.runtime_extensions.cron_watcher",
        ):
            ok = cron_watcher._verify_platform_reachable_or_log()

        assert ok is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "expected an ERROR log on missing base URL"
        joined = " ".join(r.getMessage() for r in errors).lower()
        assert "cron watcher" in joined

    def test_startup_probe_logs_error_on_unreachable(self, monkeypatch, caplog):
        """Patch _probe_platform to return False → verify returns False
        AND logs at ERROR level mentioning 'cron watcher'."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        from myah_hermes_plugin.runtime_extensions import cron_watcher

        with patch.object(cron_watcher, "_probe_platform", return_value=False), \
            caplog.at_level(
                logging.ERROR,
                logger="myah_hermes_plugin.runtime_extensions.cron_watcher",
            ):
            ok = cron_watcher._verify_platform_reachable_or_log()

        assert ok is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "expected ERROR log when probe fails"
        joined = " ".join(r.getMessage() for r in errors).lower()
        assert "cron watcher" in joined
        assert "unreachable" in joined or "platform" in joined


# ── Task 1.6.D — consecutive failure escalation ─────────────────────


class TestConsecutiveFailureEscalation:
    def setup_method(self):
        # Reset module-level counter before each test.
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        cron_watcher._consecutive_post_failures = 0

    def teardown_method(self):
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        cron_watcher._consecutive_post_failures = 0

    def test_consecutive_failures_escalate_at_3(self):
        """Three consecutive _on_post_failure calls → one
        sentry_sdk.capture_message on the third."""
        from myah_hermes_plugin.runtime_extensions import cron_watcher

        fake_sentry = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": fake_sentry}):
            cron_watcher._on_post_failure("job-1", "error 1")
            assert fake_sentry.capture_message.call_count == 0, (
                "1st failure must not capture a message yet"
            )

            cron_watcher._on_post_failure("job-1", "error 2")
            assert fake_sentry.capture_message.call_count == 0, (
                "2nd failure must not capture a message yet"
            )

            cron_watcher._on_post_failure("job-1", "error 3")
            assert fake_sentry.capture_message.call_count == 1, (
                "3rd failure MUST trigger sentry_sdk.capture_message"
            )

    def test_success_resets_counter(self):
        """A successful POST after failures resets the counter; the next
        single failure must NOT escalate."""
        from myah_hermes_plugin.runtime_extensions import cron_watcher

        cron_watcher._on_post_failure("job-1", "err 1")
        cron_watcher._on_post_failure("job-1", "err 2")
        cron_watcher._on_post_success()
        assert cron_watcher._consecutive_post_failures == 0

        fake_sentry = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": fake_sentry}):
            cron_watcher._on_post_failure("job-1", "err 3")
            assert fake_sentry.capture_message.call_count == 0, (
                "counter must reset on success — single failure after reset "
                "must not escalate"
            )
