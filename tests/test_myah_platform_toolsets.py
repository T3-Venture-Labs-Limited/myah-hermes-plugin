"""Regression tests for the Myah platform's toolset wiring.

Phase 4d (2026-05-04) moved these tests out of
``tests/hermes_cli/test_tools_config.py::TestMyahPlatformSecrets`` because
they exercise ``_get_platform_tools("myah")``, which depends on the Myah
``PlatformEntry`` being present in the gateway's platform registry — and
that registration only happens when ``myah-hermes-plugin``'s
``register(ctx)`` runs. Core test runs without the plugin installed
should not be expected to resolve the ``"myah"`` key.

The plugin's ``conftest.py`` registers the entry session-scoped, so the
registry-aware fall-through in ``hermes_cli.tools_config.PLATFORMS``
resolves ``PLATFORMS["myah"]["default_toolset"]`` to ``"hermes-myah"``
just like it would in a real plugin-loaded run.
"""

from __future__ import annotations

from hermes_cli.tools_config import _get_platform_tools


# ── Myah: secrets toolset regression coverage ───────────────────────────────


class TestMyahPlatformSecrets:
    """Regression: secrets toolset must be exposed to the Myah platform.

    Previously the secrets tool was registered but missing from
    ``CONFIGURABLE_TOOLSETS`` and the ``TOOLSETS`` dict, so
    ``_get_platform_tools`` never included it. Agents on the Myah platform
    couldn't see the secrets tool in their schema and would tell users
    "there is no secrets tool" when asked to use it.
    """

    def test_secrets_in_myah_default_toolsets(self) -> None:
        """Default (no explicit platform_toolsets) — secrets must be enabled."""
        config: dict = {}
        enabled = _get_platform_tools(config, "myah")
        assert "secrets" in enabled, (
            f"secrets toolset missing from myah default platform tools: "
            f"{sorted(enabled)}"
        )

    def test_secrets_in_myah_with_explicit_config(self) -> None:
        """Explicit platform_toolsets config containing secrets — must be enabled."""
        config = {"platform_toolsets": {"myah": ["secrets", "terminal"]}}
        enabled = _get_platform_tools(config, "myah")
        assert "secrets" in enabled

    def test_secrets_excluded_when_explicitly_omitted(self) -> None:
        """Explicit opt-out: user previously had secrets enabled (recorded in
        known_plugin_toolsets), now removes it from platform_toolsets — must
        be excluded.

        Tier 2C Issue 1 (2026-05-08): secrets is plugin-derived, so the
        opt-out path requires known_plugin_toolsets to mark the platform as
        having previously seen the toolset. Without that hint, plugin
        toolsets are 'default-on for new platforms' and the explicit
        platform_toolsets list is treated as additive, not authoritative.
        """
        config = {
            "platform_toolsets": {"myah": ["terminal"]},
            "known_plugin_toolsets": {"myah": ["secrets"]},
        }
        enabled = _get_platform_tools(config, "myah")
        assert "secrets" not in enabled
        assert "terminal" in enabled
