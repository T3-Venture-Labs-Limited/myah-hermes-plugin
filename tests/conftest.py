"""Test fixtures for myah-hermes-plugin.

Sets up an isolated HERMES_HOME per test so secrets_tool's reads/writes
to ~/.hermes/.env are scoped to a tempdir.

The hermes-fork's top-level tests/conftest.py provides the same fixture
(plus credential blanking, locale pinning, etc.) but its scope is the
tests/ subtree. When this plugin is run on its own (e.g. `pytest
plugins/myah-hermes-plugin/tests/`), this conftest takes over.

Also registers the Myah platform with the gateway platform registry at
session start so tests that go through ``GatewayRunner._run_agent`` (and
thus ``_get_platform_tools``) can resolve ``"myah"`` without booting the
full plugin discovery machinery.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to a per-test tempdir."""
    fake_hermes_home = tmp_path / "hermes_test"
    fake_hermes_home.mkdir()
    (fake_hermes_home / "sessions").mkdir()
    (fake_hermes_home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_hermes_home))
    # Blank any credential vars that could leak into _is_secret_like_name
    # checks via os.environ inspection.
    for name in list(os.environ.keys()):
        upper = name.upper()
        if upper.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
            monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True, scope="session")
def _register_myah_platform_for_tests():
    """Register the Myah platform AND invoke the plugin's ``register(ctx)``.

    Phase 4d (2026-05-04): the platform is registered at runtime by the
    plugin's ``register(ctx)`` callback, but tests don't run the full
    plugin discovery cycle. Registering here makes the registry-aware
    fall-through paths in core (e.g. ``_get_platform_tools``,
    ``_is_user_authorized``) see Myah just like they would in production.

    Tier 2C Issue 1 (2026-05-08): the secrets toolset is now exposed via
    plugin auto-derivation rather than a hardcoded ``CONFIGURABLE_TOOLSETS``
    entry. The hardcoded fallback ``platform_registry.register(...)`` below
    is kept as a safety net for tests that touch only the platform registry
    without going through ``_get_platform_tools``. We then construct a real
    :class:`PluginContext` against the global ``PluginManager`` and invoke
    the plugin's ``register(ctx)`` so ``_plugin_tool_names`` is populated
    with ``"secrets"`` and ``get_plugin_toolsets()`` auto-derives the
    ``"secrets"`` entry that ``_get_platform_tools(config={}, "myah")``
    expects in the default-config branch. The Hermes plugin loader gates
    standalone plugins on ``plugins.enabled``, which is unset in the
    per-test ``HERMES_HOME`` tempdir — without this fixture extension the
    plugin would load but ``register(ctx)`` would never run.
    """
    from gateway.platform_registry import PlatformEntry, platform_registry
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from tools.registry import registry as tool_registry

    # Safety-net platform registration (preserves prior behavior for tests
    # that don't need the full plugin context).
    platform_registry.register(
        PlatformEntry(
            name="myah",
            label="🌐 Myah",
            adapter_factory=lambda cfg: None,  # tests construct adapters directly
            check_fn=lambda: True,
            allowed_users_env="MYAH_ALLOWED_USERS",
            allow_all_env="MYAH_ALLOW_ALL_USERS",
            source="plugin",
        )
    )

    # Drive the plugin's register(ctx) against a real PluginContext so the
    # tool registry, plugin-tool-name set, and gateway hooks all see the
    # same effects production sees.
    manifest = PluginManifest(
        name="myah-platform",
        description="Myah web platform plugin (test-fixture-loaded)",
        source="entrypoint",
        path="myah_hermes_plugin.myah_platform",
        kind="standalone",
        key="myah-platform",
    )
    manager = get_plugin_manager()
    ctx = PluginContext(manifest=manifest, manager=manager)

    from myah_hermes_plugin.myah_platform import register as plugin_register

    plugin_register(ctx)

    yield

    # Tear down everything we registered. ``deregister`` on the tool registry
    # cleans up both the tool entry and the toolset's check_fn when no other
    # tools share the toolset; ``_plugin_tool_names`` must be cleaned manually.
    tool_registry.deregister("secrets")
    manager._plugin_tool_names.discard("secrets")
    platform_registry.unregister("myah")
