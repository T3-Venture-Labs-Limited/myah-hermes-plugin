"""Tests for POST /myah/v1/aux/{task} — HTTP wrapper for auxiliary_client.call_llm."""
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request


def _make_aux_request(task: str, body: dict):
    """Build a mocked request for /myah/v1/aux/{task}."""
    request = make_mocked_request(
        'POST',
        f'/myah/v1/aux/{task}',
        match_info={'task': task},
    )
    request.json = AsyncMock(return_value=body)
    return request


def _make_adapter():
    """Construct a MyahAdapter with register_pre_setup_hook mocked out."""
    from gateway.config import PlatformConfig
    with patch('gateway.platforms.api_server.register_pre_setup_hook'):
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
        return MyahAdapter(PlatformConfig(enabled=True, extra={'auth_key': ''}))


@pytest.fixture(autouse=True)
def fake_aux_module():
    """Inject a fake agent.auxiliary_client module so tests don't need openai installed."""
    fake_llm = AsyncMock()
    fake_mod = types.ModuleType('agent.auxiliary_client')
    fake_mod.async_call_llm = fake_llm
    # The aux endpoint now falls back through extract_content_or_reasoning
    # so it can surface reasoning-model responses whose .content is None.
    # In tests, pass .content through untouched.
    fake_mod.extract_content_or_reasoning = lambda response: (
        response.choices[0].message.content
    )

    # Track whether we created the 'agent' parent package entry
    agent_existed = 'agent' in sys.modules
    if not agent_existed:
        agent_pkg = types.ModuleType('agent')
        agent_pkg.__path__ = []
        sys.modules['agent'] = agent_pkg
    sys.modules['agent.auxiliary_client'] = fake_mod

    yield fake_mod

    sys.modules.pop('agent.auxiliary_client', None)
    # Only remove the parent package if WE created it
    if not agent_existed:
        sys.modules.pop('agent', None)


@pytest.mark.asyncio
async def test_aux_rejects_unknown_task(fake_aux_module):
    adapter = _make_adapter()
    request = _make_aux_request('arbitrary_task', {'messages': [{'role': 'user', 'content': 'hi'}]})
    resp = await adapter._handle_aux_endpoint(request)
    assert resp.status == 400
    import json
    body = json.loads(resp.body)
    assert 'error' in body


@pytest.mark.asyncio
async def test_aux_accepts_title_generation(fake_aux_module):
    adapter = _make_adapter()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"title": "My Chat"}'
    mock_response.choices[0].finish_reason = 'stop'
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    fake_aux_module.async_call_llm = AsyncMock(return_value=mock_response)

    request = _make_aux_request('title_generation', {
        'messages': [{'role': 'user', 'content': 'hi'}],
        'response_format': {'type': 'json_object'},
        'max_tokens': 50,
    })
    resp = await adapter._handle_aux_endpoint(request)

    assert resp.status == 200
    import json
    body = json.loads(resp.body)
    assert body['choices'][0]['message']['content'] == '{"title": "My Chat"}'
    assert body['usage']['total_tokens'] == 15


@pytest.mark.asyncio
async def test_aux_accepts_follow_up_generation(fake_aux_module):
    adapter = _make_adapter()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"follow_ups": ["Q1", "Q2"]}'
    mock_response.choices[0].finish_reason = 'stop'
    mock_response.usage = None
    fake_aux_module.async_call_llm = AsyncMock(return_value=mock_response)

    request = _make_aux_request('follow_up_generation', {
        'messages': [{'role': 'user', 'content': 'hi'}],
    })
    resp = await adapter._handle_aux_endpoint(request)

    assert resp.status == 200
    import json
    body = json.loads(resp.body)
    assert 'follow_ups' in body['choices'][0]['message']['content']
    assert body['usage'] == {}  # usage None → empty dict


@pytest.mark.asyncio
async def test_aux_propagates_call_llm_error(fake_aux_module):
    adapter = _make_adapter()
    fake_aux_module.async_call_llm = AsyncMock(side_effect=RuntimeError('provider down'))

    request = _make_aux_request('title_generation', {
        'messages': [{'role': 'user', 'content': 'hi'}],
    })
    resp = await adapter._handle_aux_endpoint(request)

    assert resp.status == 502
    import json
    body = json.loads(resp.body)
    assert 'provider down' in body['error']


@pytest.mark.asyncio
async def test_aux_requires_messages_field(fake_aux_module):
    adapter = _make_adapter()
    request = _make_aux_request('title_generation', {})
    resp = await adapter._handle_aux_endpoint(request)
    assert resp.status == 400
