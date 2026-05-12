# Changelog

All notable changes to `myah-hermes-plugin` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Known limitations (all releases)

- **F5 — BOOT.md startup hook**: not supported on stock vanilla upstream.
  Vanilla's `hermes_cli/plugins.py:VALID_HOOKS` does not expose a
  `gateway:startup` event. The fork-only `gateway/builtin_hooks/boot_md.py`
  registers for that event to run a one-shot agent with `BOOT.md` as
  the prompt. There is no semantically equivalent vanilla hook (the
  closest, `on_session_start`, fires per-session not at-boot — that
  would re-inject the preamble every chat, breaking prompt cache).
  OSS users on stock-vanilla + plugin do **NOT** get BOOT.md. The
  hosted deployment carries it via the fork-bundled `boot_md.py`.
  This will return once upstream merges a `register_gateway_event_hook`
  surface (tracked as upstream PR `U-HOOK`).

  **Workaround for OSS users who need BOOT.md today:** schedule a
  cron job at `@reboot` (or equivalent) that runs `hermes` with the
  preamble as the prompt; or contribute the upstream PR.

- **F6 — Cron→Myah delivery enrichment on stock vanilla**: vanilla
  `cron/scheduler.py:_deliver_result` does NOT call the polymorphic
  `runtime_adapter.build_delivery_metadata()` hook the fork carries
  (Tier 2B Task 2B.4). Result: cron jobs fire on stock vanilla, the
  agent runs and writes output to `~/.hermes/cron/output/`, but the
  plugin's adapter does not receive the enriched metadata
  (`job_id`, `job_name`, `status`, `ran_at`) needed to route the
  result through the platform's `/api/v1/processes/webhook/run-complete`
  handler.

  **Impact on OSS users:** cron jobs run successfully and persist
  their output to disk, but they do not appear back in the Myah chat
  history automatically. Users can read cron output via
  `hermes cron list` and the dashboard's cron pane.

  **Two paths to resolution (not shipped in v1.1.0 — needs design
  approval):**
  1. **Upstream PR** adding the polymorphic call to vanilla
     `cron/scheduler.py:_deliver_result` (queued as `U-CRON` in the
     spec; same diff as Tier 2B Task 2B.4).
  2. **Plugin-side cron output watcher** that polls
     `~/.hermes/cron/output/` and posts to the platform webhook
     directly. ~150 LOC, no monkey-patch, no upstream PR required.

  The hosted Myah deployment uses the fork build with Tier 2B's
  polymorphic hook, so cron deliveries land in chat normally there.

- **OSS multi-tenant**: the plugin assumes single-tenant per process.
  The OSS `/api/v1/myah/whoami` endpoint resolves to the FIRST
  registered user. Multi-user OSS deployments require additional auth
  wiring not shipped in v1.

## Upstream private-API dependencies

The plugin reads several private (underscore-prefixed) attributes from
upstream `hermes-agent` modules. Each is wrapped in a defensive `getattr`
chain and covered by a CI guard test that fails loudly at plugin-CI time
if upstream drops the attribute. The guards are the canary — if one
goes red on a submodule bump, investigate before merging.

| Upstream symbol | Module | Used by | CI guard |
|---|---|---|---|
| `_gateway_runner_ref` (`weakref.ref`) | `gateway/run.py` | `MyahAdapter._resolve_runner` (lazy runner self-discovery for plugin-registered platforms); Phase F `myah_pre_llm_call` (resolves the active `GatewayRunner` from outside `_run_agent`'s closure). | `tests/test_streaming_callbacks.py::test_gateway_runner_ref_is_module_level_weakref` |
| `_agent_cache` (`Dict[session_key, (AIAgent, sig)]`) | `gateway.run.GatewayRunner` | Phase F `myah_pre_llm_call` (looks up the cached agent to swap its callback attributes before the LLM call). | `tests/test_streaming_callbacks.py::test_runner_agent_cache_attr_exists` |
| `_session_model_overrides` (`Dict[session_key, dict]`) | `gateway.run.GatewayRunner` | `runtime_extensions/_runner_state.py` (Tier 2B established pattern for per-session model/provider override). | Covered by `tests/test_runner_state.py`. |
| `_servers` / `_lock` / `_run_on_mcp_loop` | `tools/mcp_tool` | `runtime_extensions/mcp_disconnect.disconnect_mcp_server` (per-server MCP teardown — vanilla only exposes "shutdown all"). | `tests/test_mcp_disconnect.py::test_upstream_state_present` |

**Why this is necessary:** vanilla upstream `gateway/run.py:_create_adapter`
only sets `adapter.gateway_runner` for built-in (fork-bundled) platforms,
NOT for plugin-registered platforms. With no public way for a plugin
adapter to reach the live runner, every Myah feature that needs the
runner — Phase B model overrides, per-message attribution, Phase F
structured streaming — silently degrades unless the plugin self-resolves
via the upstream-exposed module-level weakref.

**Removal path:** the listed features are eligible for removal once
upstream exposes equivalent public APIs (tracked as U-RUNNER in the
spec). The CI guards make the swap auditable — when a public surface
lands, delete the corresponding private access path and its guard test
in the same PR.

## [1.1.0] — 2026-05-10

### Added

- **OSS user_id bootstrap (Phase 8.2)**: `register(ctx)` now calls
  the platform's `/api/v1/myah/whoami` to auto-discover its own
  `MYAH_USER_ID` if not set. Removes the manual "copy your user_id
  from the platform UI to ~/.hermes/.env" friction for OSS deployers.
  Hosted Myah unchanged (spawner still injects `MYAH_USER_ID`
  per-container).
- **F4 secret-capture global wiring (Phase 5.1)**: `register(ctx)`
  now calls `tools.skills_tool.set_secret_capture_callback(...)` with
  a wrapper that routes to the active `MyahAdapter._secret_capture_callback`
  via the `_LATEST_ADAPTER` module pointer + the
  `tools.approval.get_current_session_key()` contextvar. Without this,
  secret prompts silently auto-skipped on stock vanilla because no
  callback was wired (the fork's session-keyed wiring lived in
  `_run_agent`'s closure).
- **F7 MCP per-server disconnect (Phase 5.2)**:
  `myah_hermes_plugin.runtime_extensions.mcp_disconnect.disconnect_mcp_server(name)`.
  Direct access to upstream's `tools.mcp_tool._servers` /
  `_lock` (`threading.Lock`, sync) / `_run_on_mcp_loop` to tear down a
  single MCP server without restarting the gateway. Two CI guards
  catch upstream rename of any of those private attrs.

### Test gates

- 23 new tests across `test_user_id_bootstrap.py`,
  `test_secret_capture_wiring.py`, `test_mcp_disconnect.py`.
- All 333 plugin tests pass (310 prior + 23 new).

## [1.0.0] — 2026-05-08

First OSS-launch-eligible release. Tier 2C of the Myah OSS Completion epic.

### Compatibility

- **hermes-agent**: SHA-pinned to upstream commit
  `faa13e49f81480771ceeb55991bb0c27edf1a5fb` (Hermes-Agent v0.11-track,
  fetched 2026-05-08 from `NousResearch/Hermes-Agent@main`).
- **Verification:** Mode D litmus test (Tier 2A Task 2A.8) — 9/9 passing
  on stock upstream + plugin (F5/BOOT.md is deferred per spec §3.1 and
  excluded from the Mode D matrix).
- **Python:** ≥ 3.11.
- **aiohttp:** ≥ 3.9, < 4.0.

When `hermes-agent` ships to PyPI, the SHA pin becomes a semver pin
(`hermes-agent>=0.11,<0.12`) — see `pyproject.toml` for the canonical
declaration.

### Vendored upstream features

The plugin vendors the following Myah-platform-specific features that
do not yet exist upstream. Each will be removed as the corresponding
upstream PR (designed in spec §5) merges:

- **F1 — Cron approval card UI flow** (~322 LOC):
  `myah_hermes_plugin.cron_approval` (vendored from upstream
  `tools/approval.py:request_action_confirmation` + dispatcher);
  `myah_hermes_plugin.myah_tools.cron_tool` (shadows upstream's
  `tools/cronjob_tools.py` to import the vendored confirmation
  primitive). Removed when upstream PR U5 lands.
- **F2 — Provider catalog** (Myah V1 picker):
  `myah_hermes_plugin.myah_admin.myah_overrides`. No upstream PR planned
  (data-only, no generic value).
- **F3 — Telemetry hook protocol** (Sentry breadcrumbs, AI monitoring):
  `myah_hermes_plugin.myah_platform.adapter`'s telemetry wiring + plugin
  `register()` Sentry init. Removed when upstream PR U1 lands.
- **F4 — Session-keyed secret capture**:
  `myah_hermes_plugin.myah_tools.secrets_tool`.
  No upstream PR planned for v1.0.0 (revisit if Nous expresses interest).
- **F6 — Cron→Myah delivery metadata enrichment**:
  `MyahAdapter.build_delivery_metadata` (override of polymorphic
  `BasePlatformAdapter.build_delivery_metadata` shipped to fork in
  Tier 2B Task 2B.4 — same diff queued as upstream PR U-CRON).
- **F7 — MCP per-server disconnect**:
  `tools.mcp_tool.disconnect_mcp_server` (fork-side; same diff queued
  as upstream PR U-MCP).

### Deferred from this release

- **F5 — BOOT.md startup hook**: requires upstream
  `register_gateway_event_hook` (PR U-HOOK in spec §5). OSS users on
  stock+plugin do **not** get BOOT.md until U-HOOK merges. Mode D test
  matrix excludes the F5 row per spec §3.1.

### Architectural notes

- Plugin runs in **standalone-mode adapter** on `MYAH_GATEWAY_PORT`
  (default `8643`). One-way door per spec Tier 2A Task 2A.3 — hosted
  Myah keeps standalone mode permanently even if upstream PR U2
  (`register_pre_setup_hook`) merges later.
- Plugin uses **direct attribute access** against upstream-native private
  dicts (`_session_model_overrides`, `_agent_cache`, etc.) per spec
  §3.2.1's 2026-05-07-evening discovery, NOT the v2 plan's plugin-local
  vendored dicts. This unblocked Tier 2B without depending on upstream
  PRs U4 / U-OVERRIDE (both downgraded to "optional future robustness").
- A CI guard test (`tests/test_upstream_runner_attrs_present.py`) asserts
  the upstream private attrs exist; if a future upstream rename breaks
  the plugin, CI flags it loudly before deploy.

### Distribution

- The plugin is shipped to OSS users via `pip install` (from PyPI when
  available, or `pip install <local-source>` from the fork's
  `plugins/myah-hermes-plugin/` directory).
- Hosted Myah's stock+plugin agent image (`agent/Dockerfile.stock`)
  installs the plugin from local source — see `myah` parent repo
  `agent/Dockerfile.stock` for the canonical image build.
- The dashboard plugin (`myah_admin/`) is materialized at image build
  time via `myah-hermes-plugin install --dashboard-only --target
  /opt/myah/plugins/`. Hermes' filesystem-discovery loader picks it up
  on container start. Image SHA = plugin version; atomic rollback.

## [0.3.0] — 2026-05-07 (internal-only, Tier 2B)

- Tier 2B Task 2B.3: migrated `agent/hermes/plugins/myah-admin/` into
  `myah_hermes_plugin.myah_admin/` (Phase 4e) with a new
  `myah-hermes-plugin install --dashboard-only` console script.
- Tier 2B Task 2B.4: shipped polymorphic
  `BasePlatformAdapter.build_delivery_metadata` to the fork +
  `MyahAdapter.build_delivery_metadata` override (Phase 4f); deletes
  `cron/scheduler.py`'s hardcoded `if platform_name == "myah"` branch.
- Tier 2B Task 2B.0: replaced 19 plugin callsites of fork-only
  `GatewayRunner` methods with direct attribute access against
  upstream-native private dicts; deletes 8 fork-only methods +
  `SessionOverride` TypedDict.

## [0.2.0] — 2026-04-28 (internal-only, Phase 4d)

- Phase 4d: moved `gateway/platforms/myah.py` adapter from the fork
  into the plugin via `ctx.register_platform()`.
- Phase 4c: moved `tools/secrets_tool.py` into the plugin.

## [0.1.0] — 2026-04-21 (internal-only, Phase 4b)

- Phase 4b: empty skeleton, pip-installable, `hermes_agent.plugins`
  entry point registered.
