"""CI guard: assert upstream-native private attrs exist on GatewayRunner.

Tier 2B.0 refactored the plugin from fork-only wrapper methods to direct
attribute access on upstream's GatewayRunner. If upstream renames any of
these attrs, the plugin's runtime_admin endpoints would silently fail at
runtime. This test catches the rename at plugin CI time, before deploy.

Per spec §3.2.1 robustness story.
"""
from __future__ import annotations

import inspect


def test_session_model_overrides_attr_exists():
    """GatewayRunner exposes _session_model_overrides dict.

    Used by:
    - /myah/v1/admin/sessions/{id}/override (PUT/GET/DELETE)
    - MyahAdapter._handle_message_endpoint (one-shot model override)
    """
    from gateway.run import GatewayRunner

    # Class-level annotation is present even before __init__
    has_attr = (
        '_session_model_overrides' in GatewayRunner.__annotations__
        or hasattr(GatewayRunner, '_session_model_overrides')
    )
    if not has_attr:
        # Fallback: inspect __init__ source for runtime initialization
        init_src = inspect.getsource(GatewayRunner.__init__)
        has_attr = '_session_model_overrides' in init_src

    assert has_attr, (
        'Upstream renamed or removed GatewayRunner._session_model_overrides; '
        'plugin\'s runtime_admin endpoints (/myah/v1/admin/sessions/{id}/override) '
        'and MyahAdapter._handle_message_endpoint will silently fail at runtime. '
        'See spec §3.2.1.'
    )


def test_agent_cache_attrs_exist():
    """GatewayRunner exposes _agent_cache + _agent_cache_lock.

    Used by:
    - /myah/v1/admin/cache/evict-all
    - /myah/v1/admin/cache/evict/{session_key}
    - MyahAdapter cached-agent attribution lookup
    """
    from gateway.run import GatewayRunner

    init_src = inspect.getsource(GatewayRunner.__init__)
    assert '_agent_cache' in init_src, (
        'Upstream renamed or removed GatewayRunner._agent_cache. '
        'Cache eviction and attribution lookups will silently fail. '
        'See spec §3.2.1.'
    )
    assert '_agent_cache_lock' in init_src, (
        'Upstream renamed or removed GatewayRunner._agent_cache_lock. '
        'Concurrent cache reads will be unsafe. See spec §3.2.1.'
    )


def test_running_agents_attr_exists():
    """GatewayRunner exposes _running_agents dict.

    Used by:
    - /myah/v1/admin/sessions/active
    - /myah/v1/admin/gateway/restart (busy-check)
    """
    from gateway.run import GatewayRunner

    init_src = inspect.getsource(GatewayRunner.__init__)
    assert '_running_agents' in init_src, (
        'Upstream renamed or removed GatewayRunner._running_agents. '
        'Active-sessions list and gateway-restart busy-check will silently fail. '
        'See spec §3.2.1.'
    )


def test_evict_cached_agent_method_exists():
    """GatewayRunner exposes _evict_cached_agent(session_key) method.

    Used by:
    - set_session_override_direct (after writing the override)
    - evict_session_agent_direct (the public eviction path)
    """
    from gateway.run import GatewayRunner

    assert hasattr(GatewayRunner, '_evict_cached_agent'), (
        'Upstream renamed or removed GatewayRunner._evict_cached_agent. '
        'Cache eviction will silently fail. See spec §3.2.1.'
    )
    method = GatewayRunner._evict_cached_agent
    assert callable(method), (
        'GatewayRunner._evict_cached_agent is not callable; '
        'upstream may have changed it from a method to an attribute. '
        'See spec §3.2.1.'
    )
