"""Tests for MyahAdapter.build_delivery_metadata.

Tier 2B Task 2B.4 / Phase 4f plugin side. Overrides the optional
BasePlatformAdapter.build_delivery_metadata hook (added fork-side in
the same task) to enrich cron-delivery metadata so the offline-webhook
fallback at _send_via_webhook receives job_id, job_name, status,
ran_at, and origin.

Replaces the deleted cron/scheduler.py:_build_myah_send_metadata helper.
"""
from __future__ import annotations

import datetime as dt


def _build_adapter():
    """Construct a MyahAdapter without invoking aiohttp/network setup.

    Uses object.__new__ to bypass __init__ — build_delivery_metadata is
    a pure function of (job, status_hint, base_metadata) and does not
    touch instance state.
    """
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
    return object.__new__(MyahAdapter)


def test_basic_fields_populated():
    """Enriches the dict with job_id, job_name, status, ran_at."""
    adapter = _build_adapter()
    result = adapter.build_delivery_metadata(
        job={"id": "job-123", "name": "Daily Report", "origin": None},
        status_hint="ok",
        base_metadata={"thread_id": "thread-abc"},
    )
    assert result["job_id"] == "job-123"
    assert result["job_name"] == "Daily Report"
    assert result["status"] == "ok"
    assert result["thread_id"] == "thread-abc"  # base preserved
    assert "ran_at" in result
    assert result["origin"] is None


def test_ran_at_is_iso_utc():
    """ran_at is an ISO-8601 UTC timestamp parseable by datetime.fromisoformat."""
    adapter = _build_adapter()
    result = adapter.build_delivery_metadata(
        job={"id": "j", "name": "n"}, status_hint="ok",
    )
    parsed = dt.datetime.fromisoformat(result["ran_at"])
    # Must carry timezone info (UTC) so the platform can render it correctly.
    assert parsed.tzinfo is not None


def test_preserves_origin_when_complete():
    """origin is preserved verbatim when both platform and chat_id present."""
    adapter = _build_adapter()
    origin = {"platform": "myah", "chat_id": "chat-42", "user_id": "user-7"}
    result = adapter.build_delivery_metadata(
        job={"id": "job-9", "name": "Job 9", "origin": origin},
        status_hint="error",
    )
    assert result["origin"] == origin
    assert result["status"] == "error"


def test_drops_invalid_origin_missing_chat_id():
    """origin missing chat_id collapses to None."""
    adapter = _build_adapter()
    result = adapter.build_delivery_metadata(
        job={"id": "j", "name": "n", "origin": {"platform": "myah"}},
        status_hint="ok",
    )
    assert result["origin"] is None


def test_returns_copy_not_reference():
    """Caller mutations of the returned dict do NOT affect base_metadata.

    Parity with BasePlatformAdapter default's `dict(base_metadata)` semantics.
    """
    adapter = _build_adapter()
    base = {"thread_id": "t-1"}
    result = adapter.build_delivery_metadata(
        job={"id": "j", "name": "n"}, status_hint="ok", base_metadata=base,
    )
    result["thread_id"] = "mutated"
    result["job_id"] = "evil"
    assert base["thread_id"] == "t-1"
    assert "job_id" not in base


def test_job_name_falls_back_to_job_id_when_name_empty():
    """If job dict has no 'name' (or empty string), job_name falls back to id."""
    adapter = _build_adapter()
    result = adapter.build_delivery_metadata(
        job={"id": "fallback-id", "name": ""},
        status_hint="ok",
    )
    assert result["job_name"] == "fallback-id"
