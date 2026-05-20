"""Task 1.5 — cron Path A: MyahAdapter.send recovers job_id when absent.

Per Myah reliability spec §6.1 Path A: when the cron scheduler invokes
``adapter.send(...)`` without ``meta['job_id']`` set (vanilla upstream
does NOT forward the job dict through ``build_delivery_metadata``), the
adapter must still recover the cron identity and route through the
webhook persistence path — never down the SSE-first branch.

Two signals are checked, in order:

1. **Session contextvar.** The upstream scheduler builds the session key
   as ``f"cron_{job_id}_{strftime}"`` at ``cron/scheduler.py:1319``. The
   ``cron_approval._get_current_session_key()`` helper wraps the upstream
   contextvar read. Parsing: split on ``_`` and take index 1 — the
   ``strftime`` portion can contain underscores but ``job_id`` is
   ``uuid.uuid4().hex[:12]`` (pure hex, no separators).

2. **jobs.json fallback.** When the contextvar is empty, look up jobs
   by ``origin.chat_id`` matching the destination ``chat_id``. If
   multiple match, pick the most recent ``last_run_at``.

Either signal causes ``job_id``, ``job_name``, and ``origin`` to be
injected into metadata and the cron webhook path to be taken.

When neither signal is present, the SSE-first behavior is preserved
unchanged for live chat replies — this is the regression guard.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig


_TEST_AUTH_KEY = "test-bearer-key-for-test_adapter_send_cron_path"
_PLATFORM_BASE_URL = "http://platform:8081"
_PLATFORM_BEARER = "test-bearer-xyz"
_USER_ID = "user-abc"
_CHAT_ID = "chat-recovery-target"
_JOB_ID = "abc123def456"
_JOB_NAME = "test-cron-recovery"


def _make_adapter():
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
    config = PlatformConfig(enabled=True, extra={"auth_key": _TEST_AUTH_KEY})
    return MyahAdapter(config)


class _RecordingResponse:
    def __init__(self, status: int = 200):
        self.status = status

    async def text(self) -> str:
        return '{"ok": true}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingClientSession:
    def __init__(self):
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return _RecordingResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ── Task 1.5 — Path A recovery in MyahAdapter.send ──────────────────


class TestCronJobIdRecovery:
    @pytest.mark.asyncio
    async def test_send_recovers_job_id_from_session_key(self, monkeypatch):
        """When meta lacks job_id but the session contextvar is set,
        adapter must parse job_id from the session_key and take the
        webhook (cron) path."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        # Session key format from upstream cron/scheduler.py:1319 —
        # the job_id is the hex segment between two underscores.
        session_key = f"cron_{_JOB_ID}_20260519_120000"

        # Also have a live SSE stream — recovered cron must STILL
        # take the webhook path (cron always persists via webhook).
        q: asyncio.Queue = asyncio.Queue()
        stream_id = "stream-irrelevant"
        adapter._chat_id_streams[_CHAT_ID] = stream_id
        adapter._streams[stream_id] = q

        recorder = _RecordingClientSession()

        with patch(
            "myah_hermes_plugin.cron_approval._get_current_session_key",
            return_value=session_key,
        ), patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(
                _CHAT_ID,
                "recovered cron output",
                metadata={"thread_id": "thr-1"},  # NO job_id
            )

        assert result.success is True, f"expected success: {result.error!r}"
        assert recorder.posts, (
            "cron recovery must POST to webhook, not just SSE — "
            "got zero posts"
        )
        post = recorder.posts[0]
        assert post["url"].endswith("/api/v1/processes/webhook/run-complete")
        assert post["json"]["job_id"] == _JOB_ID
        assert post["json"]["chat_id"] == _CHAT_ID
        assert post["json"]["response"] == "recovered cron output"

    @pytest.mark.asyncio
    async def test_send_falls_back_to_jobs_json_lookup_by_chat_id(self, monkeypatch):
        """Session contextvar empty → look up jobs.json by chat_id."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        fake_job = {
            "id": _JOB_ID,
            "name": _JOB_NAME,
            "origin": {"platform": "myah", "chat_id": _CHAT_ID},
            "last_run_at": 1700000000.0,
            "last_status": "ok",
        }
        unrelated_job = {
            "id": "otherjob9999",
            "name": "other",
            "origin": {"platform": "myah", "chat_id": "different-chat"},
            "last_run_at": 1700000500.0,
        }

        recorder = _RecordingClientSession()

        with patch(
            "myah_hermes_plugin.cron_approval._get_current_session_key",
            return_value="",
        ), patch(
            "myah_hermes_plugin.myah_platform.adapter._load_cron_jobs_safely",
            return_value=[fake_job, unrelated_job],
        ), patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(
                _CHAT_ID,
                "json fallback content",
                metadata={"thread_id": "thr-2"},
            )

        assert result.success is True, f"expected success: {result.error!r}"
        assert recorder.posts, "jobs.json fallback must trigger webhook POST"
        body = recorder.posts[0]["json"]
        assert body["job_id"] == _JOB_ID
        assert body["job_name"] == _JOB_NAME
        assert body["chat_id"] == _CHAT_ID

    @pytest.mark.asyncio
    async def test_send_picks_most_recent_when_multiple_jobs_match(self, monkeypatch):
        """Multiple jobs share the same chat_id → take the one with the
        most recent ``last_run_at``."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        older = {
            "id": "olderjob1234",
            "name": "older",
            "origin": {"platform": "myah", "chat_id": _CHAT_ID},
            "last_run_at": 1700000000.0,
        }
        newer = {
            "id": "newerjob5678",
            "name": "newer",
            "origin": {"platform": "myah", "chat_id": _CHAT_ID},
            "last_run_at": 1700001000.0,
        }

        recorder = _RecordingClientSession()

        with patch(
            "myah_hermes_plugin.cron_approval._get_current_session_key",
            return_value="",
        ), patch(
            "myah_hermes_plugin.myah_platform.adapter._load_cron_jobs_safely",
            return_value=[older, newer],
        ), patch("aiohttp.ClientSession", return_value=recorder):
            await adapter.send(
                _CHAT_ID,
                "tie-breaker content",
                metadata={},
            )

        assert recorder.posts
        body = recorder.posts[0]["json"]
        assert body["job_id"] == "newerjob5678", (
            f"expected newer job id; got {body['job_id']!r} "
            f"— last_run_at tie-break failed"
        )

    @pytest.mark.asyncio
    async def test_send_takes_sse_path_when_no_cron_signal(self, monkeypatch):
        """No session_key match, no jobs.json match → SSE-first path
        runs unchanged (regression guard for live chat replies)."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        # SSE stream is wired up for the live reply.
        q: asyncio.Queue = asyncio.Queue()
        stream_id = "stream-live-reply"
        adapter._chat_id_streams[_CHAT_ID] = stream_id
        adapter._streams[stream_id] = q

        recorder = _RecordingClientSession()

        with patch(
            "myah_hermes_plugin.cron_approval._get_current_session_key",
            return_value="",
        ), patch(
            "myah_hermes_plugin.myah_platform.adapter._load_cron_jobs_safely",
            return_value=[],
        ), patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(_CHAT_ID, "live reply content")

        assert result.success is True
        assert recorder.posts == [], (
            "no cron signal → no webhook call; live reply must stay SSE-only"
        )
        # Event was queued on the SSE stream.
        event = q.get_nowait()
        assert event["event"] == "message.delta"
        assert event["delta"] == "live reply content"

    @pytest.mark.asyncio
    async def test_send_ignores_non_cron_session_key(self, monkeypatch):
        """A session_key that doesn't start with 'cron_' must NOT trigger
        recovery — chat sessions also have session_keys and we mustn't
        mis-classify them as cron deliveries."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        q: asyncio.Queue = asyncio.Queue()
        stream_id = "stream-chat-session"
        adapter._chat_id_streams[_CHAT_ID] = stream_id
        adapter._streams[stream_id] = q

        recorder = _RecordingClientSession()

        with patch(
            "myah_hermes_plugin.cron_approval._get_current_session_key",
            return_value="chat_some_other_session_key_format",
        ), patch(
            "myah_hermes_plugin.myah_platform.adapter._load_cron_jobs_safely",
            return_value=[],
        ), patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(_CHAT_ID, "chat reply content")

        assert result.success is True
        assert recorder.posts == [], (
            "non-cron session_key must not be parsed as cron recovery"
        )
