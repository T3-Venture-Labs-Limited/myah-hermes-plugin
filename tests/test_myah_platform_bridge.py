"""Verify plugin's register(ctx) bridges 'myah' into tools_config.PLATFORMS.

This is a workaround test for Tier 2C Issue 2. The plugin mutates
hermes_cli.tools_config.PLATFORMS at register time so upstream code
paths that do PLATFORMS["myah"] direct lookup find the platform.

Two tests:
  1. The bridge installs the entry (positive — confirms the workaround works).
  2. tools_config.PLATFORMS is still a plain dict (sentinel — fails LOUDLY
     if upstream changes PLATFORMS to a derived view, signaling the bridge
     has silently stopped working and U-PLAT needs to be filed).

The conftest.py session-scoped fixture invokes the plugin's register(ctx)
at session start, so the bridge has already executed by the time these
tests run.
"""

from __future__ import annotations


def test_register_bridges_into_tools_config_platforms() -> None:
    """Plugin register(ctx) must add 'myah' to tools_config.PLATFORMS."""
    import hermes_cli.tools_config as tc
    assert "myah" in tc.PLATFORMS, (
        f"plugin did not bridge 'myah' into tools_config.PLATFORMS; "
        f"got keys: {sorted(tc.PLATFORMS.keys())}"
    )
    entry = tc.PLATFORMS["myah"]
    assert entry["label"] == "Myah"
    assert entry["default_toolset"] == "hermes-myah"


def test_platforms_is_still_a_plain_dict() -> None:
    """Sentinel: if upstream changes PLATFORMS to a derived view, the
    bridge in register() silently stops working. Catches that."""
    import hermes_cli.tools_config as tc
    assert isinstance(tc.PLATFORMS, dict), (
        f"PLATFORMS changed type to {type(tc.PLATFORMS).__name__}; "
        "the plugin's runtime mutation will silently no-op. File U-PLAT."
    )
