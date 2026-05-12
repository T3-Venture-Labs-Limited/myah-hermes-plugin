"""Smoke tests: the moved dashboard FastAPI router still imports + mounts
under the new pip-package layout (Phase 4e — Approach A).

Two load contexts are exercised:

1. **Direct package import** (``from myah_hermes_plugin.myah_admin.dashboard
   import plugin_api``) — what the materialized shim resolves to and what
   the migrated tests use.
2. **Filesystem-discovery shim load** — simulates Hermes' dashboard
   loader by running the install CLI into a tempdir and loading the
   resulting ``plugin_api.py`` via ``spec_from_file_location`` (the same
   path ``hermes_cli/web_server.py:_mount_plugin_api_routes`` uses).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


# ── Direct package import ───────────────────────────────────────────────────


def test_top_level_router_exists():
    """``plugin_api`` exports a ``router`` attribute."""
    from myah_hermes_plugin.myah_admin.dashboard import plugin_api

    assert hasattr(plugin_api, 'router')
    assert hasattr(plugin_api.router, 'routes')


def test_health_route_present():
    """The /health route added by ``plugin_api`` itself is present (the
    canonical reachability probe used by the platform's
    ``/api/v1/containers/{user_id}/web-health`` endpoint)."""
    from myah_hermes_plugin.myah_admin.dashboard import plugin_api

    paths = {r.path for r in plugin_api.router.routes if hasattr(r, 'path')}
    assert '/health' in paths


def test_sub_routers_imported():
    """All five sub-router modules import without error."""
    from myah_hermes_plugin.myah_admin.dashboard import (  # noqa: F401
        _common,
        _providers,
        _sessions_and_lifecycle,
        _skills_plugins_mcp,
        _soul_and_config,
    )


def test_sentry_hook_reexport():
    """``myah_admin.__init__`` re-exports ``register_sentry_hook`` for
    callers that previously imported it as
    ``plugins.myah_admin.myah_hook.register_sentry_hook``."""
    from myah_hermes_plugin.myah_admin import register_sentry_hook

    assert callable(register_sentry_hook)


# ── Filesystem-discovery shim load ──────────────────────────────────────────


def test_materialized_shim_is_filesystem_loadable(tmp_path: Path):
    """The materialized ``plugin_api.py`` shim loads under
    ``importlib.util.spec_from_file_location`` (the path Hermes' dashboard
    loader uses) and resolves to the real router from the pip package.

    This catches regressions where the shim accidentally grows a relative
    import — those work in package-aware contexts (tests, REPL) but fail
    silently for the production dashboard loader.
    """
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
    spec = importlib.util.spec_from_file_location(
        'hermes_dashboard_plugin_myah-admin',
        shim_path,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # The shim re-exports ``router`` from the pip-package module.
    assert hasattr(mod, 'router')
    paths = {r.path for r in mod.router.routes if hasattr(r, 'path')}
    assert '/health' in paths
