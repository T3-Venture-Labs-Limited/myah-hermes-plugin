"""Tests for the ``myah-hermes-plugin install --dashboard-only`` console script.

The CLI materializes a discovery hook (manifest.json + a one-import shim
``plugin_api.py``) at ``<target>/myah-admin/dashboard/`` so Hermes'
filesystem dashboard scanner can find the plugin. The actual router code
lives only in the pip-installed package (Phase 4e Approach A — see
``myah_hermes_plugin/cli.py`` for rationale).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_install_dashboard_only_creates_manifest(tmp_path: Path):
    """``install --dashboard-only --target <dir>`` produces
    ``<dir>/myah-admin/dashboard/manifest.json`` with the expected name field."""
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'myah_hermes_plugin.cli',
            'install',
            '--dashboard-only',
            '--target',
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f'CLI failed: {result.stderr}'

    manifest_path = tmp_path / 'myah-admin' / 'dashboard' / 'manifest.json'
    assert manifest_path.exists(), f'manifest.json not at {manifest_path}'

    manifest = json.loads(manifest_path.read_text())
    assert manifest['name'] == 'myah-admin'
    assert manifest['api'] == 'plugin_api.py'


def test_install_dashboard_only_writes_shim(tmp_path: Path):
    """The materialized ``plugin_api.py`` is the absolute-import shim — NOT
    a copy of the real module. The shim has no relative imports (which would
    fail under ``spec_from_file_location`` load) and re-exports ``router``."""
    subprocess.run(
        [
            sys.executable,
            '-m',
            'myah_hermes_plugin.cli',
            'install',
            '--dashboard-only',
            '--target',
            str(tmp_path),
        ],
        check=True,
    )

    shim_path = tmp_path / 'myah-admin' / 'dashboard' / 'plugin_api.py'
    assert shim_path.exists()
    contents = shim_path.read_text()

    # The shim must use an absolute import (the dashboard loader gives
    # the loaded module no parent-package context).
    assert 'from myah_hermes_plugin.myah_admin.dashboard.plugin_api import router' in contents

    # No relative imports — those would fail at load time.
    assert 'from .' not in contents


def test_install_dashboard_only_does_not_copy_sub_routers(tmp_path: Path):
    """Approach A: only manifest.json + plugin_api.py shim are materialized.
    The sub-router files (_common, _providers, _sessions_and_lifecycle,
    _skills_plugins_mcp, _soul_and_config) live exclusively inside the pip
    package and must not be duplicated to the materialized location."""
    subprocess.run(
        [
            sys.executable,
            '-m',
            'myah_hermes_plugin.cli',
            'install',
            '--dashboard-only',
            '--target',
            str(tmp_path),
        ],
        check=True,
    )

    dashboard_dir = tmp_path / 'myah-admin' / 'dashboard'
    materialized = {p.name for p in dashboard_dir.iterdir() if not p.name.startswith('__')}
    assert materialized == {'manifest.json', 'plugin_api.py'}


def test_install_requires_dashboard_only_flag(tmp_path: Path):
    """``install`` without --dashboard-only currently errors (forward-compat
    reservation for tools/skills install variants)."""
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'myah_hermes_plugin.cli',
            'install',
            '--target',
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    # Either explicit error message or argparse rejection — both prove the
    # forward-compat reservation is active.
    combined = (result.stderr + result.stdout).lower()
    assert 'dashboard' in combined


def test_install_idempotent(tmp_path: Path):
    """Running install twice in a row succeeds — the CLI overwrites both
    files cleanly. This is what the Dockerfile relies on if a build re-runs
    the RUN step (cache miss) without rebuilding the layer."""
    cmd = [
        sys.executable,
        '-m',
        'myah_hermes_plugin.cli',
        'install',
        '--dashboard-only',
        '--target',
        str(tmp_path),
    ]
    subprocess.run(cmd, check=True)
    subprocess.run(cmd, check=True)

    assert (tmp_path / 'myah-admin' / 'dashboard' / 'manifest.json').exists()
    assert (tmp_path / 'myah-admin' / 'dashboard' / 'plugin_api.py').exists()
