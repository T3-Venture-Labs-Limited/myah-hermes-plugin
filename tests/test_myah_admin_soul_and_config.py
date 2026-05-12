"""Tests for the myah-admin plugin's SOUL/config/commands endpoints.

Exercises ``myah_hermes_plugin.myah_admin.dashboard._soul_and_config``
via FastAPI ``TestClient``.

Phase 4e (2026-05-07): test was migrated from
``agent/hermes/tests/plugins/`` to the pip-plugin's tests/ directory.
The dashboard now lives inside the pip package as a proper Python
package; the synthetic-module loading boilerplate (which used
``spec_from_file_location`` to work around the hyphen in
``plugins/myah-admin/``) is gone.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import _soul_and_config as _soul_and_config_module


def _load_module():
    """Compatibility shim — returns the already-imported pip-package module
    with ``_SOUL_DEFAULTS_PATH`` refreshed from the (possibly monkeypatched)
    ``MYAH_SOUL_DEFAULTS`` env var.

    The legacy test loaded the module fresh per call via
    ``spec_from_file_location`` which re-read env vars at load time. The
    pip-package layout imports once at collection time so the module-level
    ``_SOUL_DEFAULTS_PATH = Path(os.environ.get(...))`` gets evaluated
    before pytest fixtures monkeypatch the env var. Refreshing it here
    preserves the original semantics without changing production code.
    """
    _soul_and_config_module._SOUL_DEFAULTS_PATH = Path(
        os.environ.get('MYAH_SOUL_DEFAULTS', '/opt/myah/defaults/SOUL.md')
    )
    return _soul_and_config_module


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Per-test HERMES_HOME with config.yaml and SOUL.md placeholders."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Disable the session token requirement; the dependency short-circuits
    # when ``HERMES_WEB_SESSION_TOKEN`` is unset.
    monkeypatch.delenv("HERMES_WEB_SESSION_TOKEN", raising=False)
    return home


@pytest.fixture
def soul_defaults(tmp_path, monkeypatch):
    """Image-default SOUL.md path, redirected to a tempdir for tests."""
    defaults_dir = tmp_path / "image-defaults"
    defaults_dir.mkdir()
    soul_path = defaults_dir / "SOUL.md"
    soul_path.write_text("# Default SOUL\n", encoding="utf-8")
    monkeypatch.setenv("MYAH_SOUL_DEFAULTS", str(soul_path))
    return soul_path


@pytest.fixture
def client(hermes_home, soul_defaults):
    """FastAPI test client with the plugin router mounted."""
    mod = _load_module()
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app), mod


# ── Helpers ─────────────────────────────────────────────────────────────────


def _etag_for(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f'"sha256-{digest}"'


# ── SOUL endpoints ──────────────────────────────────────────────────────────


def test_get_soul_returns_text_markdown_with_etag(client, hermes_home):
    tc, _mod = client
    soul = hermes_home / "SOUL.md"
    soul.write_text("# Hello SOUL\n", encoding="utf-8")

    resp = tc.get("/config/soul")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["etag"] == _etag_for("# Hello SOUL\n")
    assert resp.headers["x-soul-soft-warn-chars"] == "8192"
    assert resp.headers["x-soul-hard-cap-chars"] == "32768"
    assert resp.text == "# Hello SOUL\n"


def test_get_soul_404_when_absent(client):
    tc, _mod = client
    resp = tc.get("/config/soul")
    assert resp.status_code == 404


def test_put_soul_without_if_match_returns_428(client, hermes_home):
    tc, _mod = client
    (hermes_home / "SOUL.md").write_text("a", encoding="utf-8")
    resp = tc.put("/config/soul", content="b", headers={"Content-Type": "text/markdown"})
    assert resp.status_code == 428


def test_put_soul_with_wrong_if_match_returns_412(client, hermes_home):
    tc, _mod = client
    (hermes_home / "SOUL.md").write_text("current\n", encoding="utf-8")
    resp = tc.put(
        "/config/soul",
        content="new",
        headers={
            "Content-Type": "text/markdown",
            "If-Match": '"sha256-deadbeef"',
        },
    )
    assert resp.status_code == 412
    payload = resp.json()
    assert payload["current_body"] == "current\n"
    # Server returns the *current* ETag so the client can retry without a re-fetch.
    assert resp.headers["etag"] == _etag_for("current\n")


def test_put_soul_over_32k_returns_413(client, hermes_home):
    tc, _mod = client
    (hermes_home / "SOUL.md").write_text("x", encoding="utf-8")
    etag = _etag_for("x")
    body = "y" * 40_000  # exceeds 32 KiB hard cap
    resp = tc.put(
        "/config/soul",
        content=body,
        headers={"Content-Type": "text/markdown", "If-Match": etag},
    )
    assert resp.status_code == 413


def test_put_soul_happy_path(client, hermes_home):
    tc, _mod = client
    (hermes_home / "SOUL.md").write_text("old\n", encoding="utf-8")
    etag = _etag_for("old\n")
    new_body = "shiny new soul\n"
    resp = tc.put(
        "/config/soul",
        content=new_body,
        headers={"Content-Type": "text/markdown", "If-Match": etag},
    )
    assert resp.status_code == 200
    assert resp.headers["etag"] == _etag_for(new_body)
    assert resp.json() == {"ok": True}
    assert (hermes_home / "SOUL.md").read_text(encoding="utf-8") == new_body


def test_put_soul_warns_above_soft_limit(client, hermes_home):
    tc, _mod = client
    (hermes_home / "SOUL.md").write_text("old", encoding="utf-8")
    etag = _etag_for("old")
    body = "y" * (8_192 + 100)  # over soft, under hard
    resp = tc.put(
        "/config/soul",
        content=body,
        headers={"Content-Type": "text/markdown", "If-Match": etag},
    )
    assert resp.status_code == 200
    assert "warning" in resp.json()


# ── /config/aux-resolved ────────────────────────────────────────────────────


def test_get_aux_resolved_returns_dict_with_source_tags(client):
    tc, _mod = client
    fake_default = {"auxiliary": {"vision": {}, "compression": {}}}

    def fake_load_config():
        return {"model": {"provider": "openrouter", "default": "anthropic/claude-3-5-sonnet"}}

    def fake_resolve(task):
        if task == "vision":
            return ("auto", None, None, None, None)
        if task == "compression":
            return ("anthropic", "claude-3-5-haiku", None, None, None)
        return ("auto", None, None, None, None)

    fake_modules = {
        "hermes_cli.config": type("M", (), {"DEFAULT_CONFIG": fake_default, "load_config": fake_load_config}),
        "agent.auxiliary_client": type(
            "M",
            (),
            {"_resolve_task_provider_model": lambda task=None, **_: fake_resolve(task)},
        ),
    }

    with patch.dict("sys.modules", fake_modules):
        resp = tc.get("/config/aux-resolved")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"vision", "compression"}
    # vision -> "auto" with main model present -> auto-main
    assert body["vision"]["source"] == "auto-main"
    assert body["vision"]["provider"] == "openrouter"
    # compression -> explicit provider/model -> config
    assert body["compression"]["source"] == "config"
    assert body["compression"]["provider"] == "anthropic"


# ── /commands ───────────────────────────────────────────────────────────────


def test_get_commands_returns_list(client):
    tc, mod = client
    # Reset cache before each call so we exercise the full collection path.
    mod._commands_cache["value"] = None
    mod._commands_cache["expires_at"] = 0.0

    # Stub the command sources via sys.modules patching. The endpoint catches
    # all exceptions, so even a missing import returns an empty list — but we
    # want to verify it does include the registered sources.
    class FakeCmd:
        def __init__(self, name, description, category="Session", aliases=()):
            self.name = name
            self.description = description
            self.category = category
            self.aliases = aliases
            self.args_hint = ""
            self.cli_only = False

    fake_registry = [FakeCmd("ping", "send a ping"), FakeCmd("help", "show help")]
    fake_modules = {
        "hermes_cli.commands": type(
            "M",
            (),
            {
                "COMMAND_REGISTRY": fake_registry,
                "ACTIVE_SESSION_BYPASS_COMMANDS": frozenset({"help"}),
            },
        ),
        "agent.skill_commands": type("M", (), {"get_skill_commands": lambda: {}}),
        "hermes_cli.plugins": type("M", (), {"get_plugin_commands": lambda: {}}),
    }
    with patch.dict("sys.modules", fake_modules):
        resp = tc.get("/commands")

    assert resp.status_code == 200
    body = resp.json()
    assert "commands" in body
    names = {c["name"] for c in body["commands"]}
    assert {"ping", "help"}.issubset(names)


# ── /config/reset ───────────────────────────────────────────────────────────


def test_reset_unknown_section_returns_400(client):
    tc, _mod = client
    resp = tc.post("/config/reset/not-a-real-section")
    assert resp.status_code == 400
    assert "unknown section" in resp.json()["detail"]


def test_reset_soul_copies_from_default(client, hermes_home, soul_defaults):
    tc, _mod = client
    # Pre-existing SOUL gets overwritten.
    (hermes_home / "SOUL.md").write_text("user-edited soul\n", encoding="utf-8")

    resp = tc.post("/config/reset/soul")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "section": "soul"}
    assert (hermes_home / "SOUL.md").read_text(encoding="utf-8") == "# Default SOUL\n"


def test_reset_soul_503_when_defaults_missing(tmp_path, monkeypatch, hermes_home):
    """When MYAH_SOUL_DEFAULTS does not exist, return 503 (dev-container case)."""
    monkeypatch.setenv("MYAH_SOUL_DEFAULTS", str(tmp_path / "nonexistent" / "SOUL.md"))
    mod = _load_module()
    app = FastAPI()
    app.include_router(mod.router)
    tc = TestClient(app)

    resp = tc.post("/config/reset/soul")
    assert resp.status_code == 503


def test_reset_mcp_servers_clears_config_and_calls_gateway(client, hermes_home):
    """Reset of mcp_servers writes empty dict and invokes gateway evict + refresh."""
    tc, mod = client

    # Seed a config.yaml with some mcp_servers.
    cfg_path = hermes_home / "config.yaml"
    import yaml as _yaml
    cfg_path.write_text(
        _yaml.safe_dump({"mcp_servers": {"foo": {"command": "x"}, "bar": {"command": "y"}}})
    )

    calls: list[tuple[str, str]] = []

    async def fake_request_or_raise(method, path, **_):
        calls.append((method, path))
        return {}

    def fake_set_config_value(key, value):
        # Simulate the canonical setter writing to YAML.
        data = _yaml.safe_load(cfg_path.read_text()) or {}
        data[key] = _yaml.safe_load(value)
        cfg_path.write_text(_yaml.safe_dump(data))

    fake_modules = {
        "hermes_cli.config": type(
            "M",
            (),
            {"set_config_value": fake_set_config_value, "DEFAULT_CONFIG": {}},
        ),
    }
    with patch.dict("sys.modules", fake_modules), \
         patch.object(mod.gateway_client, "request_or_raise", side_effect=fake_request_or_raise):
        resp = tc.post("/config/reset/mcp_servers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["section"] == "mcp_servers"
    assert sorted(body["removed"]) == ["bar", "foo"]
    # Both gateway calls were attempted.
    assert ("POST", "/cache/evict-all") in calls
    assert ("POST", "/mcp/refresh") in calls


# ── /config/last-reseed ─────────────────────────────────────────────────────


def test_get_last_reseed_204_when_absent(client):
    tc, _mod = client
    resp = tc.get("/config/last-reseed")
    assert resp.status_code == 204
    assert resp.content == b""


def test_get_last_reseed_normalises_files_to_array(client, hermes_home):
    tc, _mod = client
    marker = hermes_home / ".myah_last_reseed"
    marker.write_text("ts=2026-04-25T10:00:00Z\nfiles=config soul cron\nreason=image-bump\n")
    resp = tc.get("/config/last-reseed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == ["config", "soul", "cron"]
    assert body["ts"] == "2026-04-25T10:00:00Z"
    assert body["reason"] == "image-bump"


# ── Auth gate (smoke test) ──────────────────────────────────────────────────


def test_session_token_enforced_when_set(hermes_home, soul_defaults, monkeypatch):
    monkeypatch.setenv("HERMES_WEB_SESSION_TOKEN", "secret-token")
    mod = _load_module()
    app = FastAPI()
    app.include_router(mod.router)
    tc = TestClient(app)

    # No header -> 401
    resp = tc.get("/commands")
    assert resp.status_code == 401

    # Wrong token -> 401
    resp = tc.get("/commands", headers={"X-Hermes-Session-Token": "wrong"})
    assert resp.status_code == 401

    # Correct token -> 200 (or whatever the underlying handler returns).
    # Stub modules so the handler doesn't blow up importing hermes internals.
    fake_modules = {
        "hermes_cli.commands": type(
            "M",
            (),
            {"COMMAND_REGISTRY": [], "ACTIVE_SESSION_BYPASS_COMMANDS": frozenset()},
        ),
        "agent.skill_commands": type("M", (), {"get_skill_commands": lambda: {}}),
        "hermes_cli.plugins": type("M", (), {"get_plugin_commands": lambda: {}}),
    }
    mod._commands_cache["value"] = None
    mod._commands_cache["expires_at"] = 0.0
    with patch.dict("sys.modules", fake_modules):
        resp = tc.get(
            "/commands",
            headers={"X-Hermes-Session-Token": "secret-token"},
        )
    assert resp.status_code == 200
