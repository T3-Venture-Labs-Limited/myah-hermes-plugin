"""Direct attribute access helpers for GatewayRunner session state (Tier 2B.0).

Replaces 8 fork-only public methods on ``GatewayRunner`` with helpers that
read and write upstream-native private attributes directly. Lets the plugin
run on stock upstream Hermes without modification.

Architectural rationale: the 8 wrapper methods (``get_session_override``,
``set_session_override``, ``evict_session_agent``, ``is_session_running``,
``iter_running_session_keys``, ``iter_cached_session_keys``,
``get_cached_agent``, ``get_cached_agent_attribution``) are pure syntactic
sugar around private attributes that exist on upstream's GatewayRunner:

    _session_model_overrides    # dict — line 1008/1086 of upstream/main
    _agent_cache                # OrderedDict — line 1081 of upstream/main
    _agent_cache_lock           # threading.Lock — line 1082 of upstream/main
    _running_agents             # dict — line 1053 of upstream/main
    _evict_cached_agent(key)    # method — line 12250 of upstream/main

The wrappers add ``getattr(self, attr, None) or {}`` for graceful degradation
on rename. These helpers do the same defensive pattern, plus a CI guard test
(``tests/test_upstream_runner_attrs_present.py``) catches renames at plugin
CI time.

Per agent/hermes/AGENTS.md:478-483 (Teknium May 2026): plugins must not
modify core files. Reading/writing instance attributes on a framework-
provided object is not modifying any file — same shape as calling any
public method, just bypassing a syntactic-sugar wrapper.

Existing precedent: ``adapter.py:407`` already accesses
``runner._session_model_overrides.pop(...)`` directly (with comment
"Public API doesn't expose a clear method; pop directly from the
documented private dict"). This module makes the pattern uniform.

See spec §3.2.1 for full empirical evidence and Tier 2B Task 2B.0 for
the migration plan.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from gateway.run import GatewayRunner  # noqa: F401 — type-only

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

def set_session_override_direct(
    runner: 'GatewayRunner', session_key: str, body: Dict[str, Any]
) -> None:
    """Replaces runner.set_session_override(session_key, body).

    Writes the override into the runner's own _session_model_overrides
    dict, then evicts the cached agent (matching the upstream wrapper's
    semantics). Initializes the dict if it didn't exist (defensive).
    """
    overrides = getattr(runner, '_session_model_overrides', None)
    if overrides is None:
        runner._session_model_overrides = {}
        overrides = runner._session_model_overrides
    overrides[session_key] = dict(body)
    try:
        runner._evict_cached_agent(session_key)
    except Exception:
        # _evict_cached_agent already swallows cache-miss; we only
        # catch unexpected lock errors so the override write is not
        # undone by an eviction failure (matches upstream wrapper
        # behaviour at gateway/run.py:9437-9447 of the fork).
        logger.warning(
            '[myah] _evict_cached_agent failed for %s during set_session_override',
            session_key,
            exc_info=True,
        )


def get_session_override_direct(
    runner: 'GatewayRunner', session_key: str
) -> Optional[Dict[str, Any]]:
    """Replaces runner.get_session_override(session_key) -> Optional[Dict]."""
    overrides = getattr(runner, '_session_model_overrides', None) or {}
    return overrides.get(session_key)


def evict_session_agent_direct(runner: 'GatewayRunner', session_key: str) -> bool:
    """Replaces runner.evict_session_agent(session_key) -> bool.

    Returns True if there was a cached entry (now removed), False if
    nothing was cached. Mirrors the upstream wrapper at
    gateway/run.py:9449-9469 of the fork.
    """
    cache = getattr(runner, '_agent_cache', None)
    if cache is None:
        return False
    lock = getattr(runner, '_agent_cache_lock', None)
    had_entry = False
    if lock is not None:
        with lock:
            had_entry = session_key in cache
    else:
        had_entry = session_key in cache
    # Use upstream's _evict_cached_agent which handles its own locking.
    try:
        runner._evict_cached_agent(session_key)
    except Exception:
        logger.warning(
            '[myah] _evict_cached_agent failed for %s in evict_session_agent_direct',
            session_key,
            exc_info=True,
        )
    return had_entry


def is_session_running_direct(runner: 'GatewayRunner', session_key: str) -> bool:
    """Replaces runner.is_session_running(session_key) -> bool."""
    running = getattr(runner, '_running_agents', None) or {}
    return session_key in running


def iter_running_session_keys_direct(runner: 'GatewayRunner') -> List[str]:
    """Replaces runner.iter_running_session_keys() -> List[str].

    Returns a snapshot list (materialised eagerly) so callers can
    iterate without contending on the live dict.
    """
    running = getattr(runner, '_running_agents', None) or {}
    return list(running.keys())


def iter_cached_session_keys_direct(runner: 'GatewayRunner') -> List[str]:
    """Replaces runner.iter_cached_session_keys() -> List[str].

    Snapshot taken under _agent_cache_lock (when available) so callers
    can safely iterate without holding the lock.
    """
    cache = getattr(runner, '_agent_cache', None)
    if cache is None:
        return []
    lock = getattr(runner, '_agent_cache_lock', None)
    if lock is not None:
        with lock:
            return list(cache.keys())
    return list(cache.keys())


def get_cached_agent_direct(runner: 'GatewayRunner', session_key: str) -> Optional[Any]:
    """Replaces runner.get_cached_agent(session_key) -> Optional[Any].

    The cache stores tuples (agent, attribution) per Hermes' convention;
    extract the agent at index 0 if it's a tuple.
    """
    cache = getattr(runner, '_agent_cache', None)
    if cache is None:
        return None
    lock = getattr(runner, '_agent_cache_lock', None)
    if lock is not None:
        with lock:
            entry = cache.get(session_key)
    else:
        entry = cache.get(session_key)
    if entry is None:
        return None
    return entry[0] if isinstance(entry, tuple) else entry


def get_cached_agent_attribution_direct(
    runner: 'GatewayRunner', session_key: str
) -> Optional[Dict[str, str]]:
    """Replaces runner.get_cached_agent_attribution(session_key) -> Optional[Dict].

    Returns {"model": ..., "provider": ...} extracted from the cached
    AIAgent instance, or None if no cached agent.
    """
    agent = get_cached_agent_direct(runner, session_key)
    if agent is None:
        return None
    return {
        'model': getattr(agent, 'model', '') or '',
        'provider': getattr(agent, 'provider', '') or '',
    }
