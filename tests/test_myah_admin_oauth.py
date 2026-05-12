"""Tests for plugin OAuth device-flow endpoints.

Wraps upstream's POST /api/providers/oauth/{provider_id}/start and
GET /api/providers/oauth/{provider_id}/poll/{session_id} via the
loopback proxy. Used by platform/providers.py:245, :265.

Spec includes /submit and /cancel for completeness even though the
platform doesn't currently call them.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import (
    _common as _common_module,
    _providers as _providers_module,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(_common_module, '_get_session_token', lambda: None)
    application = FastAPI()
    application.include_router(_providers_module.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def test_oauth_start_proxies_to_native(client, monkeypatch):
    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        return {
            'verification_url': 'https://auth.openai.com/oauth/device',
            'session_id': 'sess-abc123',
            'expires_at': 1700000000,
        }

    monkeypatch.setattr(_providers_module, 'proxy_to_native', _fake_proxy)

    resp = client.post('/providers/oauth/openai-codex/start')
    assert resp.status_code == 200
    assert resp.json()['session_id'] == 'sess-abc123'
    assert captured == {
        'method': 'POST',
        'path': '/api/providers/oauth/openai-codex/start',
    }


def test_oauth_poll_proxies_to_native(client, monkeypatch):
    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        return {
            'session_id': 'sess-abc123',
            'status': 'pending',
            'error_message': None,
            'expires_at': 1700000000,
        }

    monkeypatch.setattr(_providers_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/providers/oauth/openai-codex/poll/sess-abc123')
    assert resp.status_code == 200
    assert resp.json()['status'] == 'pending'
    assert captured == {
        'method': 'GET',
        'path': '/api/providers/oauth/openai-codex/poll/sess-abc123',
    }


def test_oauth_submit_proxies_to_native(client, monkeypatch):
    """Completeness: /submit endpoint for PKCE flows."""
    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['json_body'] = kwargs.get('json_body')
        return {'status': 'completed'}

    monkeypatch.setattr(_providers_module, 'proxy_to_native', _fake_proxy)

    resp = client.post(
        '/providers/oauth/anthropic/submit',
        json={'session_id': 'sess-1', 'code': 'auth-code-xyz'},
    )
    assert resp.status_code == 200
    assert captured['method'] == 'POST'
    assert captured['path'] == '/api/providers/oauth/anthropic/submit'
    assert captured['json_body'] == {'session_id': 'sess-1', 'code': 'auth-code-xyz'}


def test_oauth_cancel_proxies_to_native(client, monkeypatch):
    """Completeness: DELETE /sessions/{id} for cancelling a pending flow."""
    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        return {'ok': True, 'session_id': 'sess-1'}

    monkeypatch.setattr(_providers_module, 'proxy_to_native', _fake_proxy)

    resp = client.delete('/providers/oauth/sessions/sess-1')
    assert resp.status_code == 200
    assert captured == {
        'method': 'DELETE',
        'path': '/api/providers/oauth/sessions/sess-1',
    }
