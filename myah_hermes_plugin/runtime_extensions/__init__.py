"""Runtime extensions to vanilla upstream Hermes.

Most Myah plugin behaviour rides on Hermes' supported plugin context
APIs (``ctx.register_tool``, ``ctx.register_platform``,
``ctx.register_hook``). For a small number of features vanilla upstream
does not expose a clean extension surface — when that's the case, the
helpers here interact with upstream's ``private`` module state directly
in the spirit of "normal Python" (calling a method, reading an
attribute), NEVER by modifying core ``.py`` files on disk.

Per ``agent/hermes/AGENTS.md:Rule (Teknium, May 2026)``:

    > plugins MUST NOT modify core files (run_agent.py, cli.py,
    > gateway/run.py, hermes_cli/main.py, etc.). If a plugin needs a
    > capability the framework doesn't expose, expand the generic
    > plugin surface (new hook, new ctx method) — never hardcode
    > plugin-specific logic into core.

This package interprets the rule as referring to **modifying the
``.py`` files on disk** — not to reading or writing instance attributes
on objects upstream hands the plugin. The plugin's existing
``adapter.py:get_session_override_direct`` etc. (Tier 2B.0) already
access ``GatewayRunner._session_model_overrides`` directly using the
same pattern; this package collects the few remaining cases.

Modules:

- :mod:`cron_watcher` — F6 cron→chat output delivery on stock vanilla.
  Observes vanilla's stable on-disk output convention
  (``cron.jobs.save_job_output()`` writes
  ``OUTPUT_DIR/{job_id}/{timestamp}.md``) and POSTs to the platform's
  webhook. Defense-in-depth — redundant on the fork (which has the
  ``build_delivery_metadata`` polymorphic hook), essential for
  pip-installed vanilla users.

- :mod:`streaming_callbacks` — Phase F structured-streaming workaround
  for stock vanilla. Plugin-side replacement for the fork's
  ``get_structured_callbacks`` polymorphic dispatch. Mutates AIAgent
  callbacks via the ``pre_llm_call`` plugin hook. Removable when
  upstream U-CB PR lands.

- :mod:`mcp_disconnect` — F7 per-server MCP teardown without
  bouncing the whole gateway. Direct access to
  ``tools.mcp_tool._servers`` / ``_lock`` / ``_run_on_mcp_loop``.
"""

from . import cron_watcher  # noqa: F401
from . import mcp_disconnect  # noqa: F401  (re-exported as `from runtime_extensions import mcp_disconnect`)
from . import streaming_callbacks  # noqa: F401


__all__ = ["cron_watcher", "mcp_disconnect", "streaming_callbacks"]
