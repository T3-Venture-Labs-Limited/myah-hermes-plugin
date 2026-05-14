"""Verify the plugin works under hermes plugins install (directory-style).

Phase 0 Task 0.4b confirmed that `hermes plugins install owner/repo` does NOT
pip-install — it git-clones + reads plugin.yaml + moves to
~/.hermes/plugins/<name>/. The plugin loader then requires __init__.py at the
cloned-dir root that exports a register(ctx) function.

This test enforces those layout invariants so the canonical install command
in the OSS launch README (`hermes plugins install T3-Venture-Labs-Limited/myah-hermes-plugin`)
is truthful.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent  # myah-hermes-plugin/


def test_plugin_yaml_exists_at_root() -> None:
    """hermes plugins install reads plugin.yaml at the cloned-dir root.

    Without this file, the plugin loader skips the dir and the install is a
    silent no-op (see plugins_cmd.py:383-388 + plugins.py:826 in the pinned
    Hermes SHA).
    """
    manifest = PLUGIN_ROOT / 'plugin.yaml'
    assert manifest.exists(), (
        'plugin.yaml MUST exist at the repo root for hermes plugins install '
        'to recognize this as a plugin'
    )


def test_root_init_exists() -> None:
    """The plugin loader requires __init__.py at the cloned-dir root.

    plugins.py:1038-1040 raises FileNotFoundError if absent.
    """
    init_path = PLUGIN_ROOT / '__init__.py'
    assert init_path.exists(), 'Root __init__.py required by hermes plugin loader'


def test_root_init_defines_register_function() -> None:
    """The root __init__.py must define a top-level `register` callable.

    Structural-AST check (does not actually exec the import chain — that
    requires hermes-agent to be installed alongside the plugin, which is
    only true in Mode D tests run inside the agent stock image).

    The execution check lives in test_root_init_exec_loads_register below,
    guarded by importorskip on a hermes-agent symbol.
    """
    import ast

    init_path = PLUGIN_ROOT / '__init__.py'
    tree = ast.parse(init_path.read_text())

    # Walk top-level statements for a function named `register`
    register_funcs = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'register'
    ]
    assert register_funcs, (
        'The root __init__.py must define a top-level `register(ctx)` function. '
        'The hermes plugin loader does `module.register(ctx)` after importing '
        '__init__.py via spec_from_file_location.'
    )

    # Sanity: takes exactly one positional arg (the ctx)
    register_fn = register_funcs[0]
    args = register_fn.args.args
    assert len(args) == 1, (
        f'register() should take exactly one positional arg (ctx); '
        f'found {len(args)}.'
    )


def test_root_init_exec_loads_register() -> None:
    """End-to-end load: imports the root __init__.py and verifies register exists.

    This test requires the hermes-agent package to be installed alongside the
    plugin (otherwise the `from myah_hermes_plugin.myah_platform import register`
    chain pulls in `tools.skills_tool` etc which depend on hermes-agent internals).
    Skipped gracefully when hermes-agent is absent.
    """
    pytest.importorskip(
        'tools',
        reason='hermes-agent not installed — exec test only runs in Mode D / agent image',
    )

    init_path = PLUGIN_ROOT / '__init__.py'
    spec = importlib.util.spec_from_file_location('myah_root_under_test', init_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    plugin_root_str = str(PLUGIN_ROOT)
    sys.path.insert(0, plugin_root_str)
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(plugin_root_str)
        except ValueError:
            pass

    assert hasattr(module, 'register'), '__init__.py must export `register(ctx)`'
    assert callable(module.register), '`register` must be callable'


def test_plugin_yaml_name_field_is_myah() -> None:
    """hermes uses plugin.yaml's `name:` for enable/disable commands. Must be `myah`."""
    import yaml

    with (PLUGIN_ROOT / 'plugin.yaml').open() as f:
        manifest = yaml.safe_load(f)

    assert manifest.get('name') == 'myah', (
        f"plugin.yaml `name:` must be 'myah' (currently: {manifest.get('name')!r})"
    )


def test_plugin_yaml_manifest_version_supported() -> None:
    """Must declare manifest_version: 1 (the upstream-supported version).

    Higher values cause hermes plugins install to abort with a "this
    installer only supports up to N" error (plugins_cmd.py:364-371).
    """
    import yaml

    with (PLUGIN_ROOT / 'plugin.yaml').open() as f:
        manifest = yaml.safe_load(f)

    assert manifest.get('manifest_version') == 1


def test_pyproject_version_matches_plugin_yaml_version() -> None:
    """plugin.yaml `version` and pyproject `version` must stay in sync.

    Drift is invisible at install time (hermes doesn't validate version) but
    confuses users who see different numbers between `hermes plugins list`
    and `pip show myah-hermes-plugin`.
    """
    try:
        import tomllib
    except ImportError:
        # Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]
    import yaml

    with (PLUGIN_ROOT / 'pyproject.toml').open('rb') as f:
        pyproject = tomllib.load(f)
    with (PLUGIN_ROOT / 'plugin.yaml').open() as f:
        manifest = yaml.safe_load(f)

    pyproject_version = pyproject['project']['version']
    manifest_version = manifest['version']

    assert pyproject_version == manifest_version, (
        f'Version drift between pyproject and plugin.yaml: '
        f'pyproject={pyproject_version!r}, plugin.yaml={manifest_version!r}'
    )


def test_plugin_yaml_has_minimal_required_fields() -> None:
    """plugin.yaml should have description, author, repository for marketplace UX.

    Upstream hermes plugins list and the marketplace UI surface these fields.
    """
    import yaml

    with (PLUGIN_ROOT / 'plugin.yaml').open() as f:
        manifest = yaml.safe_load(f)

    for field in ('description', 'author', 'repository'):
        value = manifest.get(field)
        assert value, f'plugin.yaml is missing field {field!r}'
