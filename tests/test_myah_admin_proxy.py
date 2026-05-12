"""Tests for the loopback HTTP proxy helper.

The proxy forwards requests from the plugin's auth-exempt namespace
(/api/plugins/myah-admin/*) into the dashboard's native /api/* surface,
using the dashboard's own _SESSION_TOKEN so auth_middleware accepts the
request.

Test strategy
-------------
The plan's original snippet uses
``monkeypatch.setattr("hermes_cli.web_server._SESSION_TOKEN", ...)``.
That only works when ``hermes_cli.web_server`` is importable — which it
is at runtime inside the dashboard process, but NOT in the plugin's
isolated test venv (the dashboard is loaded by upstream Hermes' bundled
CLI, which is only present on PATH inside the agent container).

To keep the test portable we install a fake ``hermes_cli.web_server``
module into ``sys.modules`` before exercising the proxy. The proxy
imports ``_SESSION_TOKEN`` lazily from inside ``proxy_to_native`` so
this swap is picked up. The final test in this file
(``test_session_token_import_path_exists_or_documents_environment``)
documents which mode we're running in.
"""

from __future__ import annotations

import sys
import types

import httpx
import pytest
import respx
from fastapi import HTTPException
from httpx import Response

from myah_hermes_plugin.myah_admin.dashboard import _proxy as _proxy_module


def _install_fake_web_server(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """Inject a stand-in ``hermes_cli.web_server`` module with the given token.

    Uses ``monkeypatch.setitem`` so the fake module is removed automatically
    when the test ends, preventing cross-test contamination.
    """
    if 'hermes_cli' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'hermes_cli', types.ModuleType('hermes_cli'))
    fake = types.SimpleNamespace(_SESSION_TOKEN=token)
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', fake)


@pytest.mark.asyncio
async def test_proxy_forwards_get_with_session_token(monkeypatch):
    """A GET via proxy must hit localhost:9119/api/<native> with the
    dashboard's _SESSION_TOKEN as the Authorization header."""
    fake_token = 'tok-fixture-abc'
    _install_fake_web_server(monkeypatch, fake_token)

    with respx.mock(base_url='http://localhost:9119') as mock:
        route = mock.get('/api/tools/toolsets').mock(
            return_value=Response(200, json=[{'name': 'browser'}]),
        )
        result = await _proxy_module.proxy_to_native('GET', '/api/tools/toolsets')

    assert result == [{'name': 'browser'}]
    assert route.called
    sent_headers = route.calls[0].request.headers
    assert sent_headers['authorization'] == f'Bearer {fake_token}'


@pytest.mark.asyncio
async def test_proxy_forwards_put_with_json_body(monkeypatch):
    _install_fake_web_server(monkeypatch, 'tok-1')

    with respx.mock(base_url='http://localhost:9119') as mock:
        route = mock.put('/api/config').mock(
            return_value=Response(200, json={'ok': True}),
        )
        result = await _proxy_module.proxy_to_native(
            'PUT', '/api/config', json_body={'config': {'model': 'x-ai/grok-4'}},
        )

    assert result == {'ok': True}
    assert route.called
    body = route.calls[0].request.read()
    assert b'"model"' in body
    assert b'"x-ai/grok-4"' in body


@pytest.mark.asyncio
async def test_proxy_raises_on_4xx_with_native_body(monkeypatch):
    """Non-2xx from native handler is propagated as an HTTPException
    with the same status code + body."""
    _install_fake_web_server(monkeypatch, 'tok-1')

    with respx.mock(base_url='http://localhost:9119') as mock:
        mock.get('/api/skills').mock(
            return_value=Response(404, json={'detail': 'Not found'}),
        )
        with pytest.raises(HTTPException) as excinfo:
            await _proxy_module.proxy_to_native('GET', '/api/skills')

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == {'detail': 'Not found'}


@pytest.mark.asyncio
async def test_proxy_503_on_connection_error(monkeypatch):
    """If the dashboard isn't reachable, proxy returns 503 (not 500)."""
    _install_fake_web_server(monkeypatch, 'tok-1')

    with respx.mock(base_url='http://localhost:9119') as mock:
        mock.get('/api/tools/toolsets').mock(side_effect=httpx.ConnectError('refused'))
        with pytest.raises(HTTPException) as excinfo:
            await _proxy_module.proxy_to_native('GET', '/api/tools/toolsets')

    assert excinfo.value.status_code == 503


def test_session_token_import_path_exists_or_documents_environment():
    """CI guard: ``_SESSION_TOKEN`` MUST be importable from
    ``hermes_cli.web_server`` in any environment that ships the
    dashboard (the production agent container).

    The plugin's isolated test venv does not pip-install the bundled
    ``hermes_cli`` CLI (it's bundled with the hermes-agent core repo,
    not the plugin's deps), so we skip rather than fail here. If you
    are running this test inside an environment that DOES have the
    dashboard installed and this skip fires, the dashboard runtime is
    broken — investigate the import.
    """
    try:
        from hermes_cli.web_server import _SESSION_TOKEN  # noqa: F401
    except ImportError:
        pytest.skip(
            'hermes_cli.web_server not importable in this env — '
            'expected outside the agent container; the proxy is '
            'exercised end-to-end inside the Mode D image build.',
        )
