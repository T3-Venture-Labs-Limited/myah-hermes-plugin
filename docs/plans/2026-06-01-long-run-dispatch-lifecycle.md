# Long-Run Dispatch Lifecycle Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Stop the Myah plugin from marking long-running Hermes turns as complete at the old hard-coded ~10-minute lifecycle boundary, while preserving safety cleanup for real failures, disconnects, and truly orphaned streams.

**Architecture:** Keep the fix inside `myah-hermes-plugin` because logs prove Hermes keeps running after Myah's stream closes. Refactor dispatch completion into small helper methods so tests can drive the lifecycle without real-time sleeps. The plugin should wait until the gateway session leaves `_active_sessions` before emitting `run.completed`; diagnostic thresholds can only log, never close the stream. The existing orphan sweeper must also exempt active sessions, because `_STREAM_TTL = 600` is another 10-minute stream closer.

**Tech Stack:** Python 3.11, aiohttp, pytest, pytest-asyncio, Hermes gateway `BasePlatformAdapter` semantics.

---

## Background evidence

Observed failure shape:

- Myah UI shows the agent turn as complete around 10 minutes / ~50-ish turns.
- Hermes logs later show normal completion (`api_calls=55/150`, final response text produced after ~689s).
- Plugin log then falls back because stream mappings were already cleaned up.
- Current plugin code uses `for _ in range(6000): await asyncio.sleep(0.1)`, then the `finally` block emits `run.completed` and sends the SSE sentinel even if the session is still in `_active_sessions`.
- Independent plan review found a second hard 10-minute lifecycle path: `_sweep_orphaned_streams()` uses `_STREAM_TTL = 600` and can pop stream/mapping state for an active long run.

User requirement: complex Myah agent turns must be able to run for a few hours.

## Plan review triage incorporated

Accepted blocking/high-priority feedback from independent review:

1. **Accept:** Exempt active sessions from `_sweep_orphaned_streams`; add RED test for active stream older than `_STREAM_TTL` not being swept.
2. **Accept:** Add terminal-event exclusivity tests and production outcome flag so `run.failed` is not followed by `run.completed`.
3. **Accept:** Define cancellation/shutdown semantics: cancellation must not emit `run.completed`.
4. **Accept:** Avoid test-only exceptions in the main long-run RED test; use fake sleep/counters/events and cancellation only after assertions.
5. **Accept:** Cover the race where `_active_sessions` appears shortly after `handle_message()` returns.
6. **Accept:** Document/test disconnect/fallback semantics at unit level where practical; do not redesign disconnect behavior in this PR.
7. **Accept:** Do not emit new frontend `status` event in this PR; log only to avoid frontend contract drift.
8. **Accept:** Use `time.monotonic()` for diagnostic interval tracking.
9. **Accept:** No completion timeout; use rate-limited logs and cancellation-safe cleanup for zombie-session observability.
10. **Accept:** Config parsing tests include strings, whitespace, invalids, missing `extra`, and precedence.
11. **Accept:** Preserve approval deferral behavior and add an interaction test when completion happens with pending approval.
12. **Accept:** Avoid replacing internal `_active_sessions` where possible in new tests.
13. **Accept:** Assert terminal event order and exactly-one semantics.
14. **Accept:** For final delivery, assert live SSE receives the event; durable fallback remains covered elsewhere.
15. **Accept:** Keep adapter changes localized.

Rejected/deferred feedback: none rejected. Metrics for active wait duration/count are deferred as a follow-up because the current bug fix only needs logging and tests.

## Design decisions

1. **No premature terminal event.** `run.completed` must only be emitted when the gateway session is no longer active and dispatch was not failed/cancelled.
2. **Failures are terminal failures.** `run.failed` must never be followed by `run.completed`.
3. **Cancellation is non-completion.** If `_dispatch_message` is cancelled during shutdown or test cleanup, it should not emit `run.completed`.
4. **Safety is non-terminal by default.** A long-run threshold can log “still running past threshold”, but must not close SSE or cleanup stream mappings.
5. **Config-aware diagnostic interval.** Introduce a helper that derives a long-run logging interval from plugin/config/env values. This interval is for diagnostics only, not completion.
6. **Active streams are not orphaned.** `_sweep_orphaned_streams()` must skip streams whose reverse-mapped session key is currently in `_active_sessions`.
7. **Test without real sleeps.** Add helpers that monkeypatch adapter-module `asyncio.sleep` and `time.monotonic` so tests can cross legacy boundaries immediately.
8. **Keep final fallback as backup only.** Do not remove durable final fallback; this patch makes it unnecessary for normal connected >10m turns.

## Acceptance criteria

- A dispatch whose session remains active past the old 600s/6000-poll boundary does **not** emit `run.completed` and does **not** enqueue `None` until the session clears.
- A dispatch whose session clears after crossing the old boundary emits exactly one `run.completed` event after the clear.
- `run.failed` paths emit no later `run.completed`.
- Cancellation while active emits no `run.completed`.
- The stream/chat/session mappings remain live while the run is still active, so `adapter.send(...)` can continue to deliver visible events to the SSE queue.
- `_sweep_orphaned_streams()` does not close active long-running streams older than `_STREAM_TTL`.
- Config parsing supports multi-hour diagnostic intervals and makes a 600s hard-coded cap impossible as a completion trigger.
- Focused plugin tests pass.

---

## Task 1: Add RED tests for terminal-event exclusivity and legacy long-run dispatch behavior

**Objective:** Reproduce the dispatch bug in tests before changing production code.

**Files:**
- Modify: `tests/test_dispatch_message_defers_cleanup.py`
- Test target: `myah_hermes_plugin/myah_platform/adapter.py`

**Step 1: Add queue/test helpers**

Add helpers to the existing test file:

- `drain_queue_nowait(q)` returns queued items without blocking.
- `terminal_events(items)` filters `run.completed` and `run.failed`.
- `_seed_dual_mapping(...)` already exists; extend it to also seed `_streams_created[stream_id] = time.time()` where useful.
- A fake sleep factory that increments a poll counter and awaits original `asyncio.sleep(0)` to yield. It must not raise inside the dispatch path.

**Step 2: RED test — no-handler emits failed only**

Add:

```python
@pytest.mark.asyncio
async def test_dispatch_message_no_handler_emits_failed_without_completed():
    ...
```

Expected current failure: queue contains `run.failed`, then `run.completed`, then `None`.

**Step 3: RED test — active past old 6000 polls stays non-terminal**

Add:

```python
@pytest.mark.asyncio
async def test_dispatch_message_does_not_complete_when_session_still_active_after_legacy_wait(monkeypatch):
    ...
```

Pattern:

- monkeypatch `adapter.handle_message` to add the computed session key to `adapter._active_sessions`.
- monkeypatch adapter-module `asyncio.sleep` to increment a counter.
- start `_dispatch_message` as a task.
- wait until the counter is >6000 using an `asyncio.Event` set by fake sleep.
- inspect the queue while the dispatch task is still pending.
- assert no terminal events and no `None` sentinel.
- cleanup by removing the session key, allowing one more fake sleep tick, then awaiting task completion.

Expected current failure: by poll 6000, `_dispatch_message` has already emitted `run.completed` and `None`.

**Step 4: RED test — clears after legacy boundary completes after clear**

Add:

```python
@pytest.mark.asyncio
async def test_dispatch_message_completes_after_session_clears_beyond_legacy_wait(monkeypatch):
    ...
```

Pattern:

- fake sleep removes the session only at e.g. poll 6005.
- assert `run.completed` timestamp/order occurs after the session removal marker, not at poll 6000.

Expected current failure: completion occurs before the fake clear.

**Step 5: RED test — cancellation does not complete**

Add:

```python
@pytest.mark.asyncio
async def test_dispatch_message_cancelled_while_active_does_not_emit_completed(monkeypatch):
    ...
```

Expected current failure: `finally` emits `run.completed` during cancellation.

**Step 6: Run RED tests**

Run:

```bash
python -m pytest tests/test_dispatch_message_defers_cleanup.py -q
```

Expected: new tests fail against current production code for the described reasons. Existing approval-cleanup tests should still pass.

---

## Task 2: Implement minimal dispatch lifecycle fix

**Objective:** Make Task 1 tests pass without broad refactors.

**Files:**
- Modify: `myah_hermes_plugin/myah_platform/adapter.py`
- Test: `tests/test_dispatch_message_defers_cleanup.py`

**Step 1: Add helper methods near `_dispatch_message`**

Add private methods on `MyahAdapter`:

```python
def _coerce_positive_float(self, value: Any) -> Optional[float]:
    ...

def _get_long_run_status_interval_seconds(self) -> float:
    """Return diagnostic interval for long-running dispatches; never a completion cap."""

async def _wait_for_session_completion(self, *, session_key: str, stream_id: str) -> None:
    """Wait until session_key leaves _active_sessions.

    This method must not return merely because a duration threshold elapsed. If a threshold
    elapses, log a non-terminal diagnostic and continue waiting.
    """
```

Config precedence for the status/log interval:

1. `self.config.extra['long_run_status_interval']` if present
2. env `MYAH_LONG_RUN_STATUS_INTERVAL`
3. `self.config.extra['gateway_timeout']`
4. `self.config.extra['agent_gateway_timeout']`
5. env `HERMES_AGENT_TIMEOUT`
6. default `1800.0`

Clamp invalid/non-positive values to `1800.0`. Values may be ints/floats/strings with whitespace.

Use `time.monotonic()` in `_wait_for_session_completion`. On each interval elapsed while the session is still active, log an info line and reset the next log deadline. Do not `_push_event_sync` any new frontend status event in this PR.

**Step 2: Replace the 6000 loop**

Replace:

```python
for _ in range(6000):
    if _sk not in self._active_sessions:
        break
    await asyncio.sleep(0.1)
```

with:

```python
await self._wait_for_session_completion(session_key=_sk, stream_id=stream_id)
```

**Step 3: Add terminal outcome guard**

In `_dispatch_message`, track outcome flags, e.g.:

```python
_terminal_event_sent = False
_cancelled = False
```

Set `_terminal_event_sent = True` whenever emitting `run.failed`. In `except asyncio.CancelledError`, set cancelled and re-raise or return after preventing completion. In `finally`, emit `run.completed` only when:

- not cancelled,
- no terminal event already sent,
- queue still exists,
- the relevant session is no longer active.

Do not change existing suppression-warning behavior except to ensure it only runs on successful completion.

**Step 4: Run GREEN tests**

Run:

```bash
python -m pytest tests/test_dispatch_message_defers_cleanup.py -q
```

Expected: all tests in the file pass.

---

## Task 3: Add RED/GREEN tests for active stream sweeper exemption

**Objective:** Fix the second 10-minute closer: `_STREAM_TTL` orphan sweeping.

**Files:**
- Modify: `tests/test_dispatch_message_defers_cleanup.py`
- Modify: `myah_hermes_plugin/myah_platform/adapter.py`

**Step 1: RED test for active stream not swept**

Add a test that directly exercises one sweep iteration without waiting 60 seconds. Preferred implementation: extract one helper in production first only after RED test names it, e.g. `_sweep_orphaned_streams_once(now: Optional[float] = None)`.

Test shape:

```python
def test_orphan_sweeper_skips_stream_with_active_session():
    adapter = _make_adapter()
    _seed_dual_mapping(...)
    adapter._streams_created[stream_id] = 0
    adapter._active_sessions[session_key] = object()
    adapter._sweep_orphaned_streams_once(now=_STREAM_TTL + 1)
    assert stream_id in adapter._streams
    assert chat_id in adapter._chat_id_streams
    assert session_key in adapter._session_streams
```

Expected current failure: helper does not exist, or current sweep logic would remove it.

**Step 2: GREEN implementation**

Extract `_sweep_orphaned_streams_once(now: Optional[float] = None)` and have `_sweep_orphaned_streams()` call it after sleeping.

When building stale list, skip any `sid` whose `session_key = self._stream_sessions.get(sid)` is in `self._active_sessions`.

Keep sweeping truly orphaned streams unchanged.

**Step 3: Add control test for inactive orphan still swept**

Add a test that a stale stream with no active session is still removed and receives `None`.

Run:

```bash
python -m pytest tests/test_dispatch_message_defers_cleanup.py -q
```

---

## Task 4: Add config/status interval and live-send regression coverage

**Objective:** Prove hours-long semantics and live final delivery path.

**Files:**
- Modify: `tests/test_dispatch_message_defers_cleanup.py`
- Modify if needed: `myah_hermes_plugin/myah_platform/adapter.py`

**Step 1: Config parsing tests**

Add tests for `_get_long_run_status_interval_seconds`:

- `extra={"long_run_status_interval": 7200}` returns `7200.0`
- `extra={"long_run_status_interval": " 7200 "}` returns `7200.0`
- env `MYAH_LONG_RUN_STATUS_INTERVAL=10800` returns `10800.0`
- explicit config wins over env
- env `HERMES_AGENT_TIMEOUT=1800` returns `1800.0` when no explicit plugin value exists
- missing `extra`, invalid string, zero, negative values fall back to `1800.0`

**Step 2: Live-send while active test**

Add a test that starts a long-running dispatch task with an active session, crosses old 6000-poll boundary, then calls:

```python
await adapter.send(chat_id, "still streaming")
```

Assert before session clear:

- `chat_id in adapter._chat_id_streams`
- `session_key in adapter._session_streams`
- `stream_id in adapter._stream_sessions`
- stream queue receives a visible `message.delta`/content event from `send(...)`
- no terminal event exists yet

Then clear the session, let dispatch finish, and assert exactly one `run.completed` follows.

**Step 3: Approval deferral interaction test**

Add or adjust a test where the session completes while an approval remains pending. Verify existing deferral still preserves mappings after `run.completed` path.

Run:

```bash
python -m pytest tests/test_dispatch_message_defers_cleanup.py tests/test_adapter_send_cron_path.py -q
```

---

## Task 5: Verification, independent review, and PR

**Objective:** Verify the change, commit it, and open a plugin PR.

**Files:**
- All modified files

**Step 1: Static checks**

```bash
git diff --check
python -m compileall myah_hermes_plugin tests
```

**Step 2: Focused tests**

```bash
python -m pytest tests/test_dispatch_message_defers_cleanup.py -q
python -m pytest tests/test_adapter_send_cron_path.py tests/test_myah_adapter_offline_delivery.py -q
python -m pytest tests/test_myah_adapter.py tests/test_myah_native_streaming.py tests/test_dispatch_message_defers_cleanup.py -q
```

**Step 3: Independent work review**

Before PR, get an independent reviewer subagent/Claude Code review focused on:

- no premature `run.completed`
- no hidden 600s stream cleanup for active sessions
- no completion on failure/cancellation
- config semantics for hours-long runs
- test quality and TDD evidence

Triage every finding as accepted/rejected with reason, patch accepted findings, rerun relevant tests.

**Step 4: Commit and open PR**

```bash
git status --short
git add myah_hermes_plugin/myah_platform/adapter.py tests/test_dispatch_message_defers_cleanup.py docs/plans/2026-06-01-long-run-dispatch-lifecycle.md
git commit -m "fix: keep long-running Myah dispatch streams active"
git push -u origin fix/long-run-dispatch-lifecycle
gh pr create --repo T3-Venture-Labs-Limited/myah-hermes-plugin --base master --head fix/long-run-dispatch-lifecycle --title "fix: keep long-running Myah dispatch streams active" --body-file /tmp/myah-long-run-dispatch-pr.md
```

PR body must include:

- root cause summary
- why prior fixes were insufficient
- exact tests/commands run with output summaries
- note that multi-hour agent turns are supported because there is no completion timeout, only non-terminal diagnostic logging intervals

## Risks / watch-outs

- Do not emit `run.completed` from a timeout path.
- Do not emit `run.completed` after `run.failed` or cancellation.
- Do not remove durable final fallback.
- Do not introduce a new frontend event shape in this PR unless tested end-to-end; log only.
- Do not cleanup mappings while the session is still active merely because `_STREAM_TTL` elapsed.
- Avoid broad refactors in `adapter.py`; the file is large and fragile.
