"""Plugin contract tests for the Myah platform.

Phase 4d (2026-05-04) moved the Myah platform adapter out of upstream
Hermes core and into ``myah-hermes-plugin``. The previous ADDING_A_PLATFORM
contract — which asserted that Myah was hardcoded into the core ``Platform``
enum, ``PLATFORM_HINTS``, ``cron/scheduler.py``, ``_create_adapter`` etc. —
no longer applies; those assertions would *fail by design* in the new world.

These tests instead assert the new contract: when the plugin's
``register(ctx)`` is invoked, it correctly registers a platform named
``"myah"`` with the expected capability fields on its ``PlatformEntry``,
and the adapter is importable from the plugin namespace.
"""

from __future__ import annotations

from typing import Any

import pytest


# ── Plugin entry point + register() invocation ────────────────────────────


class _RecordingContext:
    """Minimal PluginContext stand-in capturing every register_* call."""

    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.platforms: list[dict] = []
        self.hooks: list[tuple[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_platform(self, **kwargs: Any) -> None:
        self.platforms.append(kwargs)

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))


@pytest.fixture
def captured_registration() -> _RecordingContext:
    """Run the plugin's register(ctx) and capture what it registered."""
    from myah_hermes_plugin.myah_platform import register

    ctx = _RecordingContext()
    register(ctx)
    return ctx


# ── Platform registration contract (replaces the old ADDING_A_PLATFORM
# ── checklist for Myah) ────────────────────────────────────────────────


class TestPluginRegistersMyahPlatform:
    """Phase 4d invariants on ``ctx.register_platform`` for Myah."""

    def test_platform_registered(self, captured_registration: _RecordingContext) -> None:
        """register() must call ctx.register_platform exactly once for Myah."""
        myah_entries = [p for p in captured_registration.platforms if p.get("name") == "myah"]
        assert len(myah_entries) == 1, (
            "myah-hermes-plugin's register() must call ctx.register_platform "
            "exactly once for the Myah platform."
        )

    def test_platform_label(self, captured_registration: _RecordingContext) -> None:
        entry = next(p for p in captured_registration.platforms if p.get("name") == "myah")
        assert entry.get("label") == "🌐 Myah"

    def test_allowed_users_env(self, captured_registration: _RecordingContext) -> None:
        """Replaces the hardcoded MYAH_ALLOWED_USERS entry in core's platform_env_map."""
        entry = next(p for p in captured_registration.platforms if p.get("name") == "myah")
        assert entry.get("allowed_users_env") == "MYAH_ALLOWED_USERS"

    def test_allow_all_env(self, captured_registration: _RecordingContext) -> None:
        """Replaces the hardcoded MYAH_ALLOW_ALL_USERS entry in core's platform_allow_all_map."""
        entry = next(p for p in captured_registration.platforms if p.get("name") == "myah")
        assert entry.get("allow_all_env") == "MYAH_ALLOW_ALL_USERS"

    def test_platform_hint_present(self, captured_registration: _RecordingContext) -> None:
        """Replaces the 'myah' entry deleted from PLATFORM_HINTS in prompt_builder."""
        entry = next(p for p in captured_registration.platforms if p.get("name") == "myah")
        hint = entry.get("platform_hint")
        assert isinstance(hint, str) and "Myah" in hint

    def test_check_fn_returns_bool(self, captured_registration: _RecordingContext) -> None:
        """check_fn must be callable and return a bool (aiohttp availability)."""
        entry = next(p for p in captured_registration.platforms if p.get("name") == "myah")
        check_fn = entry.get("check_fn")
        assert callable(check_fn)
        result = check_fn()
        assert isinstance(result, bool)

    def test_cron_deliver_env_var_is_myah_home_chat(
        self, captured_registration: _RecordingContext
    ) -> None:
        """B1 regression — pin the env var name that suppresses the gateway warning.

        Upstream's gateway/run.py:6802 (at submodule SHA 87b22d309) calls
        _home_target_env_var('myah'), which goes through
        cron/scheduler.py:_resolve_home_env_var() → looks up the plugin's
        PlatformEntry and returns its cron_deliver_env_var. The platform side
        of the warning suppression (containers.py per-user container env +
        entrypoint.sh's seed of /data/.hermes/.env) sets MYAH_HOME_CHAT=disabled,
        which only works if cron_deliver_env_var stays equal to "MYAH_HOME_CHAT".

        If this assertion ever fails, the platform-side companion test
        test_container_env_includes_myah_home_chat_disabled in
        platform-oss/backend/myah/test/apps/myah/routers/test_containers.py
        must be updated in lockstep, otherwise the "📬 No home channel is set"
        warning will resurface on every fresh chat in production.
        """
        entry = next(p for p in captured_registration.platforms if p.get("name") == "myah")
        assert entry.get("cron_deliver_env_var") == "MYAH_HOME_CHAT", (
            "PlatformEntry.cron_deliver_env_var for 'myah' must stay "
            "'MYAH_HOME_CHAT' — this is the env var name the gateway warning "
            "check resolves to via _resolve_home_env_var(). The platform "
            "(containers.py + entrypoint.sh) sets MYAH_HOME_CHAT=disabled "
            "to suppress the warning; changing the name here without "
            "updating the platform companions resurrects the B1 bug."
        )

    def test_adapter_factory_constructible(
        self, captured_registration: _RecordingContext
    ) -> None:
        """adapter_factory must produce a MyahAdapter instance from a PlatformConfig.

        Tier 2A Task 2A.3 (see ``myah_platform/standalone_runner.py``)
        retired the adapter's dependency on
        ``gateway.platforms.api_server.register_pre_setup_hook``: the
        adapter now owns its own aiohttp ``AppRunner`` via
        ``MyahStandaloneRunner``. The factory call therefore has no
        side effects on upstream's api_server module and no patching
        is required.
        """
        from gateway.config import PlatformConfig

        entry = next(p for p in captured_registration.platforms if p.get("name") == "myah")
        factory = entry.get("adapter_factory")
        assert callable(factory)

        adapter = factory(PlatformConfig(enabled=True, extra={}))
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter

        assert isinstance(adapter, MyahAdapter)


# ── Importability contract ─────────────────────────────────────────────────


class TestImportability:
    """The adapter and runtime_admin must be importable from the plugin namespace."""

    def test_adapter_importable_from_plugin(self) -> None:
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter

        assert MyahAdapter is not None

    def test_runtime_admin_importable_from_plugin(self) -> None:
        from myah_hermes_plugin.myah_platform.runtime_admin import (
            register_runtime_admin_routes,
        )

        assert callable(register_runtime_admin_routes)

    def test_adapter_NOT_importable_from_old_core_path(self) -> None:
        """gateway.platforms.myah must no longer exist in core."""
        with pytest.raises(ImportError):
            __import__("gateway.platforms.myah", fromlist=["MyahAdapter"])


# ── Removed: upstream-toolset-preset and cronjob-deliver-schema tests ─────
#
# Two contract tests previously enforced invariants on upstream Hermes
# core that no longer hold at the pinned ``HERMES_SHA``:
#
# * ``test_hermes_myah_toolset_preset_exists`` — asserted that upstream's
#   ``toolsets.TOOLSETS`` dict contained a ``"hermes-myah"`` preset. Its
#   own docstring flagged Phase 4f as the point at which the preset
#   would migrate to the plugin; that move has happened upstream, so the
#   preset is gone from core. The plugin's own ``default_toolset =
#   "hermes-myah"`` wiring (via ``hermes_cli.tools_config.PLATFORMS``)
#   is asserted from the right angle by
#   ``test_myah_platform_bridge.py::test_register_bridges_into_tools_config_platforms``.
#
# * ``test_myah_in_cronjob_deliver_schema`` — read upstream's
#   ``tools/cronjob_tools.py`` via ``Path(__file__).resolve().parents[3]``
#   and asserted ``"myah"`` appeared in the deliver-target description.
#   Two things broke: upstream no longer mentions myah in
#   ``cronjob_tools.py`` (the platform-specific guidance moved out of
#   the generic deliver-target docs), and in Mode D the plugin source
#   lives at ``/opt/myah-plugin-source`` so ``parents[3]`` resolves to
#   ``/`` rather than the hermes checkout. The test failed with
#   ``FileNotFoundError`` regardless of upstream content.
#
# Both tests were enforcing the wrong contract from the wrong location;
# deleting them removes the false signal without losing any real
# protection. The model's understanding of the ``myah:<chat>:<thread>``
# deliver-target shape is now seeded from the plugin's own platform
# entry rather than from upstream cron schema strings.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
