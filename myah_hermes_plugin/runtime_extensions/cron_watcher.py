"""F6 cron→chat delivery via background output-dir polling.

Why this exists
---------------

Vanilla NousResearch upstream's ``cron/scheduler.py:_deliver_result``
calls ``adapter.send(chat_id, content, metadata={"thread_id": ...})``.
The ``job`` dict (with ``job_id``, ``job_name``, ``origin``, etc.) is
in scope but is NOT forwarded to the adapter.

The fork (Tier 2B Task 2B.4) added a polymorphic
``runtime_adapter.build_delivery_metadata(job, status_hint, base_metadata)``
hook that scheduler.py calls before adapter.send to enrich metadata.
The MyahAdapter implements that hook to merge in ``job_id``,
``job_name``, ``status``, ``ran_at``, ``origin`` — and then
``MyahAdapter.send()`` detects cron deliveries via
``meta.get("job_id")`` and routes through the platform's
``/webhook/run-complete``.

Vanilla doesn't have that polymorphic hook, so MyahAdapter never sees
``job_id`` in metadata, the cron-detection branch never fires, and
output sits on disk unread.

Strategy
--------

This watcher observes vanilla's stable on-disk output convention
(``cron.jobs.save_job_output()`` writes
``OUTPUT_DIR/{job_id}/{timestamp}.md`` — verified upstream/main:
``cron/jobs.py:972``). For every new file it discovers, it reads the
job metadata via the vanilla ``cron.jobs.load_jobs()`` API and POSTs
to the platform's existing ``/api/v1/processes/webhook/run-complete``
endpoint.

This is strictly a plugin-side observer of vanilla primitives. Zero
core mutation. No monkey-patching. No upstream PR required.

Trade-off accepted: ``tool_calls_log`` is ``None`` because the on-disk
``.md`` file doesn't carry it. Cron jobs that emit AG-UI ``render_*``
artifacts deliver as raw Markdown text. The user has confirmed this is
acceptable since render_* in cron output was never a fully-built
feature.

For full ``tool_calls_log`` fidelity in the future, the optional
upstream U-CRON PR (~10 LOC adding ``build_delivery_metadata``
polymorphism) would close that gap. Not blocking OSS launch.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from cron.jobs import OUTPUT_DIR, load_jobs

logger = logging.getLogger(__name__)

# Module-level state for idempotency: seen file mtimes, so the same
# output isn't delivered twice on consecutive ticks.
_seen_mtimes: dict[Path, float] = {}

# Don't replay historical files on plugin startup. Files older than
# this many seconds are seeded into _seen_mtimes without triggering
# delivery on the first tick.
_BOOTSTRAP_AGE_SECS = 60

_TICK_INTERVAL_SECS = 2.0
_HTTP_TIMEOUT_SECS = 15

_running_task: Optional[asyncio.Task] = None


async def _watch_loop() -> None:
    base_url = os.environ.get("MYAH_PLATFORM_BASE_URL", "").rstrip("/")
    bearer = os.environ.get("MYAH_AGENT_BEARER_TOKEN", "")
    if not (base_url and bearer):
        logger.info(
            "cron watcher: MYAH_PLATFORM_BASE_URL or MYAH_AGENT_BEARER_TOKEN unset; "
            "watcher will be a no-op"
        )
        return

    while True:
        try:
            await _tick(base_url, bearer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cron watcher tick raised")
        await asyncio.sleep(_TICK_INTERVAL_SECS)


async def _tick(base_url: str, bearer: str) -> None:
    if not OUTPUT_DIR.exists():
        return
    try:
        all_jobs = load_jobs() or []
    except Exception:
        logger.exception("cron watcher: failed to read jobs.json")
        all_jobs = []
    jobs_by_id = {j.get("id"): j for j in all_jobs if isinstance(j, dict)}
    cutoff = time.time() - _BOOTSTRAP_AGE_SECS

    for job_dir in OUTPUT_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        for output_file in job_dir.glob("*.md"):
            try:
                mtime = output_file.stat().st_mtime
            except OSError:
                continue
            if _seen_mtimes.get(output_file) == mtime:
                continue
            # Bootstrap behavior: don't replay files that predate
            # plugin startup.
            if output_file not in _seen_mtimes and mtime < cutoff:
                _seen_mtimes[output_file] = mtime
                continue
            _seen_mtimes[output_file] = mtime
            await _deliver(
                base_url, bearer, jobs_by_id.get(job_id, {}), job_id, output_file
            )


async def _deliver(
    base_url: str, bearer: str, job: dict, job_id: str, output_file: Path
) -> None:
    origin = job.get("origin") or {}
    if not isinstance(origin, dict) or origin.get("platform") != "myah":
        # Non-myah cron jobs are delivered by their own adapters. Skip.
        return

    try:
        content = output_file.read_text(encoding="utf-8")
    except Exception:
        logger.exception("cron watcher: failed to read %s", output_file)
        return

    payload = {
        "user_id": os.environ.get("MYAH_USER_ID", ""),
        "job_id": job_id,
        "job_name": job.get("name") or job_id,
        "chat_id": origin.get("chat_id", ""),
        "response": content,
        "status": job.get("last_status") or "ok",
        "ran_at": datetime.fromtimestamp(
            output_file.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "tool_calls_log": None,  # not available on disk; accepted degradation
    }
    if not payload["chat_id"]:
        logger.warning(
            "cron watcher: job %s has myah origin but no chat_id; skipping", job_id
        )
        return

    url = f"{base_url}/api/v1/processes/webhook/run-complete"
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {bearer}"},
            ) as resp:
                if 200 <= resp.status < 300:
                    logger.info(
                        "cron watcher: delivered job=%s chat=%s status=%s",
                        job_id, payload["chat_id"], resp.status,
                    )
                else:
                    logger.warning(
                        "cron watcher: webhook returned status=%s for job=%s",
                        resp.status, job_id,
                    )
    except Exception:
        logger.warning("cron watcher: webhook POST failed for job=%s", job_id)


_started = False


def _lazy_start_via_hook(*args, **kwargs):
    """``pre_gateway_dispatch`` hook that starts the watcher on first
    dispatch. The gateway's event loop is guaranteed to be running by
    the time any dispatch fires, so ``asyncio.get_running_loop()`` is
    safe here. Idempotent — only the first call schedules the task.

    Returns None so this hook never short-circuits gateway dispatch.
    """
    global _started, _running_task
    if _started:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Should never happen — pre_gateway_dispatch fires inside the
        # gateway's running loop. Log and stay un-started rather than
        # crash the dispatch.
        logger.warning(
            "cron watcher: pre_gateway_dispatch fired without a running "
            "loop; watcher will not start"
        )
        return None
    _running_task = loop.create_task(_watch_loop())
    _started = True
    logger.info("Myah cron output watcher started (via pre_gateway_dispatch)")
    return None


def register_cron_watcher(ctx) -> None:
    """Register the pre_gateway_dispatch lazy-start hook.

    Called from plugin ``register(ctx)``. Avoids the
    ``asyncio.get_event_loop()`` deprecation warning under Python 3.12+
    by deferring task creation until the gateway's event loop is
    confirmed running.
    """
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_gateway_dispatch", _lazy_start_via_hook)


def stop() -> None:
    """Cancel the running watcher (test cleanup helper)."""
    global _running_task, _started
    if _running_task and not _running_task.done():
        _running_task.cancel()
    _running_task = None
    _started = False
