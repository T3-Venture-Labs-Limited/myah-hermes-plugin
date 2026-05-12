"""Tests for the /api/plugins/myah-admin/config + /config/schema endpoints.

Wraps upstream's GET/PUT /api/config (web_server.py:856 / :1161) and
GET /api/config/schema via the loopback proxy. These three handlers
are part of the Phase 7.7 plugin migration (PR 3).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import (
    _common as _common_module,
    _soul_and_config as _sc_module,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(_common_module, '_get_session_token', lambda: None)
    application = FastAPI()
    application.include_router(_sc_module.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def test_get_config_proxies(client, monkeypatch):
    """GET /config must proxy to /api/config and return the body."""
    fake_response = {
        'model': {'provider': 'openrouter', 'default': 'google/gemini-3-flash-preview'},
        'auxiliary': {},
    }

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['kwargs'] = kwargs
        return fake_response

    monkeypatch.setattr(_sc_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/config')
    assert resp.status_code == 200
    assert resp.json() == fake_response
    assert captured['method'] == 'GET'
    assert captured['path'] == '/api/config'


def test_put_config_proxies_with_body(client, monkeypatch):
    """PUT /config with json body must proxy to /api/config with the same body."""
    fake_response = {'ok': True}
    payload = {'config': {'model': {'provider': 'anthropic', 'default': 'claude-opus-4'}}}

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['kwargs'] = kwargs
        return fake_response

    monkeypatch.setattr(_sc_module, 'proxy_to_native', _fake_proxy)

    resp = client.put('/config', json=payload)
    assert resp.status_code == 200
    assert resp.json() == fake_response
    assert captured['method'] == 'PUT'
    assert captured['path'] == '/api/config'
    assert captured['kwargs'].get('json_body') == payload


def test_get_schema_proxies(client, monkeypatch):
    """GET /config/schema must proxy to /api/config/schema and return the schema."""
    fake_schema = {
        'type': 'object',
        'properties': {
            'model': {'type': 'object'},
            'auxiliary': {'type': 'object'},
        },
    }

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        captured['kwargs'] = kwargs
        return fake_schema

    monkeypatch.setattr(_sc_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/config/schema')
    assert resp.status_code == 200
    assert resp.json() == fake_schema
    assert captured['method'] == 'GET'
    assert captured['path'] == '/api/config/schema'
