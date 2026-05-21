"""Tests for the Myah cron tool boundary validation.

This module covers Bug G + Bug A regressions:

- **Bug G (2026-05-21):** the LLM passes a multi-line Python script body
  as the ``script`` argument instead of a filename. The original
  validator only rejected absolute paths and traversal — it accepted
  content like ``#!/usr/bin/env python3\\nimport json...`` as if it were
  a relative path, then the cron scheduler stored it as a "path" and
  failed at runtime with ``Script not found:
  /data/.hermes/scripts/#!/usr/bin/env python3\\nimport json...``.

- **Bug A (2026-05-21):** one-cron-per-chat constraint and
  deliver-target redaction in ``cronjob list`` output (added in a
  later test class in this file).
"""
from __future__ import annotations

import pytest


# ── Bug G: script-body-as-path validation ───────────────────────────────────


class TestValidateCronScriptPath:
    """Boundary validation for the ``script`` argument of
    ``cronjob(action='create', ...)``. Production occurrence 2026-05-21
    03:12 UTC, job ``775f9c441d66`` (``Hourly random dog picture``):
    the LLM passed the full Python source as ``script``; the cron
    scheduler tried to execute it as a filename and emitted
    ``Script not found: /data/.hermes/scripts/#!/usr/bin/env python3...``.
    """

    def test_rejects_multiline_script_body(self):
        """Multi-line content is unambiguously not a filename."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        multiline_body = (
            "#!/usr/bin/env python3\n"
            "import json, urllib.request, ssl\n"
            "ctx = ssl.create_default_context()\n"
            "with urllib.request.urlopen("
            "'https://dog.ceo/api/breeds/image/random', context=ctx) as r:\n"
            "    data = json.load(r)\n"
            "url = data.get('message', '')\n"
            "print(f'Random dog picture: {url}')\n"
        )

        err = _validate_cron_script_path(multiline_body)
        assert err is not None, (
            "Multi-line script body must be rejected — it's not a filename. "
            "Production saw the body stored as a path, then 'Script not found' "
            "at run time."
        )
        assert "filename" in err.lower() or "path" in err.lower() or "newline" in err.lower()

    def test_rejects_shebang_prefix(self):
        """A bare shebang line is unambiguously content, not a path."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        err = _validate_cron_script_path("#!/usr/bin/env python3")
        assert err is not None, (
            "Shebang-prefixed strings are script bodies, not filenames."
        )

    def test_rejects_path_with_embedded_newline(self):
        """Even short content with a newline is not a filename."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        err = _validate_cron_script_path("my_script.py\nimport os\n")
        assert err is not None

    def test_rejects_path_with_null_byte(self):
        """Null bytes are never valid in POSIX filenames."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        err = _validate_cron_script_path("my_script\x00.py")
        assert err is not None

    def test_accepts_simple_filename(self):
        """Sanity: ordinary filenames are still accepted."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        err = _validate_cron_script_path("hermes_agent_news.py")
        assert err is None, f"Simple filename was rejected: {err}"

    def test_accepts_relative_subdir_filename(self):
        """Sanity: paths within scripts/ are still accepted."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        err = _validate_cron_script_path("daily/news.py")
        assert err is None, f"Relative subdir path was rejected: {err}"

    def test_accepts_empty_or_none(self):
        """Sanity: empty / None means 'clearing the field'."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        assert _validate_cron_script_path(None) is None
        assert _validate_cron_script_path("") is None
        assert _validate_cron_script_path("   ") is None

    def test_existing_rejection_of_absolute_path_still_works(self):
        """Regression: pre-Bug-G validation must not regress."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        assert _validate_cron_script_path("/etc/passwd") is not None
        assert _validate_cron_script_path("~/secrets") is not None

    @pytest.mark.parametrize(
        "bad_filename",
        [
            "../../etc/passwd",
            "../escape.py",
        ],
    )
    def test_existing_traversal_rejection_still_works(self, bad_filename):
        """Regression: pre-Bug-G traversal validation must not regress."""
        from myah_hermes_plugin.myah_tools.cron_tool import _validate_cron_script_path

        err = _validate_cron_script_path(bad_filename)
        assert err is not None, f"Traversal not rejected: {bad_filename!r}"


# ── Bug A: deliver-target resolution helpers ───────────────────────────────


class TestResolveDeliverToChatId:
    """Bug A helper test (2026-05-21). Resolves a job's ``deliver`` field
    to a concrete chat_id so the one-cron-per-chat constraint and the
    deliver-redaction in ``_format_job`` work correctly.
    """

    def test_origin_resolves_to_current_chat(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _resolve_deliver_to_chat_id

        assert _resolve_deliver_to_chat_id("origin", "ca5ecc16") == "ca5ecc16"
        assert _resolve_deliver_to_chat_id("ORIGIN", "ca5ecc16") == "ca5ecc16"
        assert _resolve_deliver_to_chat_id(None, "ca5ecc16") == "ca5ecc16"
        assert _resolve_deliver_to_chat_id("", "ca5ecc16") == "ca5ecc16"

    def test_local_resolves_to_none(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _resolve_deliver_to_chat_id

        assert _resolve_deliver_to_chat_id("local", "ca5ecc16") is None
        assert _resolve_deliver_to_chat_id("LOCAL", "ca5ecc16") is None

    def test_myah_chat_id_resolves_directly(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _resolve_deliver_to_chat_id

        assert _resolve_deliver_to_chat_id("myah:be3ae76a", "ca5ecc16") == "be3ae76a"

    def test_myah_with_thread_id_strips_thread(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _resolve_deliver_to_chat_id

        assert (
            _resolve_deliver_to_chat_id("myah:be3ae76a:thread-1", "ca5ecc16")
            == "be3ae76a"
        )

    def test_unknown_platform_returns_none(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _resolve_deliver_to_chat_id

        assert _resolve_deliver_to_chat_id("discord:abc123", "ca5ecc16") is None
        assert _resolve_deliver_to_chat_id("junk", "ca5ecc16") is None


class TestSafeDeliverDisplay:
    """Bug A redaction test (2026-05-21). The LLM may run ``cronjob list``
    and copy a foreign chat's UUID from the ``deliver`` field into its
    next ``cronjob create`` call. That's the production root cause of the
    wrong-chat-delivery incident on 2026-04-27.

    Fix: never expose another chat's UUID in ``cronjob list`` output.
    Display ``"this chat"`` when the cron delivers here, ``"<other chat>"``
    when it delivers elsewhere, and keep ``"local"`` literal.
    """

    def test_displays_this_chat_for_matching_origin_delivery(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _safe_deliver_display

        assert _safe_deliver_display("origin", "ca5ecc16") == "this chat"
        assert _safe_deliver_display(None, "ca5ecc16") == "this chat"

    def test_displays_this_chat_for_matching_myah_delivery(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _safe_deliver_display

        # deliver points to the same chat we're currently in.
        assert _safe_deliver_display("myah:ca5ecc16", "ca5ecc16") == "this chat"
        assert (
            _safe_deliver_display("myah:ca5ecc16:thread-1", "ca5ecc16")
            == "this chat"
        )

    def test_redacts_uuid_for_other_chat_delivery(self):
        """The critical anti-context-poisoning case."""
        from myah_hermes_plugin.myah_tools.cron_tool import _safe_deliver_display

        result = _safe_deliver_display("myah:be3ae76a", "ca5ecc16")
        assert "be3ae76a" not in result, (
            f"Other chat's UUID leaked: {result!r}. The LLM saw this UUID "
            f"in cronjob list output and copied it into a new cron's "
            f"deliver field, causing wrong-chat delivery (Bug A history)."
        )
        assert "other" in result.lower() or "chat" in result.lower()

    def test_preserves_local_literal(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _safe_deliver_display

        result = _safe_deliver_display("local", "ca5ecc16")
        assert "local" in result.lower()


class TestFindConflictingJobInChat:
    """Bug A constraint test (2026-05-21). One-cron-per-chat: if a chat
    already has a cron, refuse to add another. Detects the conflict by
    resolving each existing job's deliver target.
    """

    def test_returns_none_when_no_jobs(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _find_conflicting_job_in_chat

        assert _find_conflicting_job_in_chat("ca5ecc16", []) is None

    def test_returns_none_when_no_conflict(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _find_conflicting_job_in_chat

        existing = [
            {
                "id": "job-1",
                "name": "Joke teller",
                "deliver": "myah:other-chat-1",
                "origin": {"chat_id": "other-chat-1"},
            },
        ]
        assert _find_conflicting_job_in_chat("ca5ecc16", existing) is None

    def test_finds_conflict_by_deliver_field(self):
        from myah_hermes_plugin.myah_tools.cron_tool import _find_conflicting_job_in_chat

        existing = [
            {
                "id": "job-1",
                "name": "Joke teller",
                "deliver": "myah:ca5ecc16",
                "origin": {"chat_id": "different-chat"},
            },
        ]
        conflict = _find_conflicting_job_in_chat("ca5ecc16", existing)
        assert conflict is not None
        assert conflict["name"] == "Joke teller"

    def test_finds_conflict_with_origin_deliver(self):
        """A job with deliver='origin' resolves to its own origin.chat_id."""
        from myah_hermes_plugin.myah_tools.cron_tool import _find_conflicting_job_in_chat

        existing = [
            {
                "id": "job-1",
                "name": "Daily news",
                "deliver": "origin",
                "origin": {"chat_id": "ca5ecc16"},
            },
        ]
        conflict = _find_conflicting_job_in_chat("ca5ecc16", existing)
        assert conflict is not None
        assert conflict["name"] == "Daily news"

    def test_local_delivery_does_not_conflict(self):
        """deliver='local' has no chat target — never conflicts."""
        from myah_hermes_plugin.myah_tools.cron_tool import _find_conflicting_job_in_chat

        existing = [
            {
                "id": "job-1",
                "name": "Background data sync",
                "deliver": "local",
                "origin": {"chat_id": "ca5ecc16"},
            },
        ]
        assert _find_conflicting_job_in_chat("ca5ecc16", existing) is None


# ── Bug A: integration of _format_job redaction ────────────────────────────


class TestFormatJobDeliverRedaction:
    """Bug A: ``_format_job`` MUST not leak other chats' UUIDs in its
    ``deliver`` output (used by ``cronjob list``)."""

    def test_format_job_redacts_other_chat_uuid_in_deliver(self, monkeypatch):
        """Regression: 2026-04-27 wrong-chat delivery. The cron listing
        leaked ``deliver: myah:be3ae76a-...`` to the LLM in a chat whose
        origin was a different UUID; the LLM copied that UUID verbatim
        into a new cron's deliver field."""
        from myah_hermes_plugin.myah_tools import cron_tool

        # Pretend we're currently in chat ca5ecc16.
        monkeypatch.setattr(
            cron_tool,
            "_origin_from_env",
            lambda: {
                "platform": "myah",
                "chat_id": "ca5ecc16",
                "chat_name": None,
                "thread_id": None,
            },
        )

        # An existing cron that delivers to a DIFFERENT chat.
        job_dict = {
            "id": "44fde84ef629",
            "name": "Hermes Agent daily news",
            "prompt": "...",
            "schedule_display": "0 12 * * *",
            "deliver": "myah:be3ae76a-3e37-4a7a-89a9-e2cd0968393f",
            "next_run_at": "2026-05-22T12:00:00+00:00",
        }

        formatted = cron_tool._format_job(job_dict)

        assert "be3ae76a" not in str(formatted["deliver"]), (
            f"Other chat's UUID leaked in formatted deliver: "
            f"{formatted['deliver']!r}. This is the context-poisoning "
            f"vector that caused wrong-chat delivery on 2026-04-27."
        )


# ── Bug A: one-cron-per-chat constraint integration ───────────────────────


class TestCronjobCreateOneCronPerChat:
    """Bug A constraint integration test (2026-05-21). The ``cronjob``
    create action MUST refuse to create a second cron in a chat that
    already has one, and the error must include the existing cron's
    name so the LLM can explain to the user."""

    def test_create_in_chat_without_existing_cron_succeeds(self, monkeypatch):
        """Sanity: when no existing cron in the target chat, creation proceeds."""
        from myah_hermes_plugin.myah_tools import cron_tool

        monkeypatch.setattr(
            cron_tool, "_origin_from_env",
            lambda: {"platform": "myah", "chat_id": "chat-new", "chat_name": None, "thread_id": None},
        )
        monkeypatch.setattr(cron_tool, "list_jobs", lambda *a, **kw: [])
        captured = {}

        def _fake_create_job(**kwargs):
            captured.update(kwargs)
            return {
                "id": "new-job-1",
                "name": kwargs.get("name") or "Unnamed",
                "schedule_display": "every 60m",
                "next_run_at": "2026-05-21T13:00:00+00:00",
                "deliver": kwargs.get("deliver") or "origin",
                "repeat": {"completed": 0},
            }

        monkeypatch.setattr(cron_tool, "create_job", _fake_create_job)

        import json
        result_str = cron_tool.cronjob(
            action="create",
            schedule="*/60 * * * *",
            prompt="Tell me a joke every hour.",
        )
        result = json.loads(result_str)
        assert result.get("success") is True, (
            f"Expected creation to succeed; got: {result}"
        )
        assert captured.get("prompt") == "Tell me a joke every hour."

    def test_create_in_chat_with_existing_cron_returns_error(self, monkeypatch):
        """The critical constraint test."""
        from myah_hermes_plugin.myah_tools import cron_tool

        # Pretend we're in chat 'ca5ecc16' and that chat ALREADY has a cron.
        monkeypatch.setattr(
            cron_tool, "_origin_from_env",
            lambda: {"platform": "myah", "chat_id": "ca5ecc16", "chat_name": None, "thread_id": None},
        )
        existing_job = {
            "id": "existing-job",
            "name": "Hourly joke",
            "deliver": "origin",
            "origin": {"chat_id": "ca5ecc16"},
        }
        monkeypatch.setattr(cron_tool, "list_jobs", lambda *a, **kw: [existing_job])

        created = []

        def _fail_if_called(**kwargs):
            created.append(kwargs)
            return {"id": "should-not-exist"}

        monkeypatch.setattr(cron_tool, "create_job", _fail_if_called)

        import json
        result_str = cron_tool.cronjob(
            action="create",
            schedule="0 12 * * *",
            prompt="Daily news summary.",
        )
        result = json.loads(result_str)
        assert result.get("success") is False, (
            f"Expected creation to be refused; got: {result}"
        )
        # Error message must hint at the existing cron and the user-actionable
        # remedy (new chat / update existing).
        error_text = (result.get("error") or "").lower()
        assert "hourly joke" in error_text or "existing" in error_text or "already" in error_text, (
            f"Error must mention the conflict: {result}"
        )
        # Most important: create_job MUST NOT be called.
        assert created == [], (
            "create_job was called despite the conflict — the constraint "
            "did not engage. Production scenario where two crons end up "
            "in the same chat will recur."
        )

    def test_explicit_deliver_to_other_chat_with_cron_returns_error(self, monkeypatch):
        """The LLM may set deliver='myah:<chat_id>' explicitly. The
        constraint must still engage if that target chat already has a cron."""
        from myah_hermes_plugin.myah_tools import cron_tool

        monkeypatch.setattr(
            cron_tool, "_origin_from_env",
            lambda: {"platform": "myah", "chat_id": "current-chat", "chat_name": None, "thread_id": None},
        )
        existing_job = {
            "id": "existing-job",
            "name": "Daily news",
            "deliver": "myah:target-chat",
            "origin": {"chat_id": "old-chat"},
        }
        monkeypatch.setattr(cron_tool, "list_jobs", lambda *a, **kw: [existing_job])
        monkeypatch.setattr(cron_tool, "create_job", lambda **kw: pytest.fail("should not create"))

        import json
        result_str = cron_tool.cronjob(
            action="create",
            schedule="0 9 * * *",
            prompt="Morning briefing.",
            deliver="myah:target-chat",
        )
        result = json.loads(result_str)
        assert result.get("success") is False

    def test_local_delivery_does_not_conflict_with_chat_crons(self, monkeypatch):
        """deliver='local' has no chat target and must not be blocked by
        existing chat crons."""
        from myah_hermes_plugin.myah_tools import cron_tool

        monkeypatch.setattr(
            cron_tool, "_origin_from_env",
            lambda: {"platform": "myah", "chat_id": "ca5ecc16", "chat_name": None, "thread_id": None},
        )
        existing_chat_cron = {
            "id": "chat-cron",
            "name": "Existing chat cron",
            "deliver": "origin",
            "origin": {"chat_id": "ca5ecc16"},
        }
        monkeypatch.setattr(cron_tool, "list_jobs", lambda *a, **kw: [existing_chat_cron])

        called = {"yes": False}

        def _fake_create(**kw):
            called["yes"] = True
            return {
                "id": "local-job",
                "name": kw.get("name") or "Local",
                "schedule_display": "every 30m",
                "next_run_at": "2026-05-21T13:30:00+00:00",
                "deliver": "local",
                "repeat": {"completed": 0},
            }

        monkeypatch.setattr(cron_tool, "create_job", _fake_create)

        import json
        result_str = cron_tool.cronjob(
            action="create",
            schedule="*/30 * * * *",
            prompt="Background sync.",
            deliver="local",
        )
        result = json.loads(result_str)
        assert result.get("success") is True, (
            f"Local delivery was incorrectly blocked: {result}"
        )
        assert called["yes"] is True
