"""Tests for the GET /api/plugins/myah-admin/skills endpoint.

Wraps upstream's GET /api/skills (web_server.py:2720) via the loopback
proxy. Phase 7.7 plugin migration (2026-05-12).
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


def test_get_skills_proxies_to_native(client, monkeypatch):
    """GET /skills must proxy to /api/skills and return the body."""
    fake_response = [
        {
            'name': 'dogfood',
            'description': 'Systematically explore and test a web application.',
            'category': None,
            'enabled': True,
        },
        {
            'name': 'commit',
            'description': 'Creates commits following Sentry conventions.',
            'category': 'general',
            'enabled': True,
        },
    ]

    captured = {}

    async def _fake_proxy(method, path, **kwargs):
        captured['method'] = method
        captured['path'] = path
        return fake_response

    monkeypatch.setattr(_spm_module, 'proxy_to_native', _fake_proxy)

    resp = client.get('/skills')
    assert resp.status_code == 200
    assert resp.json() == fake_response
    assert captured == {'method': 'GET', 'path': '/api/skills'}
