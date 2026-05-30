"""Tests for myah-admin cron job metadata patch endpoint."""
from __future__ import annotations

import copy
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import _cron_jobs


def _client(monkeypatch) -> TestClient:
    monkeypatch.delenv("HERMES_WEB_SESSION_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(_cron_jobs.router)
    return TestClient(app)


def test_patch_myah_metadata_merges_only_myah_namespace_and_preserves_origin(monkeypatch):
    client = _client(monkeypatch)
    job = {
        "id": "abcdef012345",
        "name": "digest",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "tg-1"},
        "myah": {"chat_id": "old-chat", "legacy_origin": {"platform": "telegram"}},
    }
    saved = []

    def fake_update_job(job_id, patch):
        assert job_id == "abcdef012345"
        # Mirror upstream update_job semantics: shallow top-level replacement.
        # The endpoint itself must have merged existing job.myah before this call.
        merged = {**copy.deepcopy(job), **copy.deepcopy(patch)}
        saved.append(merged)
        return merged

    with (
        patch.object(_cron_jobs.cron_jobs, "load_jobs", return_value=[copy.deepcopy(job)]),
        patch.object(_cron_jobs.cron_jobs, "update_job", side_effect=fake_update_job),
    ):
        resp = client.post(
            "/cron/jobs/abcdef012345/myah-metadata",
            json={"myah": {"chat_id": "new-chat", "adopted_at": "2026-05-29T00:00:00Z"}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["job"]["myah"]["chat_id"] == "new-chat"
    assert body["job"]["myah"]["adopted_at"] == "2026-05-29T00:00:00Z"
    assert body["job"]["myah"]["legacy_origin"] == {"platform": "telegram"}
    assert body["job"]["origin"] == {"platform": "telegram", "chat_id": "tg-1"}
    assert body["job"]["deliver"] == "origin"
    assert saved[0]["origin"] == job["origin"]


def test_patch_myah_metadata_rejects_unknown_nested_myah_keys(monkeypatch):
    client = _client(monkeypatch)
    with (
        patch.object(_cron_jobs.cron_jobs, "load_jobs", return_value=[{"id": "abcdef012345"}]),
        patch.object(_cron_jobs.cron_jobs, "update_job") as update_job,
    ):
        resp = client.post(
            "/cron/jobs/abcdef012345/myah-metadata",
            json={"myah": {"chat_id": "c", "unexpected": "value"}},
        )
    assert resp.status_code == 422
    update_job.assert_not_called()


def test_patch_myah_metadata_rejects_non_string_chat_id(monkeypatch):
    client = _client(monkeypatch)
    with (
        patch.object(_cron_jobs.cron_jobs, "load_jobs", return_value=[{"id": "abcdef012345"}]),
        patch.object(_cron_jobs.cron_jobs, "update_job") as update_job,
    ):
        resp = client.post(
            "/cron/jobs/abcdef012345/myah-metadata",
            json={"myah": {"chat_id": {"not": "a string"}}},
        )
    assert resp.status_code == 422
    update_job.assert_not_called()


def test_patch_myah_metadata_rejects_invalid_job_id(monkeypatch):
    client = _client(monkeypatch)
    with patch.object(_cron_jobs.cron_jobs, "update_job") as update_job:
        resp = client.post("/cron/jobs/../etc/myah-metadata", json={"myah": {"chat_id": "c"}})
    assert resp.status_code in (404, 422)
    update_job.assert_not_called()


def test_patch_myah_metadata_rejects_unknown_top_level_keys(monkeypatch):
    client = _client(monkeypatch)
    with patch.object(_cron_jobs.cron_jobs, "update_job") as update_job:
        resp = client.post(
            "/cron/jobs/abcdef012345/myah-metadata",
            json={"myah": {"chat_id": "c"}, "origin": {"platform": "myah"}},
        )
    assert resp.status_code == 422
    update_job.assert_not_called()


def test_patch_myah_metadata_returns_404_for_missing_job(monkeypatch):
    client = _client(monkeypatch)
    with patch.object(_cron_jobs.cron_jobs, "load_jobs", return_value=[]):
        resp = client.post("/cron/jobs/abcdef012345/myah-metadata", json={"myah": {"chat_id": "c"}})
    assert resp.status_code == 404
