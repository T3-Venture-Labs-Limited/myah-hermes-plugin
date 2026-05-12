"""Tests for myah_hermes_plugin._provider_sync.sync_provider_state.

Vanilla hermes treats providers as two categories:
  1. PROVIDER_REGISTRY providers (openai-codex, zai, anthropic, ...) →
     auth.json:active_provider = <provider_id>.
  2. Non-registry "special" providers (openrouter) →
     auth.json:active_provider = None, plus OPENROUTER_API_KEY in .env.

PR #96 violated category 2 by setting active_provider="openrouter".
This module restores the vanilla invariant; these tests pin that behavior.
"""

from __future__ import annotations

from typing import Optional

import pytest


def _seed_auth_store(active_provider: Optional[str], pool: dict) -> None:
    from hermes_cli.auth import _save_auth_store

    store: dict = {
        'credential_pool': pool,
        'providers': {},
    }
    if active_provider is not None:
        store['active_provider'] = active_provider
    _save_auth_store(store)


def _read_auth_store() -> dict:
    from hermes_cli.auth import _load_auth_store

    return _load_auth_store()


def _pool_entry(provider_id: str, access_token: str = 'fake-token') -> dict:
    """Build a single credential_pool entry shaped like vanilla writes them."""
    return {
        provider_id: [
            {
                'id': f'{provider_id}-1',
                'access_token': access_token,
                'auth_type': 'api_key',
                'priority': 0,
            }
        ]
    }


@pytest.mark.asyncio
async def test_registered_provider_sets_active_provider():
    """Category 1: PROVIDER_REGISTRY provider sets active_provider=<id>."""
    from myah_hermes_plugin._provider_sync import sync_provider_state

    _seed_auth_store(active_provider=None, pool=_pool_entry('zai', 'sk-z'))

    result = sync_provider_state('zai')

    assert result['active_provider'] == 'zai'
    store = _read_auth_store()
    assert store['active_provider'] == 'zai'
    assert 'zai' in store.get('providers', {})


@pytest.mark.asyncio
async def test_openrouter_sets_active_provider_to_none():
    """Category 2: openrouter clears active_provider (vanilla rule)."""
    from myah_hermes_plugin._provider_sync import sync_provider_state

    _seed_auth_store(active_provider='openai-codex', pool=_pool_entry('openrouter', 'sk-or'))

    result = sync_provider_state('openrouter')

    assert result['active_provider'] is None
    store = _read_auth_store()
    assert store.get('active_provider') is None


@pytest.mark.asyncio
async def test_openrouter_writes_env_var_from_pool():
    """When .env lacks OPENROUTER_API_KEY, it must be written from the pool."""
    from hermes_cli.config import get_env_value
    from myah_hermes_plugin._provider_sync import sync_provider_state

    _seed_auth_store(active_provider=None, pool=_pool_entry('openrouter', 'sk-or-pool'))

    # Sanity: .env starts empty.
    assert not (get_env_value('OPENROUTER_API_KEY') or '').strip()

    result = sync_provider_state('openrouter')

    assert result['env_var_written'] == 'OPENROUTER_API_KEY'
    assert result['source'] == 'pool'
    assert get_env_value('OPENROUTER_API_KEY') == 'sk-or-pool'


@pytest.mark.asyncio
async def test_openrouter_writes_env_var_from_explicit_arg():
    """Explicit api_key argument wins over the credential_pool value."""
    from hermes_cli.config import get_env_value
    from myah_hermes_plugin._provider_sync import sync_provider_state

    _seed_auth_store(active_provider=None, pool=_pool_entry('openrouter', 'sk-or-pool'))

    result = sync_provider_state('openrouter', api_key='sk-or-explicit')

    assert result['env_var_written'] == 'OPENROUTER_API_KEY'
    assert result['source'] == 'explicit'
    assert get_env_value('OPENROUTER_API_KEY') == 'sk-or-explicit'


@pytest.mark.asyncio
async def test_idempotent_does_not_overwrite_existing_env_var():
    """If .env already has OPENROUTER_API_KEY, the helper must NOT overwrite it."""
    from hermes_cli.config import get_env_value, save_env_value
    from myah_hermes_plugin._provider_sync import sync_provider_state

    _seed_auth_store(active_provider=None, pool=_pool_entry('openrouter', 'sk-or-pool'))
    save_env_value('OPENROUTER_API_KEY', 'sk-or-existing')
    assert get_env_value('OPENROUTER_API_KEY') == 'sk-or-existing'

    result = sync_provider_state('openrouter', api_key='sk-or-different')

    # No overwrite — existing key preserved.
    assert result['env_var_written'] is None
    assert get_env_value('OPENROUTER_API_KEY') == 'sk-or-existing'


@pytest.mark.asyncio
async def test_returns_dict_with_active_provider_and_env_var_written():
    """Return shape: keys 'active_provider', 'env_var_written', 'source'."""
    from myah_hermes_plugin._provider_sync import sync_provider_state

    _seed_auth_store(active_provider=None, pool=_pool_entry('zai', 'sk-z'))

    result = sync_provider_state('zai')

    assert isinstance(result, dict)
    assert 'active_provider' in result
    assert 'env_var_written' in result
    assert 'source' in result


@pytest.mark.asyncio
async def test_unknown_provider_does_not_crash():
    """Unknown provider must not crash; returns dict with env_var_written=None."""
    from myah_hermes_plugin._provider_sync import sync_provider_state

    _seed_auth_store(active_provider=None, pool={})

    result = sync_provider_state('nonexistent')

    # Not in PROVIDER_REGISTRY → category 2 path → active_provider=None,
    # no env var (no special-case mapping).
    assert result['active_provider'] is None
    assert result['env_var_written'] is None
    assert result['source'] == 'none'
