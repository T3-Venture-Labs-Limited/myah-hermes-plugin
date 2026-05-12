"""Myah admin dashboard plugin (Phase 4e migration).

Houses the FastAPI dashboard plugin (filesystem-discovered by Hermes'
``hermes_cli/web_server.py:_discover_dashboard_plugins``) plus the Sentry
telemetry-hook adapter.

The actual dashboard router lives at
``myah_hermes_plugin.myah_admin.dashboard.plugin_api`` and is imported by
the materialized ``/opt/myah/plugins/myah-admin/dashboard/plugin_api.py``
shim that the ``myah-hermes-plugin install --dashboard-only`` console
script writes at image-build time.

Re-exports ``register_sentry_hook`` for backwards compatibility — the
hook used to live at ``plugins/myah-admin/myah_hook.py`` and was
imported as ``plugins.myah_admin.myah_hook``.
"""

from .myah_hook import SentryHook, register_sentry_hook

__all__ = ['register_sentry_hook', 'SentryHook']
