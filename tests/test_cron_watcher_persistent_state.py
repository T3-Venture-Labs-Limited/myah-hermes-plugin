"""Persistent ``_seen_mtimes`` state — fix for the data-loss-on-restart class.

Background (2026-05-22): the cron output watcher kept ``_seen_mtimes``
purely in-memory and applied a ``_BOOTSTRAP_AGE_SECS = 60`` cutoff on
first tick: any output file older than 60 seconds when the watcher
started was silently seeded into ``_seen_mtimes`` without delivery.

This caused real production data loss on container restart:

* Container restarted at 07:51Z (deploy)
* Cron output files were written at 07:57Z (3 files)
* Watcher started at 07:59Z (lazy via ``pre_gateway_dispatch`` hook
  waits for first chat dispatch — typically >60s after container
  creation if the user isn't actively chatting at the moment of
  restart)
* First tick at 07:59: ``cutoff = 07:58:23``. All three 07:57 files
  are older than the cutoff → seeded as "historical" → silently lost.

The fix replaces the age-based cutoff with a **persistent seen-state
file** at ``OUTPUT_DIR.parent / ".watcher-seen.json"`` that survives
restarts. Behavior contract:

* If the state file exists: trust it. Any file in the dir whose
  ``(path, mtime)`` pair is NOT in the state is delivered, regardless
  of age.
* If the state file does NOT exist (fresh install): seed-all behavior
  — record all currently-present files into the state WITHOUT
  delivering. This protects fresh installs from replaying years of
  historical output, while still guaranteeing zero data loss on every
  subsequent restart.
* The state file is written atomically (tmp + rename) after every
  successful delivery and after the initial seed.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def watcher_dirs(tmp_path, monkeypatch):
    """Set OUTPUT_DIR to a tmp dir + reset module state so each test
    starts from a known empty state. Returns the output_dir Path.

    Also resets the module-level _seen_mtimes + _state_loaded flag
    that the new implementation will introduce."""
    output_dir = tmp_path / "cron" / "output"
    output_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.OUTPUT_DIR",
        output_dir,
        raising=False,
    )
    # The state file lives alongside OUTPUT_DIR's parent — same
    # directory tree as ``jobs.json``. Match the convention.
    from myah_hermes_plugin.runtime_extensions import cron_watcher
    cron_watcher._seen_mtimes.clear()
    if hasattr(cron_watcher, "_state_loaded"):
        cron_watcher._state_loaded = False
    return output_dir


@pytest.fixture
def fake_job():
    """Return a single myah-origin job; patch load_jobs to return it."""
    job = {
        "id": "joba",
        "name": "test-job",
        "origin": {"platform": "myah", "chat_id": "chat-xyz"},
        "last_status": "ok",
    }
    with patch(
        "myah_hermes_plugin.runtime_extensions.cron_watcher.load_jobs",
        return_value=[job],
    ):
        yield job


@pytest.fixture
def fake_aiohttp():
    """Stand-in for aiohttp.ClientSession that records every POST."""
    posted: list[dict] = []

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
        yield posted


# ─── State file contract ─────────────────────────────────────────────


class TestStateFileContract:
    def test_state_file_lives_next_to_output_dir(self, watcher_dirs):
        from myah_hermes_plugin.runtime_extensions import cron_watcher

        expected = watcher_dirs.parent / ".watcher-seen.json"
        assert cron_watcher._STATE_FILE == expected, (
            "state file must live at OUTPUT_DIR.parent / '.watcher-seen.json' "
            "so it sits alongside jobs.json under /data/.hermes/cron/"
        )

    def test_state_format_is_versioned_json(
        self, watcher_dirs, monkeypatch, fake_job, fake_aiohttp
    ):
        """After a delivery, the state file must be valid JSON with a
        version field — so future format migrations are detectable."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MYAH_USER_ID", "user-123")

        job_dir = watcher_dirs / "joba"
        job_dir.mkdir()
        f = job_dir / "out.md"
        f.write_text("Output")

        import asyncio

        from myah_hermes_plugin.runtime_extensions import cron_watcher
        # State file doesn't exist yet — first tick should create it
        # via the initial-seed path; the file we just created is at
        # tick time, so this is the "first run" scenario.
        asyncio.run(cron_watcher._tick("http://platform.test", "test-token"))

        state_file = watcher_dirs.parent / ".watcher-seen.json"
        assert state_file.exists(), (
            "state file must be created on first tick"
        )
        data = json.loads(state_file.read_text())
        assert isinstance(data, dict)
        assert data.get("version") == 1
        assert "seen" in data
        assert isinstance(data["seen"], dict)


# ─── First-run behavior (state file does NOT exist) ──────────────────


class TestFirstRunSeedsWithoutDelivery:
    def test_files_present_on_first_run_seeded_not_delivered(
        self, watcher_dirs, monkeypatch, fake_job, fake_aiohttp
    ):
        """A fresh container's first watcher start finds historical
        output files. They MUST NOT be delivered (would replay
        ancient history), but they MUST be recorded in the state file
        (so subsequent ticks don't re-deliver them as "new")."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MYAH_USER_ID", "user-123")

        job_dir = watcher_dirs / "joba"
        job_dir.mkdir()
        f = job_dir / "ancient.md"
        f.write_text("from months ago")
        # Backdate the file so it would have failed the old
        # bootstrap-age cutoff too.
        import os, time
        old = time.time() - 365 * 86400
        os.utime(f, (old, old))

        import asyncio
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        asyncio.run(cron_watcher._tick("http://platform.test", "test-token"))

        assert len(fake_aiohttp) == 0, (
            "first-run encountered an ancient file; the seed-only path "
            "must NOT deliver historical files"
        )
        state_file = watcher_dirs.parent / ".watcher-seen.json"
        assert state_file.exists()
        seen = json.loads(state_file.read_text())["seen"]
        assert str(f) in seen, (
            "the seeded file must be recorded in state so the next tick "
            "skips it"
        )


# ─── Post-first-run behavior (state file exists) ─────────────────────


class TestPersistedStateRespected:
    def test_files_in_state_skip_on_tick(
        self, watcher_dirs, monkeypatch, fake_job, fake_aiohttp
    ):
        """If a (path, mtime) pair is already in the state file, that
        file MUST be skipped on every subsequent tick — even if it's
        the same mtime as last time."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MYAH_USER_ID", "user-123")

        job_dir = watcher_dirs / "joba"
        job_dir.mkdir()
        f = job_dir / "already-seen.md"
        f.write_text("Old delivery")
        mtime = f.stat().st_mtime

        # Pre-seed the state file as if the prior watcher process
        # already delivered this file.
        state_file = watcher_dirs.parent / ".watcher-seen.json"
        state_file.write_text(json.dumps({"version": 1, "seen": {str(f): mtime}}))

        import asyncio
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        asyncio.run(cron_watcher._tick("http://platform.test", "test-token"))

        assert len(fake_aiohttp) == 0, (
            "files already in state must not be re-delivered"
        )

    def test_files_not_in_state_get_delivered_regardless_of_age(
        self, watcher_dirs, monkeypatch, fake_job, fake_aiohttp
    ):
        """The KEY regression test for the 2026-05-22 incident.

        Container restart at t=0, cron output files written at t=+10s,
        watcher starts at t=+120s. With the OLD bootstrap-age logic,
        the +10s files (now 110s old) would be silently dropped. With
        persistent state, the previous container saved them as
        delivered; the new container's watcher loads that state and
        correctly skips them.

        But: if a file is NOT in the loaded state and the state file
        existed (post-first-run), the file MUST be delivered even if
        its mtime is older than 60s."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MYAH_USER_ID", "user-123")

        # Pre-existing state file: watcher previously ran in a prior
        # container and recorded SOME files but not this one.
        state_file = watcher_dirs.parent / ".watcher-seen.json"
        state_file.write_text(json.dumps({"version": 1, "seen": {}}))

        job_dir = watcher_dirs / "joba"
        job_dir.mkdir()
        f = job_dir / "ten-min-old.md"
        f.write_text("Cron fired right before container restart")
        import os, time
        ten_min_ago = time.time() - 600
        os.utime(f, (ten_min_ago, ten_min_ago))

        import asyncio
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        asyncio.run(cron_watcher._tick("http://platform.test", "test-token"))

        assert len(fake_aiohttp) == 1, (
            "a file that's NOT in the loaded state must be delivered "
            "regardless of its age; persistent state replaces age-based "
            "cutoff. Got %d deliveries." % len(fake_aiohttp)
        )

    def test_file_mtime_change_redelivers(
        self, watcher_dirs, monkeypatch, fake_job, fake_aiohttp
    ):
        """If a file's mtime changes (the cron re-ran and overwrote
        the output), the watcher must deliver again."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MYAH_USER_ID", "user-123")

        job_dir = watcher_dirs / "joba"
        job_dir.mkdir()
        f = job_dir / "shared-path.md"
        f.write_text("v1")
        old_mtime = f.stat().st_mtime

        # State file says we already saw v1.
        state_file = watcher_dirs.parent / ".watcher-seen.json"
        state_file.write_text(json.dumps({"version": 1, "seen": {str(f): old_mtime}}))

        # Now the cron re-ran and bumped mtime.
        import os, time
        new_mtime = old_mtime + 100
        os.utime(f, (new_mtime, new_mtime))

        import asyncio
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        asyncio.run(cron_watcher._tick("http://platform.test", "test-token"))

        assert len(fake_aiohttp) == 1, (
            "file's mtime changed → state mismatch → must redeliver"
        )

    def test_corrupted_state_file_falls_back_to_first_run_behavior(
        self, watcher_dirs, monkeypatch, fake_job, fake_aiohttp, caplog
    ):
        """If the state file is unreadable / malformed, log a warning
        and treat it as if it didn't exist (seed-all on first tick,
        no delivery)."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MYAH_USER_ID", "user-123")

        state_file = watcher_dirs.parent / ".watcher-seen.json"
        state_file.write_text("{not valid JSON")

        job_dir = watcher_dirs / "joba"
        job_dir.mkdir()
        f = job_dir / "out.md"
        f.write_text("Output")

        import asyncio
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        asyncio.run(cron_watcher._tick("http://platform.test", "test-token"))

        # Corrupt → treat as first-run → seed without delivery.
        assert len(fake_aiohttp) == 0, (
            "corrupted state must fall back to seed-only behavior"
        )
        # And the state file should be re-written cleanly.
        rewritten = json.loads(state_file.read_text())
        assert rewritten.get("version") == 1


# ─── Atomic writes ───────────────────────────────────────────────────


class TestAtomicWrite:
    def test_save_uses_temp_file_then_rename(
        self, watcher_dirs, monkeypatch, fake_job, fake_aiohttp, tmp_path
    ):
        """Saves go through a .tmp file + atomic rename. If a process
        crashes mid-write, the existing state file stays intact."""
        monkeypatch.setenv("MYAH_PLATFORM_BASE_URL", "http://platform.test")
        monkeypatch.setenv("MYAH_AGENT_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MYAH_USER_ID", "user-123")

        # Pre-existing valid state file.
        state_file = watcher_dirs.parent / ".watcher-seen.json"
        state_file.write_text(json.dumps({"version": 1, "seen": {}}))

        job_dir = watcher_dirs / "joba"
        job_dir.mkdir()
        f = job_dir / "new.md"
        f.write_text("Output")

        # Patch os.replace so we can verify the call.
        import os as _os
        original_replace = _os.replace
        calls = []

        def tracking_replace(src, dst):
            calls.append((str(src), str(dst)))
            return original_replace(src, dst)

        monkeypatch.setattr("os.replace", tracking_replace)

        import asyncio
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        asyncio.run(cron_watcher._tick("http://platform.test", "test-token"))

        assert calls, (
            "expected at least one os.replace call from atomic save; "
            "implementation must use tmp+rename pattern"
        )
        assert any(
            str(state_file) in dst for (_src, dst) in calls
        ), "os.replace destination must be the state file"


# ─── Helpers exposed for direct test ─────────────────────────────────


class TestHelperFunctions:
    def test_load_seen_state_returns_none_when_missing(self, watcher_dirs):
        """``None`` signals "treat as first-run" (no file or corrupted),
        distinguishable from ``{}`` which signals "valid empty state,
        anything new IS new"."""
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        result = cron_watcher._load_seen_state()
        assert result is None

    def test_load_seen_state_returns_seen_dict_when_present(self, watcher_dirs):
        state_file = watcher_dirs.parent / ".watcher-seen.json"
        state_file.write_text(json.dumps({
            "version": 1,
            "seen": {"/data/.hermes/cron/output/a/b.md": 1234567890.0},
        }))
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        result = cron_watcher._load_seen_state()
        assert result == {Path("/data/.hermes/cron/output/a/b.md"): 1234567890.0}

    def test_save_seen_state_writes_canonical_format(self, watcher_dirs):
        from myah_hermes_plugin.runtime_extensions import cron_watcher
        state = {Path("/foo/bar.md"): 999.5}
        cron_watcher._save_seen_state(state)
        state_file = watcher_dirs.parent / ".watcher-seen.json"
        data = json.loads(state_file.read_text())
        assert data == {"version": 1, "seen": {"/foo/bar.md": 999.5}}
