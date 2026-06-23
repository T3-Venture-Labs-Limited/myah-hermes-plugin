"""Tests for the Myah Replicate image-generation provider."""

from __future__ import annotations

import pytest


class FakeContext:
    def __init__(self):
        self.image_providers = []
        self.tools = []
        self.platforms = []
        self.hooks = []

    def register_image_gen_provider(self, provider):
        self.image_providers.append(provider)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_hook(self, *args, **kwargs):
        self.hooks.append((args, kwargs))


def test_myah_platform_registers_replicate_image_provider(monkeypatch):
    from myah_hermes_plugin.myah_platform import register

    monkeypatch.setattr('myah_hermes_plugin.sentry_init.setup_sentry', lambda: None)
    monkeypatch.setattr('myah_hermes_plugin.myah_platform._bootstrap_user_id', lambda: None)
    monkeypatch.setattr('myah_hermes_plugin.myah_platform._wire_secret_capture_callback', lambda: None)
    monkeypatch.setattr('myah_hermes_plugin.myah_platform._patch_webhook_myah_delivery', lambda: None)

    ctx = FakeContext()

    register(ctx)

    assert any(provider.name == 'replicate' for provider in ctx.image_providers)


def test_provider_availability_accepts_direct_token_or_hosted_broker(monkeypatch):
    from myah_hermes_plugin.image_gen.replicate import ReplicateImageGenProvider

    provider = ReplicateImageGenProvider()
    monkeypatch.delenv('REPLICATE_API_TOKEN', raising=False)
    monkeypatch.delenv('MYAH_REPLICATE_IMAGE_BROKER_URL', raising=False)

    assert provider.is_available() is False

    monkeypatch.setenv('REPLICATE_API_TOKEN', 'r8_test_token')
    assert provider.is_available() is True

    monkeypatch.delenv('REPLICATE_API_TOKEN', raising=False)
    monkeypatch.setenv('MYAH_REPLICATE_IMAGE_BROKER_URL', 'https://app.myah.dev/api/v1/agent/image-generation/replicate')
    assert provider.is_available() is True


def test_setup_schema_prompts_for_replicate_token():
    from myah_hermes_plugin.image_gen.replicate import ReplicateImageGenProvider

    schema = ReplicateImageGenProvider().get_setup_schema()

    assert schema['name'] == 'Replicate'
    assert schema['env_vars'][0]['key'] == 'REPLICATE_API_TOKEN'
    assert 'replicate.com/account/api-tokens' in schema['env_vars'][0]['url']


def test_model_resolution_precedence(monkeypatch):
    import myah_hermes_plugin.image_gen.replicate as replicate

    monkeypatch.setattr(replicate, '_load_image_gen_config', lambda: {'model': 'google/nano-banana-2'})
    monkeypatch.delenv('REPLICATE_IMAGE_MODEL', raising=False)
    assert replicate._resolve_model() == 'google/nano-banana-2'

    monkeypatch.setattr(
        replicate,
        '_load_image_gen_config',
        lambda: {'model': 'google/nano-banana-2', 'replicate': {'model': 'openai/gpt-image-2'}},
    )
    assert replicate._resolve_model() == 'openai/gpt-image-2'

    monkeypatch.setenv('REPLICATE_IMAGE_MODEL', 'google/nano-banana-2')
    assert replicate._resolve_model() == 'google/nano-banana-2'

    monkeypatch.setenv('REPLICATE_IMAGE_MODEL', 'unknown/model')
    monkeypatch.setattr(replicate, '_load_image_gen_config', lambda: {'model': 'unknown/model'})
    assert replicate._resolve_model() == replicate.DEFAULT_MODEL


@pytest.mark.parametrize(
    ('model', 'aspect', 'expected'),
    [
        ('openai/gpt-image-2', 'landscape', '3:2'),
        ('openai/gpt-image-2', 'square', '1:1'),
        ('openai/gpt-image-2', 'portrait', '2:3'),
        ('google/nano-banana-2', 'landscape', '16:9'),
        ('google/nano-banana-2', 'square', '1:1'),
        ('google/nano-banana-2', 'portrait', '9:16'),
    ],
)
def test_payload_uses_model_specific_aspect_ratio(monkeypatch, model, aspect, expected):
    import myah_hermes_plugin.image_gen.replicate as replicate

    captured = {}

    def fake_post_json(url, payload, headers, timeout):
        captured['url'] = url
        captured['payload'] = payload
        captured['headers'] = headers
        return {'id': 'pred-123', 'status': 'succeeded', 'output': ['https://replicate.delivery/pbxt/out.png']}

    monkeypatch.setenv('REPLICATE_API_TOKEN', 'r8_secret_token')
    monkeypatch.setattr(replicate, '_resolve_model', lambda: model)
    monkeypatch.setattr(replicate, '_post_json', fake_post_json)
    monkeypatch.setattr(replicate, 'save_url_image', lambda url, prefix: '/tmp/replicate_out.png')

    result = replicate.ReplicateImageGenProvider().generate('make a product ad', aspect_ratio=aspect)

    assert result['success'] is True
    assert captured['payload']['input']['aspect_ratio'] == expected
    assert captured['payload']['input']['prompt'] == 'make a product ad'
    assert captured['headers']['Authorization'] == 'Bearer r8_secret_token'
    assert 'r8_secret_token' not in str(result)


def test_direct_prediction_polls_until_success(monkeypatch):
    import myah_hermes_plugin.image_gen.replicate as replicate

    calls = []

    def fake_post_json(url, payload, headers, timeout):
        return {'id': 'pred-123', 'status': 'starting', 'urls': {'get': 'https://api.replicate.com/v1/predictions/pred-123'}}

    def fake_get_json(url, headers, timeout):
        calls.append(url)
        if len(calls) == 1:
            return {'id': 'pred-123', 'status': 'processing'}
        return {'id': 'pred-123', 'status': 'succeeded', 'output': 'https://replicate.delivery/pbxt/out.png'}

    monkeypatch.setenv('REPLICATE_API_TOKEN', 'r8_secret_token')
    monkeypatch.setattr(replicate, '_post_json', fake_post_json)
    monkeypatch.setattr(replicate, '_get_json', fake_get_json)
    monkeypatch.setattr(replicate.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(replicate, 'save_url_image', lambda url, prefix: '/tmp/replicate_out.png')

    result = replicate.ReplicateImageGenProvider().generate('make a product ad')

    assert result['success'] is True
    assert result['image'] == '/tmp/replicate_out.png'
    assert len(calls) == 2


def test_rate_limit_errors_are_clear(monkeypatch):
    import myah_hermes_plugin.image_gen.replicate as replicate

    def fake_post_json(url, payload, headers, timeout):
        raise replicate.ReplicateHTTPError(429, 'rate limited')

    monkeypatch.setenv('REPLICATE_API_TOKEN', 'r8_secret_token')
    monkeypatch.setattr(replicate, '_post_json', fake_post_json)

    result = replicate.ReplicateImageGenProvider().generate('make a product ad')

    assert result['success'] is False
    assert result['error_type'] == 'rate_limited'
    assert 'r8_secret_token' not in result['error']


def test_broker_path_does_not_require_direct_token(monkeypatch):
    import myah_hermes_plugin.image_gen.replicate as replicate

    captured = {}

    def fake_post_json(url, payload, headers, timeout):
        captured['url'] = url
        captured['headers'] = headers
        captured['payload'] = payload
        return {
            'success': True,
            'image': '/api/v1/files/file-1/content',
            'model': 'openai/gpt-image-2',
            'provider': 'replicate',
            'prediction_id': 'pred-123',
        }

    monkeypatch.delenv('REPLICATE_API_TOKEN', raising=False)
    monkeypatch.delenv('MYAH_PLATFORM_BEARER', raising=False)
    monkeypatch.setenv('MYAH_REPLICATE_IMAGE_BROKER_URL', 'https://app.myah.dev/api/v1/agent/image-generation/replicate')
    monkeypatch.setenv('MYAH_AGENT_BEARER_TOKEN', 'agent_token')
    monkeypatch.setenv('MYAH_USER_ID', 'user-123')
    monkeypatch.setattr(replicate, '_post_json', fake_post_json)

    result = replicate.ReplicateImageGenProvider().generate('make a product ad')

    assert result['success'] is True
    assert result['image'] == '/api/v1/files/file-1/content'
    assert captured['headers']['Authorization'] == 'Bearer agent_token'
    assert captured['payload']['user_id'] == 'user-123'
    assert 'REPLICATE_API_TOKEN' not in captured['headers']


def test_missing_credentials_do_not_fall_back_to_fal(monkeypatch):
    from myah_hermes_plugin.image_gen.replicate import ReplicateImageGenProvider

    monkeypatch.delenv('REPLICATE_API_TOKEN', raising=False)
    monkeypatch.delenv('MYAH_REPLICATE_IMAGE_BROKER_URL', raising=False)

    result = ReplicateImageGenProvider().generate('make a product ad')

    assert result['success'] is False
    assert result['error_type'] == 'auth_required'
    assert 'FAL' not in result['error'].upper()
