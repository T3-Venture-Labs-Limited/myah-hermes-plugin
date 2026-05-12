"""Tests for myah_hermes_plugin.myah_platform._runner_state direct-access helpers.

These helpers replace 8 fork-only GatewayRunner public methods with direct
attribute access. The tests use a fake runner (SimpleNamespace + threading.Lock)
to avoid importing the full GatewayRunner.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_runner(running_agents=None, agent_cache=None, has_overrides=True):
    """Build a fake runner shaped like upstream's GatewayRunner."""
    runner = SimpleNamespace()
    if has_overrides:
        runner._session_model_overrides = {}
    runner._running_agents = dict(running_agents or {})
    runner._agent_cache = OrderedDict(agent_cache or [])
    runner._agent_cache_lock = threading.Lock()
    runner._evict_cached_agent = MagicMock()
    return runner


# --- set_session_override_direct -------------------------------------------

def test_set_session_override_writes_dict():
    from myah_hermes_plugin.myah_platform._runner_state import set_session_override_direct
    runner = _make_runner()
    set_session_override_direct(runner, 's1', {'model': 'claude-haiku', 'provider': 'anthropic'})
    assert runner._session_model_overrides['s1'] == {'model': 'claude-haiku', 'provider': 'anthropic'}


def test_set_session_override_evicts_cache():
    from myah_hermes_plugin.myah_platform._runner_state import set_session_override_direct
    runner = _make_runner()
    set_session_override_direct(runner, 's1', {'model': 'x'})
    runner._evict_cached_agent.assert_called_once_with('s1')


def test_set_session_override_initializes_missing_dict():
    """If _session_model_overrides doesn't exist (e.g. very early init), helper creates it."""
    from myah_hermes_plugin.myah_platform._runner_state import set_session_override_direct
    runner = _make_runner(has_overrides=False)
    assert not hasattr(runner, '_session_model_overrides')
    set_session_override_direct(runner, 's1', {'model': 'x'})
    assert runner._session_model_overrides == {'s1': {'model': 'x'}}


def test_set_session_override_swallows_evict_error(caplog):
    """If _evict_cached_agent raises, the override write must persist AND the error must be logged."""
    import logging

    from myah_hermes_plugin.myah_platform._runner_state import set_session_override_direct
    runner = _make_runner()
    runner._evict_cached_agent.side_effect = RuntimeError('lock contention')

    with caplog.at_level(logging.WARNING, logger='myah_hermes_plugin.myah_platform._runner_state'):
        set_session_override_direct(runner, 's1', {'model': 'x'})

    # Override still written despite eviction failure
    assert runner._session_model_overrides['s1'] == {'model': 'x'}
    # Warning was logged
    assert any(
        '_evict_cached_agent failed' in record.message
        for record in caplog.records
    ), f'Expected warning log; got records: {[r.message for r in caplog.records]}'


# --- get_session_override_direct -------------------------------------------

def test_get_session_override_returns_dict_when_set():
    from myah_hermes_plugin.myah_platform._runner_state import (
        get_session_override_direct,
        set_session_override_direct,
    )
    runner = _make_runner()
    set_session_override_direct(runner, 's1', {'model': 'x'})
    assert get_session_override_direct(runner, 's1') == {'model': 'x'}


def test_get_session_override_returns_none_when_unset():
    from myah_hermes_plugin.myah_platform._runner_state import get_session_override_direct
    runner = _make_runner()
    assert get_session_override_direct(runner, 'missing') is None


def test_get_session_override_handles_missing_attribute():
    from myah_hermes_plugin.myah_platform._runner_state import get_session_override_direct
    runner = _make_runner(has_overrides=False)
    assert get_session_override_direct(runner, 'any') is None


# --- evict_session_agent_direct --------------------------------------------

def test_evict_session_agent_returns_true_when_present():
    from myah_hermes_plugin.myah_platform._runner_state import evict_session_agent_direct
    runner = _make_runner(agent_cache=[('s1', ('agent_obj', {}))])
    assert evict_session_agent_direct(runner, 's1') is True
    runner._evict_cached_agent.assert_called_once_with('s1')


def test_evict_session_agent_returns_false_when_absent():
    from myah_hermes_plugin.myah_platform._runner_state import evict_session_agent_direct
    runner = _make_runner()
    assert evict_session_agent_direct(runner, 's1') is False


def test_evict_session_agent_handles_missing_cache():
    from myah_hermes_plugin.myah_platform._runner_state import evict_session_agent_direct
    runner = SimpleNamespace()  # no _agent_cache at all
    assert evict_session_agent_direct(runner, 's1') is False


def test_evict_session_agent_direct_swallows_evict_error(caplog):
    """If _evict_cached_agent raises during evict_session_agent_direct, the function must
    still return the correct boolean AND log a warning."""
    import logging

    from myah_hermes_plugin.myah_platform._runner_state import evict_session_agent_direct
    runner = _make_runner(agent_cache=[('s1', ('agent_obj', {}))])
    runner._evict_cached_agent.side_effect = RuntimeError('lock contention')

    with caplog.at_level(logging.WARNING, logger='myah_hermes_plugin.myah_platform._runner_state'):
        result = evict_session_agent_direct(runner, 's1')

    # Still returns True — the cache check happened BEFORE the eviction attempt
    assert result is True
    # Warning was logged
    assert any(
        '_evict_cached_agent failed' in record.message
        for record in caplog.records
    ), f'Expected warning log; got records: {[r.message for r in caplog.records]}'


# --- is_session_running_direct ---------------------------------------------

def test_is_session_running_true_when_present():
    from myah_hermes_plugin.myah_platform._runner_state import is_session_running_direct
    runner = _make_runner(running_agents={'s1': 'agent'})
    assert is_session_running_direct(runner, 's1') is True


def test_is_session_running_false_when_absent():
    from myah_hermes_plugin.myah_platform._runner_state import is_session_running_direct
    runner = _make_runner()
    assert is_session_running_direct(runner, 'missing') is False


# --- iter_running_session_keys_direct --------------------------------------

def test_iter_running_session_keys_returns_snapshot():
    from myah_hermes_plugin.myah_platform._runner_state import iter_running_session_keys_direct
    runner = _make_runner(running_agents={'s1': 'a', 's2': 'b'})
    keys = iter_running_session_keys_direct(runner)
    assert sorted(keys) == ['s1', 's2']
    # Mutating runner state shouldn't affect the snapshot
    runner._running_agents['s3'] = 'c'
    assert sorted(keys) == ['s1', 's2']


def test_iter_running_session_keys_empty_when_no_attr():
    from myah_hermes_plugin.myah_platform._runner_state import iter_running_session_keys_direct
    runner = SimpleNamespace()
    assert iter_running_session_keys_direct(runner) == []


# --- iter_cached_session_keys_direct ---------------------------------------

def test_iter_cached_session_keys_returns_snapshot():
    from myah_hermes_plugin.myah_platform._runner_state import iter_cached_session_keys_direct
    runner = _make_runner(agent_cache=[('s1', ('a', {})), ('s2', ('b', {}))])
    keys = iter_cached_session_keys_direct(runner)
    assert sorted(keys) == ['s1', 's2']


def test_iter_cached_session_keys_empty_when_no_attr():
    from myah_hermes_plugin.myah_platform._runner_state import iter_cached_session_keys_direct
    runner = SimpleNamespace()
    assert iter_cached_session_keys_direct(runner) == []


# --- get_cached_agent_direct -----------------------------------------------

def test_get_cached_agent_extracts_from_tuple():
    from myah_hermes_plugin.myah_platform._runner_state import get_cached_agent_direct
    agent = SimpleNamespace(model='claude', provider='anthropic')
    runner = _make_runner(agent_cache=[('s1', (agent, {'some': 'attribution'}))])
    assert get_cached_agent_direct(runner, 's1') is agent


def test_get_cached_agent_returns_none_when_absent():
    from myah_hermes_plugin.myah_platform._runner_state import get_cached_agent_direct
    runner = _make_runner()
    assert get_cached_agent_direct(runner, 'missing') is None


def test_get_cached_agent_handles_non_tuple_entry():
    """If the cache stores a bare agent (no tuple), helper returns it as-is."""
    from myah_hermes_plugin.myah_platform._runner_state import get_cached_agent_direct
    agent = SimpleNamespace(model='x')
    runner = _make_runner(agent_cache=[('s1', agent)])
    assert get_cached_agent_direct(runner, 's1') is agent


# --- get_cached_agent_attribution_direct -----------------------------------

def test_get_cached_agent_attribution_returns_model_provider():
    from myah_hermes_plugin.myah_platform._runner_state import (
        get_cached_agent_attribution_direct,
    )
    agent = SimpleNamespace(model='claude-haiku-4-5', provider='anthropic')
    runner = _make_runner(agent_cache=[('s1', (agent, {}))])
    result = get_cached_agent_attribution_direct(runner, 's1')
    assert result == {'model': 'claude-haiku-4-5', 'provider': 'anthropic'}


def test_get_cached_agent_attribution_handles_missing_attrs():
    """If the cached agent lacks model/provider attributes, fallback to empty strings."""
    from myah_hermes_plugin.myah_platform._runner_state import (
        get_cached_agent_attribution_direct,
    )
    agent = SimpleNamespace()  # no model, no provider
    runner = _make_runner(agent_cache=[('s1', (agent, {}))])
    result = get_cached_agent_attribution_direct(runner, 's1')
    assert result == {'model': '', 'provider': ''}


def test_get_cached_agent_attribution_returns_none_when_no_cache():
    from myah_hermes_plugin.myah_platform._runner_state import (
        get_cached_agent_attribution_direct,
    )
    runner = _make_runner()
    assert get_cached_agent_attribution_direct(runner, 'missing') is None
