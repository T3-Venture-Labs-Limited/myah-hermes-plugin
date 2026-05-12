# myah-hermes-plugin

Myah platform connector for [Hermes Agent](https://github.com/NousResearch/Hermes-Agent).

This pip package is the upstream-supported integration point for the
[Myah](https://app.myah.dev) self-hosted web platform. It registers a
Hermes platform adapter, Myah-specific tools, and an admin dashboard
plugin via Hermes' standard plugin extension model
(`hermes_agent.plugins` entry point).

## Status

**v1.0.0 — first OSS-launch-eligible release** (Tier 2C of the Myah OSS
Completion epic, 2026-05-08). Plugin works against stock upstream Hermes
(no fork required). See `CHANGELOG.md` for the full history of
internal-only versions (0.1.x – 0.3.x → Phases 4b – 4f → Tier 2A/2B).

## Install

```bash
# From the fork checkout (hermes-agent is not yet on PyPI).
# Step 1: install hermes-agent from upstream at the verified SHA
#         (see CHANGELOG.md "Compatibility" for the canonical SHA).
pip install "hermes-agent[messaging,cron,honcho,mcp,voice,pty,web] @ \
    git+https://github.com/NousResearch/Hermes-Agent@<HERMES_SHA>"

# Step 2: install the plugin from local source (no PyPI release yet).
pip install plugins/myah-hermes-plugin --no-deps
```

When hermes-agent ships to PyPI, both installs become single
`pip install` commands and the dep declaration in `pyproject.toml`
becomes a strict semver pin (`hermes-agent>=0.11,<0.12`).

After install, verify the entry point is registered:

```bash
python -c "import importlib.metadata as m; \
  eps = m.entry_points(group='hermes_agent.plugins'); \
  print([e.name for e in eps])"
```

Should include `myah-platform` in the output.

## Compatibility

| Plugin                | Hermes-agent                                                                             | Notes                                                                                                  |
| --------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `0.1.x` – `0.3.x`     | git+https from `T3-Venture-Labs-Limited/hermes-agent` fork (internal-only)               | Coupled to fork SHA the hosted Myah ran. Not for public consumption.                                  |
| **`1.0.x`** (current) | git+https from `NousResearch/Hermes-Agent@faa13e49f81480771ceeb55991bb0c27edf1a5fb`      | First OSS launch. Verified by Mode D against the pinned upstream SHA. PyPI pin will be `>=0.11,<0.12`. |
| `1.1.x` (future)      | `>=0.12,<0.13` (or matching minor)                                                       | Plugin minor bump tracks hermes minor bump. Each transition requires Mode D re-verification.          |

### When hermes-agent ships a new minor version

1. Plugin CI runs Mode D against `hermes-agent==<new-minor>` (forward-compat job).
2. If green: cut a new plugin minor version with the wider compat window.
3. If red: file an upstream issue; either patch the plugin or open an
   upstream PR to restore stability.

### Why pin to a single hermes minor version

The plugin imports from `gateway.run`, `gateway.platform_registry`,
`tools.approval`, and similar paths that upstream considers internal.
Upstream may refactor these between minor versions. Pinning ensures a
hermes 0.11 → 0.12 release does NOT silently change plugin behavior —
users see a clear pip resolver error and can either upgrade the plugin
or stay on the matching hermes version.

### OSS user enforcement

`pip install hermes-agent==0.12 myah-hermes-plugin==1.0.0` fails at the
pip resolver if 1.0.0 only supports `hermes-agent < 0.12`. Clear error,
no runtime mystery.

## What this plugin provides

- **Platform adapter** for the Myah web platform (chat, attachments, model
  switching, message attribution, structured streaming SSE events).
- **Standalone-mode HTTP server** on `MYAH_GATEWAY_PORT` (default `8643`)
  exposing `/myah/v1/admin/*` endpoints for the platform backend's
  control plane.
- **Vendored cron approval flow** (F1) — `request_action_confirmation`
  primitive + dispatcher so cron jobs can prompt for user approval
  before persisting.
- **Provider catalog** (F2) — Myah V1 picker data shape served at
  `/api/v1/providers/catalog`.
- **Telemetry hook** (F3) — Sentry exception capture and AI-monitoring
  breadcrumbs around the agent run.
- **Session-keyed secret capture** (F4) — secrets prompts associated
  with the requesting agent session, so the platform UI can route
  responses back correctly.
- **Cron→Myah delivery metadata enrichment** (F6) — polymorphic override
  of `BasePlatformAdapter.build_delivery_metadata`.
- **MCP per-server disconnect** (F7) — `disconnect_mcp_server(name)`
  helper for runtime "remove MCP server" UX without bouncing all servers.
- **Admin dashboard plugin** materialized at image build time via the
  `myah-hermes-plugin install --dashboard-only` console script.

## License

MIT — see `LICENSE`.
