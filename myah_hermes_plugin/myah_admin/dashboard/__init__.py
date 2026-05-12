"""Dashboard plugin sub-package.

Sub-routers (``_common``, ``_providers``, ``_sessions_and_lifecycle``,
``_skills_plugins_mcp``, ``_soul_and_config``) are loaded as proper
Python package members from here, which lets them use clean relative
imports (``from ._common import require_session_token``).

The top-level :mod:`plugin_api` module composes them into a single
FastAPI ``APIRouter`` that the dashboard plugin loader mounts at
``/api/plugins/myah-admin/``.
"""
