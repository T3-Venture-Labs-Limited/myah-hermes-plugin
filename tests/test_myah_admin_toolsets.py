"""Tests for the GET /api/plugins/myah-admin/toolsets endpoint.

Wraps upstream's GET /api/tools/toolsets (web_server.py:2745) via the
loopback proxy. This is the smoke-test blocker for Phase 7.7.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import (
    _common as _common_module,
    _skills_plugins_mcp as _spm_module,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(_common_module, '_get_session_token', lambda: None)
    application = FastAPI()
    application.include_router(_spm_module.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def test_get_toolsets_proxies_to_native(client, monkeypatch):
    """GET /toolsets must proxy to /api/tools/toolsets and return the body."""
    fake_response = [
        {
            'name': 'browser',
            'label': 'Browser',
            'description': 'Web browsing tools',
            'enabled': True,
            'available': True,
            'configured': True,
            'tools': ['browser_navigate', 'browser_click'],
        },
    ]

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        return fake_response

    monkeypatch.setattr(_spm_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/toolsets')
    assert resp.status_code == 200
    assert resp.json() == fake_response
    assert captured == {'method': 'GET', 'path': '/api/tools/toolsets'}
