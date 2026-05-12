"""Tests for plugin sessions list + messages loopback endpoints.

Wraps upstream GET /api/sessions and GET /api/sessions/{id}/messages via
the loopback proxy. Phase 7.7 plugin migration (2026-05-12) —
see docs/superpowers/specs/2026-05-12-plugin-dashboard-migration-design.md.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import (
    _common as _common_module,
    _sessions_and_lifecycle as _sl_module,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(_common_module, '_get_session_token', lambda: None)
    application = FastAPI()
    application.include_router(_sl_module.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def test_list_sessions_proxies(client, monkeypatch):
    """GET /sessions must proxy to /api/sessions with limit/offset params."""
    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['params'] = kwargs.get('params')
        return {
            'sessions': [{'id': 'sess-1', 'title': 'Hello'}],
            'total': 1,
            'limit': 50,
            'offset': 0,
        }

    monkeypatch.setattr(_sl_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/sessions', params={'limit': 25, 'offset': 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body['sessions'][0]['id'] == 'sess-1'
    assert captured == {
        'method': 'GET',
        'path': '/api/sessions',
        'params': {'limit': 25, 'offset': 10},
    }


def test_get_session_messages_proxies(client, monkeypatch):
    """GET /sessions/{id}/messages must proxy to /api/sessions/{id}/messages."""
    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        return {
            'session_id': 'abc-123',
            'messages': [
                {'id': 'm1', 'role': 'user', 'content': 'Hi'},
                {'id': 'm2', 'role': 'assistant', 'content': 'Hello'},
            ],
        }

    monkeypatch.setattr(_sl_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/sessions/abc-123/messages')
    assert resp.status_code == 200
    body = resp.json()
    assert body['session_id'] == 'abc-123'
    assert len(body['messages']) == 2
    assert captured == {
        'method': 'GET',
        'path': '/api/sessions/abc-123/messages',
    }
