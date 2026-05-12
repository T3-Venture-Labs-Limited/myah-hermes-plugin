"""Test for Bug D v3: MyahAdapter.send() must fall back to a webhook POST
when no live SSE subscriber is bound to the chat.

Before the fix, ``MyahAdapter.send(chat_id, content, ...)`` returned
``SendResult(success=False, error="No active stream for chat_id=...")``
when the user wasn't actively viewing the chat.  Cron output ran on
schedule but never reached the chat history because every scheduled
delivery happened while the browser tab was closed.

The v3 fix keeps ``cron/scheduler.py`` upstream-clean (Option B —
adapter-owned delivery, mirroring ``gateway/platforms/webhook.py``).  All
Myah-specific delivery code lives in ``MyahAdapter.send()`` itself:

* If a live SSE stream exists → push ``message.delta`` (existing behaviour).
* Otherwise, if ``MYAH_PLATFORM_BASE_URL`` and ``MYAH_PLATFORM_BEARER`` env
  vars are set, POST the cron output to
  ``{base}/api/v1/processes/webhook/run-complete`` so the platform writes
  it to chat history.
* If env vars are missing → preserve original ``"No active stream"`` failure.
* HTTP failure → ``logger.warning`` + Sentry breadcrumb (level="warning"),
  never raise.

The threading bridge — when called from the cron ThreadPoolExecutor
worker — is verified by exercising ``send()`` while ``self._loop`` is
running in the test event loop (the helper uses
``asyncio.run_coroutine_threadsafe`` internally).  Calling from the loop
thread directly (as our tests do) still works.
"""

import asyncio
import logging
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig


_PLATFORM_BASE_URL = "http://platform:8081"
_PLATFORM_BEARER = "test-bearer-xyz"
_USER_ID = "user-abc"
_CHAT_ID = "chat-123"
_JOB_ID = "job-deadbeef"
_JOB_NAME = "test-cron-job"


def _make_adapter(auth_key: str = ""):
    extra = dict()
    if auth_key:
        extra["auth_key"] = auth_key
    config = PlatformConfig(enabled=True, extra=extra)
    with patch("gateway.platforms.api_server.register_pre_setup_hook"):
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
        return MyahAdapter(config)


class _RecordingResponse:
    """Stand-in for an aiohttp ClientResponse used inside an async ctx."""

    def __init__(self, status: int = 200, text: str = "{}"):
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingClientSession:
    """Minimal async context-manager that records a single POST call."""

    def __init__(self, response: _RecordingResponse | None = None, raise_exc: Exception | None = None):
        self.posts: list[dict] = []
        self._response = response or _RecordingResponse(status=200, text='{"ok": true}')
        self._raise_exc = raise_exc

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        if self._raise_exc:
            raise self._raise_exc
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ── Myah: Bug D v3 — adapter-owned offline delivery fallback ─────


class TestSendOfflineWebhookFallback:
    @pytest.mark.asyncio
    async def test_no_stream_with_env_posts_to_webhook(self, monkeypatch):
        """When no SSE stream is active but MYAH_PLATFORM_* env is set,
        send() must POST to the platform webhook with the right payload."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()

        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        recorder = _RecordingClientSession()

        with patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(
                _CHAT_ID,
                "the cron output content",
                metadata={
                    "job_id": _JOB_ID,
                    "job_name": _JOB_NAME,
                    "status": "ok",
                    "ran_at": "2026-04-24T10:00:00Z",
                },
            )

        assert result.success is True, f"expected success, got error: {result.error!r}"
        assert recorder.posts, "expected exactly one webhook POST"
        post = recorder.posts[0]

        assert post["url"] == f"{_PLATFORM_BASE_URL}/api/v1/processes/webhook/run-complete", (
            f"wrong webhook URL: {post['url']}"
        )
        assert post["headers"].get("Authorization") == f"Bearer {_PLATFORM_BEARER}"

        body = post["json"]
        # Payload contract — pinned by platform's processes.py:824-831
        assert body["user_id"] == _USER_ID
        assert body["job_id"] == _JOB_ID
        assert body["job_name"] == _JOB_NAME
        assert body["chat_id"] == _CHAT_ID
        assert body["response"] == "the cron output content"
        assert body["status"] == "ok"
        assert body["ran_at"] == "2026-04-24T10:00:00Z"

    @pytest.mark.asyncio
    async def test_active_stream_chat_reply_pushes_sse_only(self):
        """Regression guard for live chat replies (no cron metadata):
        when a live stream exists, the SSE path runs and no HTTP call
        is made.  Webhook is cron-only — it would 400 on a chat reply."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        stream_id = "stream-online"
        adapter._chat_id_streams[_CHAT_ID] = stream_id
        adapter._streams[stream_id] = q

        recorder = _RecordingClientSession()
        with patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(_CHAT_ID, "hello live")

        assert result.success is True
        # Event was queued
        event = q.get_nowait()
        assert event["event"] == "message.delta"
        assert event["delta"] == "hello live"
        # NO http call — webhook is cron-only.
        assert recorder.posts == []

    @pytest.mark.asyncio
    async def test_cron_delivery_with_active_stream_still_uses_webhook(self, monkeypatch):
        """Bug D v4 fix: cron deliveries (metadata.job_id present) MUST
        go through the webhook even when an SSE stream is active.  SSE
        is a transient render buffer for whatever conversation turn is
        in flight — concatenating cron content into it corrupts the
        active turn AND silently loses persistence (only the webhook
        path calls _inject_cron_output_to_chat).

        SSE push is still attempted as a live-preview decoration, but
        the return value must reflect the webhook outcome."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        # Live stream exists for this chat (e.g. user just sent
        # "test it here" and that turn is still streaming).
        q: asyncio.Queue = asyncio.Queue()
        stream_id = "stream-active-during-cron"
        adapter._chat_id_streams[_CHAT_ID] = stream_id
        adapter._streams[stream_id] = q

        recorder = _RecordingClientSession()
        with patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(
                _CHAT_ID,
                "cron output content",
                metadata={
                    "job_id": _JOB_ID,
                    "job_name": _JOB_NAME,
                    "status": "ok",
                    "ran_at": "2026-04-25T14:04:02Z",
                },
            )

        # The webhook POST happened — that's the critical persistence path.
        assert result.success is True, f"webhook should succeed: {result.error!r}"
        assert recorder.posts, (
            "cron delivery must POST to the webhook even with an active stream — "
            "the SSE queue is for an unrelated conversation turn"
        )
        post = recorder.posts[0]
        assert post["url"].endswith("/api/v1/processes/webhook/run-complete")
        assert post["json"]["job_id"] == _JOB_ID
        assert post["json"]["chat_id"] == _CHAT_ID
        assert post["json"]["response"] == "cron output content"

        # Live preview (decoration) — was the SSE event also pushed?
        # Either is acceptable: agents may want to live-preview cron
        # output to a watching user.  We don't assert "no SSE", we just
        # assert that the webhook is the source of truth.
        # (The current implementation pushes for live preview when a
        # consumer might be active — verify only that it doesn't crash.)
        # No further assertion on q contents.

    @pytest.mark.asyncio
    async def test_no_stream_and_no_env_preserves_old_failure(self, monkeypatch):
        """When neither stream nor MYAH_PLATFORM_* env is configured, fall
        back to the original ``No active stream`` failure (don't crash)."""
        adapter = _make_adapter()
        monkeypatch.delenv("MYAH_PLATFORM_BASE_URL", raising=False)
        monkeypatch.delenv("MYAH_PLATFORM_BEARER", raising=False)

        result = await adapter.send("nonexistent-chat", "hello")
        assert result.success is False
        assert "No active stream" in (result.error or "")

    @pytest.mark.asyncio
    async def test_chat_id_falls_back_to_origin_chat_id(self, monkeypatch):
        """When the caller passes empty chat_id but metadata.origin.chat_id is
        set, use the origin chat_id for the webhook delivery (cron path
        passes origin metadata when deliver=origin)."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        recorder = _RecordingClientSession()
        with patch("aiohttp.ClientSession", return_value=recorder):
            result = await adapter.send(
                "",
                "fallback content",
                metadata={
                    "job_id": _JOB_ID,
                    "job_name": _JOB_NAME,
                    "status": "ok",
                    "ran_at": "2026-04-24T10:00:00Z",
                    "origin": {"platform": "myah", "chat_id": "origin-chat-zzz"},
                },
            )

        assert result.success is True
        body = recorder.posts[0]["json"]
        assert body["chat_id"] == "origin-chat-zzz"

    @pytest.mark.asyncio
    async def test_webhook_500_logs_warning_no_raise(self, monkeypatch, caplog):
        """Non-2xx response → log warning, never raise."""
        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        recorder = _RecordingClientSession(response=_RecordingResponse(status=500, text="boom"))

        with patch("aiohttp.ClientSession", return_value=recorder), \
             caplog.at_level(logging.WARNING, logger="myah_hermes_plugin.myah_platform.adapter"):
            result = await adapter.send(_CHAT_ID, "content", metadata={
                "job_id": _JOB_ID,
                "job_name": _JOB_NAME,
                "status": "ok",
                "ran_at": "now",
            })

        # Either success=False OR an error attribute set; must NOT raise
        assert result.success is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "expected at least one WARNING about webhook failure"
        joined = " ".join(r.getMessage() for r in warnings).lower()
        assert "webhook" in joined or "500" in joined or "delivery" in joined

    @pytest.mark.asyncio
    async def test_webhook_client_error_logs_warning_no_raise(self, monkeypatch, caplog):
        """aiohttp.ClientError or asyncio.TimeoutError → log warning, never raise."""
        import aiohttp

        adapter = _make_adapter()
        adapter._loop = asyncio.get_running_loop()
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", _PLATFORM_BASE_URL)
        monkeypatch.setenv("MYAH_PLATFORM_BEARER", _PLATFORM_BEARER)
        monkeypatch.setenv("MYAH_USER_ID", _USER_ID)

        recorder = _RecordingClientSession(raise_exc=aiohttp.ClientConnectionError("connection refused"))

        with patch("aiohttp.ClientSession", return_value=recorder), \
             caplog.at_level(logging.WARNING, logger="myah_hermes_plugin.myah_platform.adapter"):
            result = await adapter.send(_CHAT_ID, "content", metadata={
                "job_id": _JOB_ID,
                "job_name": _JOB_NAME,
                "status": "ok",
                "ran_at": "now",
            })

        assert result.success is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "expected at least one WARNING on connection error"
# ─────────────────────────────────────────────────────────────────
