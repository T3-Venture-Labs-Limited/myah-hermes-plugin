from __future__ import annotations

import asyncio

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult


def _make_adapter(auth_key: str = '', **extra_kwargs):
    extra = dict(extra_kwargs)
    if auth_key:
        extra['auth_key'] = auth_key
    config = PlatformConfig(enabled=True, extra=extra)
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter

    return MyahAdapter(config)


@pytest.mark.asyncio
async def test_webhook_platform_delivery_routes_to_external_run_endpoint(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'https://myah.local')
    monkeypatch.setenv('MYAH_PLATFORM_BEARER', 'platform-bearer')
    monkeypatch.setenv('MYAH_USER_ID', 'user-1')

    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def text(self):
            return '{"ok":true}'

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, url, *, json, headers):
            captured['url'] = url
            captured['json'] = json
            captured['headers'] = headers
            return FakeResponse()

    monkeypatch.setattr(
        'aiohttp.ClientSession',
        FakeSession,
    )

    result = await adapter.send(
        'target-chat',
        'PR reviewed and merged.',
        metadata={
            'source_platform': 'webhook',
            'webhook_delivery': {
                'delivery_id': 'github-delivery-1',
                'route_name': 'myah-hosted-pr-review',
                'event_type': 'pull_request',
            },
            'webhook_payload': {
                'action': 'opened',
                'repository': {'full_name': 'T3-Venture-Labs-Limited/myah-hosted'},
                'pull_request': {
                    'number': 12,
                    'title': 'Add webhook visibility',
                    'html_url': 'https://github.com/T3-Venture-Labs-Limited/myah-hosted/pull/12',
                    'user': {'login': 'octocat'},
                },
            },
        },
    )

    assert result.success is True
    assert captured['url'] == 'https://myah.local/api/v1/processes/webhook/external-run-complete'
    assert captured['headers'] == {'Authorization': 'Bearer platform-bearer'}
    assert captured['json']['user_id'] == 'user-1'
    assert captured['json']['chat_id'] == 'target-chat'
    assert captured['json']['run_id'] == 'github-delivery-1'
    assert captured['json']['run_kind'] == 'github_pr_review'
    assert captured['json']['title'] == 'PR #12 in T3-Venture-Labs-Limited/myah-hosted: Add webhook visibility'
    assert captured['json']['response'] == 'PR reviewed and merged.'
    assert captured['json']['metadata']['repo'] == 'T3-Venture-Labs-Limited/myah-hosted'
    assert captured['json']['metadata']['pr_number'] == 12
    assert captured['json']['metadata']['pr_url'].endswith('/pull/12')


@pytest.mark.asyncio
async def test_webhook_platform_delivery_requires_env(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.delenv('MYAH_PLATFORM_BASE_URL', raising=False)
    monkeypatch.delenv('MYAH_PLATFORM_BEARER', raising=False)
    monkeypatch.delenv('MYAH_USER_ID', raising=False)

    result = await adapter.send(
        'target-chat',
        'content',
        metadata={'source_platform': 'webhook', 'webhook_delivery': {'delivery_id': 'd1'}},
    )

    assert result.success is False
    assert 'external run webhook env unavailable' in (result.error or '')


@pytest.mark.asyncio
async def test_webhook_platform_delivery_handles_network_failure(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv('MYAH_PLATFORM_BASE_URL', 'https://myah.local')
    monkeypatch.setenv('MYAH_PLATFORM_BEARER', 'platform-bearer')
    monkeypatch.setenv('MYAH_USER_ID', 'user-1')

    class FailingSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            raise RuntimeError('network down')

    monkeypatch.setattr('aiohttp.ClientSession', FailingSession)

    result = await adapter.send(
        'target-chat',
        'content',
        metadata={'source_platform': 'webhook', 'webhook_delivery': {'delivery_id': 'd1'}},
    )

    assert result.success is False
    assert 'external run webhook error' in (result.error or '')


@pytest.mark.asyncio
async def test_regular_chat_without_webhook_metadata_does_not_call_external_endpoint(monkeypatch):
    adapter = _make_adapter()
    adapter._loop = asyncio.get_running_loop()
    adapter._chat_id_streams['target-chat'] = 'stream-1'
    adapter._streams['stream-1'] = asyncio.Queue()

    async def fail_external(*args, **kwargs) -> SendResult:
        raise AssertionError('external run endpoint should not be used for regular chat')

    adapter._send_external_run_via_webhook = fail_external

    result = await adapter.send('target-chat', 'hello', metadata=None)

    assert result.success is True
