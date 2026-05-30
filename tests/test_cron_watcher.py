"""F6 cron→chat delivery watcher tests.

The watcher polls ~/.hermes/cron/output/{job_id}/*.md and POSTs each
new file to the platform's run-complete webhook. Tests cover:
1. New files trigger a webhook POST.
2. Already-seen files do NOT re-trigger.
3. Old files (predating watcher start) are NOT replayed.
4. Non-myah-origin jobs are NOT delivered (telegram/discord cron
   should remain telegram/discord-delivered).
5. Missing env vars are graceful no-ops.
6. Vanilla-API CI guards: OUTPUT_DIR exists, load_jobs is callable.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def fake_output_dir(tmp_path, monkeypatch):
    """Set up a fake ~/.hermes/cron/output/ directory.

    Also resets cron_watcher module state and pre-creates an EMPTY
    persistent seen-state file. Empty state signals "post-first-run,
    we've delivered nothing so far → anything new IS new", which is
    the contract these tests exercise. (A missing state file would
    trigger the first-run seed-without-delivery path covered by the
    test_cron_watcher_persistent_state.py suite.)
    """
    import json
    output_dir = tmp_path / "cron" / "output"
    output_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.OUTPUT_DIR",
        output_dir,
        raising=False,
    )
    # Reset module state every test so order-of-execution doesn't
    # bleed across cases.
    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    cron_watcher._state_loaded = False
    # Pre-create empty state — simulates post-first-run.
    state_file = output_dir.parent / ".watcher-seen.json"
    state_file.write_text(json.dumps({"version": 1, "seen": {}}))
    return output_dir


@pytest.fixture
def fake_jobs():
    """Patch load_jobs to return our test job set."""
    job = {
        "id": "abc123def456",
        "name": "daily-summary",
        "origin": {"platform": "myah", "chat_id": "chat-xyz"},
        "last_status": "ok",
    }
    with patch(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.load_jobs",
        return_value=[job],
    ):
        yield job


@pytest.mark.asyncio
async def test_new_file_triggers_webhook(fake_output_dir, fake_jobs, monkeypatch):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
    monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("MYAH_USER_ID", "user-123")

    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    output_file = job_dir / "2026-05-10_12-00-00.md"
    output_file.write_text("Daily summary: all systems nominal.")

    posted = []

    class FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            posted.append({"url": url, "json": json, "headers": headers})
            return FakeResp()

    with patch("aiohttp.ClientSession", FakeSession):
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        cron_watcher._seen_mtimes.clear()
        await cron_watcher._tick("http://platform.test", "test-token")

    assert len(posted) == 1
    assert posted[0]["url"].endswith("/api/v1/processes/webhook/run-complete")
    payload = posted[0]["json"]
    assert payload["job_id"] == "abc123def456"
    assert payload["job_name"] == "daily-summary"
    assert payload["chat_id"] == "chat-xyz"
    assert payload["user_id"] == "user-123"
    assert "Daily summary" in payload["response"]
    assert payload["status"] == "ok"
    assert payload["tool_calls_log"] is None
    assert payload["run_id"] == "2026-05-10_12-00-00"


@pytest.mark.asyncio
async def test_seen_file_does_not_redeliver(fake_output_dir, fake_jobs, monkeypatch):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
    monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("MYAH_USER_ID", "user-123")

    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    output_file = job_dir / "2026-05-10_12-00-00.md"
    output_file.write_text("First run output")

    posted = []

    class FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def post(self, *a, **kw):
            posted.append(1)
            return FakeResp()

    with patch("aiohttp.ClientSession", FakeSession):
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        cron_watcher._seen_mtimes.clear()
        await cron_watcher._tick("http://platform.test", "test-token")
        await cron_watcher._tick("http://platform.test", "test-token")
        await cron_watcher._tick("http://platform.test", "test-token")

    assert len(posted) == 1, f"expected 1 delivery, got {len(posted)}"


@pytest.mark.asyncio
async def test_non_myah_origin_skipped(fake_output_dir, monkeypatch):
    monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
    monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("MYAH_USER_ID", "user-123")

    telegram_job = {
        "id": "tg999",
        "name": "tg-cron",
        "origin": {"platform": "telegram", "chat_id": "tg-chat-1"},
    }
    with patch(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.load_jobs",
        return_value=[telegram_job],
    ):
        job_dir = fake_output_dir / "tg999"
        job_dir.mkdir()
        (job_dir / "2026-05-10_12-00-00.md").write_text("tg output")

        posted = []
        class FakeResp:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        class FakeSession:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def post(self, *a, **kw):
                posted.append(1)
                return FakeResp()

        with patch("aiohttp.ClientSession", FakeSession):
            from myah_hermes_plugin.runtime_extensions import cron_watcher
            cron_watcher._seen_mtimes.clear()
            await cron_watcher._tick("http://platform.test", "test-token")

    assert len(posted) == 0, "non-myah origin should not be delivered to platform"


def test_vanilla_api_ci_guard():
    """Catches upstream API drift on cron.jobs.OUTPUT_DIR / load_jobs."""
    from cron import jobs as cron_jobs
    assert hasattr(cron_jobs, "OUTPUT_DIR"), "vanilla cron.jobs.OUTPUT_DIR removed?"
    assert hasattr(cron_jobs, "load_jobs"), "vanilla cron.jobs.load_jobs removed?"
    assert callable(cron_jobs.load_jobs)
    # Path layout assumption: OUTPUT_DIR / {job_id} / {timestamp}.md
    assert isinstance(cron_jobs.OUTPUT_DIR, Path)

@pytest.mark.asyncio
async def test_watcher_delivers_adopted_job_with_myah_chat_id(fake_output_dir, monkeypatch):
    """A legacy cron with external origin but job.myah.chat_id is delivered
    to Myah without overwriting/paying attention to native origin."""
    adopted_job = {
        "id": "abc123def456",
        "name": "adopted-cron",
        "origin": {"platform": "telegram", "chat_id": "tg-chat"},
        "myah": {"chat_id": "myah-chat-1"},
    }
    monkeypatch.setattr(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.load_jobs",
        lambda: [adopted_job],
    )
    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    (job_dir / "2026-05-10_12-00-00.md").write_text("adopted output")

    posted = []

    class FakeResponse:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, url, json=None, headers=None):
            posted.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(
        "aiohttp.ClientSession",
        FakeSession,
    )

    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    await cron_watcher._tick("http://platform.test", "test-token")

    assert len(posted) == 1
    assert posted[0]["json"]["job_id"] == "abc123def456"
    assert posted[0]["json"]["chat_id"] == "myah-chat-1"
    assert posted[0]["json"]["origin"]["platform"] == "telegram"


@pytest.mark.asyncio
async def test_non_myah_skipped_files_are_marked_seen_not_retried(fake_output_dir, monkeypatch):
    """External, non-adopted jobs are intentional skips and should be
    recorded as seen so adoption/backfill does not later replay them."""
    external_job = {
        "id": "abc123def456",
        "name": "telegram-cron",
        "origin": {"platform": "telegram", "chat_id": "tg-chat"},
    }
    monkeypatch.setattr(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.load_jobs",
        lambda: [external_job],
    )
    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    output_file = job_dir / "2026-05-10_12-00-00.md"
    output_file.write_text("telegram output")

    posted = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, url, json=None, headers=None):
            posted.append(json)
            raise AssertionError("external non-adopted cron should not post")

    monkeypatch.setattr(
        "aiohttp.ClientSession",
        FakeSession,
    )

    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    await cron_watcher._tick("http://platform.test", "test-token")
    await cron_watcher._tick("http://platform.test", "test-token")

    assert posted == []
    assert cron_watcher._seen_mtimes.get(output_file) == output_file.stat().st_mtime


@pytest.mark.asyncio
async def test_transient_webhook_failure_is_not_marked_seen(fake_output_dir, fake_jobs, monkeypatch):
    """A Myah/adopted job with a transient webhook failure remains retryable."""
    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    output_file = job_dir / "2026-05-10_12-00-00.md"
    output_file.write_text("myah output")

    class FakeResponse:
        status = 502
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, url, json=None, headers=None):
            return FakeResponse()

    monkeypatch.setattr(
        "aiohttp.ClientSession",
        FakeSession,
    )

    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    await cron_watcher._tick("http://platform.test", "test-token")

    assert output_file not in cron_watcher._seen_mtimes


@pytest.mark.asyncio
async def test_unreadable_myah_output_file_is_retryable(fake_output_dir, fake_jobs, monkeypatch):
    """A temporary filesystem/read error must not permanently mark a Myah
    delivery as seen. The next tick should get another chance to post it."""
    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    output_file = job_dir / "2026-05-10_12-00-00.md"
    output_file.write_text("myah output")

    original_read_text = Path.read_text

    def fail_output_read(self, *args, **kwargs):
        if self == output_file:
            raise OSError("temporary read failure")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_output_read)

    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    await cron_watcher._tick("http://platform.test", "test-token")

    assert output_file not in cron_watcher._seen_mtimes


@pytest.mark.asyncio
async def test_myah_routed_job_missing_chat_id_is_retryable(fake_output_dir, monkeypatch):
    """Missing Myah chat metadata is repairable (for example while adoption
    metadata is being written), so do not permanently skip the output."""
    job = {
        "id": "abc123def456",
        "name": "half-adopted-cron",
        "origin": {"platform": "myah"},
    }
    monkeypatch.setattr(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.load_jobs",
        lambda: [job],
    )
    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    output_file = job_dir / "2026-05-10_12-00-00.md"
    output_file.write_text("myah output")

    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    await cron_watcher._tick("http://platform.test", "test-token")

    assert output_file not in cron_watcher._seen_mtimes

@pytest.mark.asyncio
async def test_watcher_prefers_myah_metadata_over_native_origin(fake_output_dir, monkeypatch):
    """Adoption metadata is the Myah-owned routing source of truth; when both
    native origin and job.myah exist, route to job.myah.chat_id."""
    job = {
        "id": "abc123def456",
        "name": "repair-cron",
        "origin": {"platform": "myah", "chat_id": "stale-origin-chat"},
        "myah": {"chat_id": "adopted-chat"},
    }
    monkeypatch.setattr(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.load_jobs",
        lambda: [job],
    )
    job_dir = fake_output_dir / "abc123def456"
    job_dir.mkdir()
    (job_dir / "2026-05-10_13-00-00.md").write_text("repair output")

    posted = []

    class FakeResponse:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, url, json=None, headers=None):
            posted.append(json)
            return FakeResponse()

    monkeypatch.setattr("aiohttp.ClientSession", FakeSession)

    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    await cron_watcher._tick("http://platform.test", "test-token")

    assert len(posted) == 1
    assert posted[0]["chat_id"] == "adopted-chat"

