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
import json
import logging
import os
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import aiohttp

from cron.jobs import OUTPUT_DIR, load_jobs

logger = logging.getLogger(__name__)

# Module-level state for idempotency: seen file mtimes, so the same
# output isn't delivered twice on consecutive ticks. Synced to disk
# at ``_STATE_FILE`` after every successful delivery (see
# ``_save_seen_state``), so a container restart can resume from the
# exact same delivered set instead of re-applying an age-based cutoff.
_seen_mtimes: dict[Path, float] = {}

# Persistent seen-state file. Lives in the same dir as ``jobs.json``
# so it survives container restarts (the whole /data/.hermes/ tree is
# volume-mounted). Format documented in ``_save_seen_state``.
#
# Why persistent (2026-05-22): an earlier in-memory-only implementation
# applied a 60-second bootstrap-age cutoff on first tick after start.
# That cutoff caused real data loss when container respawns happened
# more than 60s after cron output was written — the plugin's lazy
# ``pre_gateway_dispatch`` start adds inherent delay, and Hetzner
# deploys took the watcher offline at exactly the wrong moments.
# Persistent state replaces the cutoff with a "what we've actually
# delivered" record, guaranteeing zero loss across restarts.
#
# Resolved dynamically from ``OUTPUT_DIR`` at every reference so tests
# can monkeypatch ``OUTPUT_DIR`` to a tmp dir and the state file
# follows. The PEP 562 ``__getattr__`` re-export below lets external
# callers (and tests) reference ``cron_watcher._STATE_FILE`` as if it
# were a module attribute.
def _state_file_path() -> Path:
    return OUTPUT_DIR.parent / ".watcher-seen.json"


def __getattr__(name: str):
    if name == "_STATE_FILE":
        return _state_file_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Internal flag — flipped to True after the first ``_tick`` call
# loads (or seeds) the on-disk state, so subsequent ticks don't
# re-load.
_state_loaded: bool = False

_TICK_INTERVAL_SECS = 2.0
_HTTP_TIMEOUT_SECS = 15
_PROBE_TIMEOUT_SECS = 5
_BACKOFF_SECS = 30

_running_task: Optional[asyncio.Task] = None

# Consecutive POST-failure counter (Task 1.6 / spec §6.2). Resets on
# any successful POST. At threshold 3 we escalate to
# ``sentry_sdk.capture_message`` for critical alerting.
_consecutive_post_failures = 0


def _get_bearer() -> str:
    """Read the platform bearer token, canonical-first.

    Per spec-review HIGH-5: in hosted containers, ``containers.py``
    injects ``MYAH_PLATFORM_BEARER`` (NOT ``MYAH_AGENT_BEARER_TOKEN``).
    On OSS hosts, ``setup-myah-oss.sh`` writes ``MYAH_AGENT_BEARER_TOKEN``.
    Read both, prefer canonical for forward-compatibility — the legacy
    name is the lower-priority fallback.

    The pre-fix watcher read only ``MYAH_AGENT_BEARER_TOKEN`` and so
    silently no-op'd in hosted prod because the canonical name was the
    only one set there.
    """
    return (
        os.environ.get("MYAH_PLATFORM_BEARER", "")
        or os.environ.get("MYAH_AGENT_BEARER_TOKEN", "")
    )


def _probe_platform() -> bool:
    """GET ``MYAH_PLATFORM_BASE_URL/health`` with a 5s timeout.

    Returns True iff 200 ≤ status < 300.

    Per spec-review CRIT-3: ``/health`` is a **root-level** FastAPI
    route, NOT ``/api/v1/health``. ``@app.get`` is GET-only — issuing
    HEAD would return 405 and falsely report the platform unreachable.
    """
    base = os.environ.get("MYAH_PLATFORM_BASE_URL", "").rstrip("/")
    if not base:
        return False
    url = urljoin(base + "/", "health")
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_SECS) as resp:
            status = getattr(resp, "status", None) or getattr(resp, "code", 0)
            return 200 <= status < 300
    except Exception:
        return False


def _sentry_breadcrumb_safe(**kwargs) -> None:
    """Add a Sentry breadcrumb without raising if ``sentry_sdk`` is
    unavailable or its API changes. The watcher must never crash on
    observability concerns."""
    try:
        import sentry_sdk
        sentry_sdk.add_breadcrumb(**kwargs)
    except Exception:
        pass


def _verify_platform_reachable_or_log() -> bool:
    """Verify the platform is reachable; log a loud ERROR otherwise.

    Returns True iff base URL is set AND the probe returns 2xx.
    Failure path logs ERROR + drops a Sentry breadcrumb. Caller is
    expected to back off and retry rather than silently no-op.
    """
    base = os.environ.get("MYAH_PLATFORM_BASE_URL", "").rstrip("/")
    if not base:
        logger.error(
            "cron watcher: MYAH_PLATFORM_BASE_URL not set; cron output will "
            "not reach the chat. Check ~/.hermes/.env or container env injection."
        )
        _sentry_breadcrumb_safe(
            category="myah.cron_watcher",
            level="error",
            message="MYAH_PLATFORM_BASE_URL unset",
        )
        return False
    if not _probe_platform():
        logger.error(
            f"cron watcher: MYAH_PLATFORM_BASE_URL={base} unreachable "
            f"(probe GET /health timed out or returned non-2xx). Cron output "
            f"will not reach the chat until this is resolved."
        )
        _sentry_breadcrumb_safe(
            category="myah.cron_watcher",
            level="error",
            message=f"platform unreachable at {base}",
        )
        return False
    return True


def _on_post_success() -> None:
    """Reset the consecutive-failure counter after a successful POST."""
    global _consecutive_post_failures
    _consecutive_post_failures = 0


def _on_post_failure(job_id: str, error: str) -> None:
    """Record a POST failure. Logs at ERROR + drops a Sentry breadcrumb.
    Escalates to ``sentry_sdk.capture_message`` once 3 consecutive
    failures accumulate so on-call gets a real alert (a breadcrumb-only
    surface gets lost in the issue feed)."""
    global _consecutive_post_failures
    _consecutive_post_failures += 1
    logger.error(
        f"cron watcher: webhook POST failed for job={job_id}: {error} "
        f"(consecutive failures: {_consecutive_post_failures})"
    )
    _sentry_breadcrumb_safe(
        category="myah.cron_watcher",
        level="error",
        message=f"webhook POST failed for {job_id}",
        data={
            "error": error,
            "consecutive_failures": _consecutive_post_failures,
        },
    )
    if _consecutive_post_failures >= 3:
        try:
            import sentry_sdk
            sentry_sdk.capture_message(
                f"cron watcher: 3+ consecutive POST failures (last: {error})",
                level="error",
            )
        except Exception:
            pass


async def _watch_loop() -> None:
    """Main watcher loop.

    Before the spec §6.2 fix this method silently ``return``ed when the
    env vars were missing — meaning hosted prod (which sets
    ``MYAH_PLATFORM_BEARER``, not the legacy ``MYAH_AGENT_BEARER_TOKEN``
    name the old code read) ran with a permanently-idle watcher and
    every cron output was silently dropped.

    New behavior:
    * Read bearer canonical-first via :func:`_get_bearer`.
    * If env vars are missing → ERROR + breadcrumb, back off 30 s, retry.
    * If env vars present but the platform doesn't answer
      ``GET /health`` → ERROR + breadcrumb, back off 30 s, retry.
    * Once reachable, fall into the normal tick loop.
    """
    base_url = os.environ.get("MYAH_PLATFORM_BASE_URL", "").rstrip("/")
    bearer = _get_bearer()
    while not (base_url and bearer):
        logger.error(
            "cron watcher: MYAH_PLATFORM_BASE_URL or platform bearer unset; "
            "cron output will NOT reach the chat. Check container env injection "
            "(MYAH_PLATFORM_BEARER) or OSS host env "
            "(~/.hermes/.env: MYAH_AGENT_BEARER_TOKEN). "
            f"Watcher will retry every {_BACKOFF_SECS}s until both are set."
        )
        _sentry_breadcrumb_safe(
            category="myah.cron_watcher",
            level="error",
            message="env vars unset; watcher idle",
            data={
                "base_url_set": bool(base_url),
                "bearer_set": bool(bearer),
            },
        )
        await asyncio.sleep(_BACKOFF_SECS)
        base_url = os.environ.get("MYAH_PLATFORM_BASE_URL", "").rstrip("/")
        bearer = _get_bearer()
    logger.info("cron watcher: env configured; starting reachability probe.")

    while not _verify_platform_reachable_or_log():
        await asyncio.sleep(_BACKOFF_SECS)
    logger.info("cron watcher: platform reachable; entering tick loop.")

    while True:
        try:
            await _tick(base_url, bearer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cron watcher tick raised")
        await asyncio.sleep(_TICK_INTERVAL_SECS)


def _load_seen_state() -> Optional[dict[Path, float]]:
    """Read the persistent seen-state file.

    Returns:
        * ``{}`` — file exists and is a valid empty seen-set (post-
          first-run with nothing delivered yet).
        * ``{Path: mtime, ...}`` — file exists with prior deliveries.
        * ``None`` — file is absent OR unreadable OR corrupted. The
          caller treats this as the "first-run / recovery" case and
          seeds without delivery.

    The ``None`` vs ``{}`` distinction matters: an empty-but-valid
    state means "we know nothing has been delivered yet" → any new
    file IS new and should be delivered. A missing/corrupted state
    means "we lost our memory" → don't replay history.
    """
    state_file = _state_file_path()
    try:
        raw = state_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("cron watcher: failed to read state file %s: %s", state_file, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "cron watcher: state file %s is corrupted (%s); treating as fresh start",
            state_file, exc,
        )
        return None
    seen = data.get("seen") if isinstance(data, dict) else None
    if not isinstance(seen, dict):
        logger.warning("cron watcher: state file %s missing 'seen' map; treating as fresh start", state_file)
        return None
    # Coerce per-entry rather than via a single dict comprehension so
    # one malformed value (non-string key, non-numeric mtime, null,
    # list, dict) doesn't crash the entire load and leave the watcher
    # in a permanent failure loop. Drop bad entries with a WARNING;
    # if the whole file is unusable (zero good entries) treat it as
    # missing and let first-run recovery seed fresh.
    out: dict[Path, float] = {}
    for raw_path, raw_mtime in seen.items():
        try:
            out[Path(str(raw_path))] = float(raw_mtime)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "cron watcher: dropping malformed state entry %r=%r (%s)",
                raw_path, raw_mtime, exc,
            )
    if not out and seen:
        # File had entries but none survived coercion → equivalent to
        # corrupted-state. Return None so the caller re-seeds from
        # disk and overwrites this file cleanly.
        logger.warning(
            "cron watcher: every entry in %s was malformed; "
            "treating as corrupted and re-seeding",
            state_file,
        )
        return None
    return out


def _save_seen_state(state: dict[Path, float]) -> None:
    """Atomic write of the seen-state to disk.

    Writes ``{"version": 1, "seen": {<str path>: <mtime>}}`` to a
    temporary file in the same directory, then ``os.replace``s it
    onto the canonical path. ``os.replace`` is atomic on POSIX, so
    a process crash mid-write leaves the prior state intact (no
    half-written JSON ever appears at the canonical path).
    """
    state_file = _state_file_path()
    parent = state_file.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cron watcher: cannot create state dir %s: %s", parent, exc)
        return
    payload = {
        "version": 1,
        "seen": {str(path): mtime for path, mtime in state.items()},
    }
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(parent),
            prefix=".watcher-seen.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        os.replace(tmp_path, str(state_file))
        tmp_path = None  # ownership transferred to state_file
    except OSError as exc:
        logger.warning("cron watcher: failed to save state file: %s", exc)
    finally:
        # Clean up the tmp file if replace() never ran (e.g. disk full
        # mid-write, EACCES on rename). Otherwise the .tmp files
        # accumulate in /data/.hermes/cron/ and compound on disk-full
        # incidents.
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _enumerate_output_files() -> list[tuple[str, Path, float]]:
    """List every current ``(job_id, output_file, mtime)`` tuple.

    Centralized so ``_tick`` and the first-run seed share the same
    discovery logic.
    """
    found: list[tuple[str, Path, float]] = []
    for job_dir in OUTPUT_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        for output_file in job_dir.glob("*.md"):
            try:
                mtime = output_file.stat().st_mtime
            except OSError:
                continue
            found.append((job_dir.name, output_file, mtime))
    return found


def _ensure_state_loaded() -> None:
    """Called once on the first ``_tick``. Either loads existing
    state from disk OR seeds every currently-present output file
    into ``_seen_mtimes`` without delivery (first-run protection
    against historical replay).

    Idempotent — subsequent calls are no-ops because ``_state_loaded``
    flips to True after the first successful call.
    """
    global _state_loaded, _seen_mtimes
    if _state_loaded:
        return
    loaded = _load_seen_state()
    if loaded is not None:
        # Trust the on-disk record — even an empty dict means "we
        # know nothing has been delivered yet, so anything new IS new".
        # Clear first so that test-harness leaks (or any other reason
        # _seen_mtimes might hold stale entries pre-load) don't merge
        # into the loaded state — replace, don't union.
        _seen_mtimes.clear()
        _seen_mtimes.update(loaded)
        _state_loaded = True
        return
    # First run (or recovered-from-corrupt). Seed everything we see
    # NOW as "already delivered" — we don't know what the prior plugin
    # process (if any) actually delivered, so the safe behavior is
    # "don't replay history". Subsequent ticks deliver only files
    # that appear AFTER this seeding pass.
    if OUTPUT_DIR.exists():
        for _job_id, output_file, mtime in _enumerate_output_files():
            _seen_mtimes[output_file] = mtime
    _save_seen_state(_seen_mtimes)
    _state_loaded = True


async def _tick(base_url: str, bearer: str) -> None:
    if not OUTPUT_DIR.exists():
        return

    # First call seeds or loads state. After this, ``_seen_mtimes``
    # reflects the persistent source of truth for delivered files.
    _ensure_state_loaded()

    try:
        all_jobs = load_jobs() or []
    except Exception:
        logger.exception("cron watcher: failed to read jobs.json")
        all_jobs = []
    jobs_by_id = {j.get("id"): j for j in all_jobs if isinstance(j, dict)}

    state_dirty = False
    for job_id, output_file, mtime in _enumerate_output_files():
        if _seen_mtimes.get(output_file) == mtime:
            continue
        # Mark-after-success: only record the file as delivered if
        # ``_deliver`` confirms the webhook accepted it. If we recorded
        # the file BEFORE the call, a transient platform failure (HTTP
        # 502 during a deploy, connection refused mid-restart) would
        # be persisted to disk as "delivered" — and the next tick
        # (and every subsequent restart-then-load) would skip the
        # file forever. That's the persistent-state version of the
        # same data-loss class this PR is fixing.
        delivered = await _deliver(
            base_url, bearer, jobs_by_id.get(job_id, {}), job_id, output_file
        )
        if delivered:
            _seen_mtimes[output_file] = mtime
            state_dirty = True
    if state_dirty:
        _save_seen_state(_seen_mtimes)


async def _deliver(
    base_url: str, bearer: str, job: dict, job_id: str, output_file: Path
) -> bool:
    """Deliver one cron-output file to the platform webhook.

    Returns:
        * ``True`` — the webhook returned 2xx and accepted the output.
          The caller MUST persist this file as seen.
        * ``False`` — anything else (non-myah origin / skip, file
          unreadable, missing chat_id, HTTP non-2xx, network exception).
          The caller MUST NOT persist; the file will be retried on a
          subsequent tick (or after a restart that reloads state).

    The split-return is the safety contract for the persistent
    seen-state: ``_tick`` only writes ``.watcher-seen.json`` for files
    this function confirms delivered, so transient platform failures
    don't get baked in as permanent "delivered" records.
    """
    origin = job.get("origin") or {}
    if not isinstance(origin, dict) or origin.get("platform") != "myah":
        # Non-myah cron jobs are delivered by their own adapters. Skip
        # cleanly (not a failure to re-attempt).
        return False

    try:
        content = output_file.read_text(encoding="utf-8")
    except Exception:
        logger.exception("cron watcher: failed to read %s", output_file)
        return False

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
        return False

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
                    _on_post_success()
                    return True
                _on_post_failure(job_id, f"HTTP {resp.status}")
                return False
    except Exception as exc:
        _on_post_failure(job_id, str(exc) or type(exc).__name__)
        return False


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
