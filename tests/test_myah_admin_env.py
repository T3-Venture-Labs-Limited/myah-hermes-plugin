"""Tests for the GET / PUT / DELETE /api/plugins/myah-admin/env endpoints.

Wraps upstream's GET / PUT / DELETE /api/env (web_server.py:1224, 1243,
1253) via the loopback proxy. Phase 7.7 plugin migration (2026-05-12).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import (
    _common as _common_module,
    _env as _env_module,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(_common_module, '_get_session_token', lambda: None)
    application = FastAPI()
    application.include_router(_env_module.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def test_get_env_proxies(client, monkeypatch):
    """GET /env must proxy to /api/env and return the body."""
    fake_response = {
        'OPENROUTER_API_KEY': {
            'is_set': True,
            'redacted_value': 'sk-or-***',
            'description': 'OpenRouter API key',
            'url': 'https://openrouter.ai',
            'category': 'provider',
            'is_password': True,
            'tools': [],
            'advanced': False,
        },
    }

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['kwargs'] = kwargs
        return fake_response

    monkeypatch.setattr(_env_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/env')
    assert resp.status_code == 200
    assert resp.json() == fake_response
    assert captured['method'] == 'GET'
    assert captured['path'] == '/api/env'


def test_put_env_proxies_with_body(client, monkeypatch):
    """PUT /env must forward the body to /api/env."""
    fake_response = {'ok': True}

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['json_body'] = kwargs.get('json_body')
        return fake_response

    monkeypatch.setattr(_env_module, 'proxy_to_native', _fake_proxy)

    resp = client.put('/env', json={'key': 'MY_KEY', 'value': 'my-value'})
    assert resp.status_code == 200
    assert resp.json() == fake_response
    assert captured['method'] == 'PUT'
    assert captured['path'] == '/api/env'
    assert captured['json_body'] == {'key': 'MY_KEY', 'value': 'my-value'}


def test_delete_env_proxies_with_body(client, monkeypatch):
    """DELETE /env must forward the body to /api/env."""
    fake_response = {'ok': True}

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['json_body'] = kwargs.get('json_body')
        return fake_response

    monkeypatch.setattr(_env_module, 'proxy_to_native', _fake_proxy)

    # TestClient.delete doesn't accept JSON natively; use the lower-level
    # ``request`` method so the body is preserved on the DELETE.
    resp = client.request('DELETE', '/env', json={'key': 'MY_KEY'})
    assert resp.status_code == 200
    assert resp.json() == fake_response
    assert captured['method'] == 'DELETE'
    assert captured['path'] == '/api/env'
    assert captured['json_body'] == {'key': 'MY_KEY'}
