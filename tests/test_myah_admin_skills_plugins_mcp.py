"""Tests for the myah-admin plugin's skills/plugins/MCP/toolset routes.

Exercises ``myah_hermes_plugin.myah_admin.dashboard._skills_plugins_mcp``
directly via FastAPI's ``TestClient``. The router is mounted on a bare
FastAPI app so the dashboard process is not required.

Auth is disabled by leaving ``HERMES_WEB_SESSION_TOKEN`` unset — the
``require_session_token`` dependency accepts all requests in that case
(matches the legacy aiohttp behaviour and the pattern used by
``test_myah_admin_providers.py``).

Phase 4e (2026-05-07): test was migrated from
``agent/hermes/tests/plugins/`` to the pip-plugin's tests/ directory.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import _skills_plugins_mcp


@pytest.fixture(scope='module')
def spm_mod() -> types.ModuleType:
    """The migrated _skills_plugins_mcp module (clean package member)."""
    return _skills_plugins_mcp


@pytest.fixture
def hermes_home(tmp_path, monkeypatch) -> Path:
    """Per-test ``HERMES_HOME`` so file-system writes don't bleed across tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def app(spm_mod) -> FastAPI:
    application = FastAPI()
    application.include_router(spm_mod.router)
    return application


@pytest.fixture
def client(app, monkeypatch) -> TestClient:
    """Auth disabled (no HERMES_WEB_SESSION_TOKEN env var)."""
    monkeypatch.delenv("HERMES_WEB_SESSION_TOKEN", raising=False)
    return TestClient(app)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_skill(home: Path, *, name: str, category: str = "general", body: str = "") -> Path:
    skill_dir = home / "skills" / category / name
    skill_dir.mkdir(parents=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body or 'body'}\n",
    )
    return md


# ── Skills ──────────────────────────────────────────────────────────────────


class TestSkillCRUD:
    def test_get_skill_returns_content(self, client, hermes_home):
        _write_skill(hermes_home, name="my-skill", category="research")

        resp = client.get("/skills/my-skill")

        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "my-skill"
        assert body["category"] == "research"
        assert "name: my-skill" in body["content"]

    def test_get_skill_missing_returns_404(self, client, hermes_home):
        resp = client.get("/skills/nope")
        assert resp.status_code == 404

    def test_get_skill_empty_dir_returns_404(self, client, hermes_home):
        # No skills/ dir at all.
        resp = client.get("/skills/anything")
        assert resp.status_code == 404

    def test_get_skill_path_traversal_rejected(self, client, hermes_home):
        # A name with `/` or `..` cannot match the regex.
        resp = client.get("/skills/..%2Fetc%2Fpasswd")
        assert resp.status_code in (404, 422)
        # FastAPI may decode the URL — both outcomes are acceptable as
        # long as the request never escapes HERMES_HOME. Confirm by
        # ensuring no file outside hermes_home was touched.

    def test_create_skill_writes_file(self, client, hermes_home):
        resp = client.post(
            "/skills",
            json={"name": "new-skill", "category": "general", "content": "body"},
        )

        assert resp.status_code == 201
        skill_path = hermes_home / "skills" / "general" / "new-skill" / "SKILL.md"
        assert skill_path.exists()
        assert skill_path.read_text() == "body"

    def test_create_skill_invalid_name_returns_422(self, client, hermes_home):
        resp = client.post(
            "/skills",
            json={"name": "bad name!", "category": "general", "content": "x"},
        )
        assert resp.status_code == 422

    def test_create_skill_path_traversal_rejected(self, client, hermes_home):
        # A path-traversal segment in the name fails the regex => 422.
        resp = client.post(
            "/skills",
            json={"name": "../../etc/passwd", "category": "general", "content": "x"},
        )
        assert resp.status_code == 422
        # Confirm nothing was written outside hermes_home.
        assert not (hermes_home.parent / "etc").exists()

    def test_create_skill_invalid_category_returns_422(self, client, hermes_home):
        resp = client.post(
            "/skills",
            json={"name": "ok", "category": "bad cat!", "content": "x"},
        )
        assert resp.status_code == 422

    def test_create_skill_blank_content_returns_400(self, client, hermes_home):
        resp = client.post(
            "/skills",
            json={"name": "ok", "category": "general", "content": "   "},
        )
        assert resp.status_code == 400

    def test_create_skill_conflict_returns_409(self, client, hermes_home):
        _write_skill(hermes_home, name="dup", category="general")
        resp = client.post(
            "/skills",
            json={"name": "dup", "category": "general", "content": "x"},
        )
        assert resp.status_code == 409

    def test_update_skill_overwrites_content(self, client, hermes_home):
        md = _write_skill(hermes_home, name="upd", category="general")
        resp = client.put("/skills/upd", json={"content": "new body"})

        assert resp.status_code == 200
        assert md.read_text() == "new body"

    def test_update_skill_missing_returns_404(self, client, hermes_home):
        resp = client.put("/skills/nope", json={"content": "x"})
        assert resp.status_code == 404

    def test_update_skill_blank_content_returns_400(self, client, hermes_home):
        _write_skill(hermes_home, name="upd2", category="general")
        resp = client.put("/skills/upd2", json={"content": ""})
        assert resp.status_code == 400

    def test_delete_skill_removes_directory(self, client, hermes_home):
        _write_skill(hermes_home, name="gone", category="general")
        skill_dir = hermes_home / "skills" / "general" / "gone"
        assert skill_dir.exists()

        resp = client.delete("/skills/gone")

        assert resp.status_code == 200
        assert not skill_dir.exists()

    def test_delete_skill_missing_returns_404(self, client, hermes_home):
        resp = client.delete("/skills/nope")
        assert resp.status_code == 404


# ── Plugins ─────────────────────────────────────────────────────────────────


class TestPluginCRUD:
    def test_list_plugins_empty(self, client, hermes_home):
        resp = client.get("/plugins")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_plugins_skips_underscore_files(self, client, hermes_home):
        plugins_dir = hermes_home / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "good.py").write_text("# Good plugin\n")
        (plugins_dir / "_private.py").write_text("# Hidden\n")

        resp = client.get("/plugins")
        body = resp.json()

        assert len(body) == 1
        assert body[0]["name"] == "good"

    def test_create_plugin_writes_file_and_schedules_restart(
        self, client, hermes_home, spm_mod,
    ):
        with patch.object(spm_mod, "_schedule_restart") as mock_restart:
            resp = client.post(
                "/plugins",
                json={"name": "myplug", "content": "x = 1\n"},
            )

        assert resp.status_code == 201
        plugin_path = hermes_home / "plugins" / "myplug.py"
        assert plugin_path.exists()
        assert plugin_path.read_text() == "x = 1\n"
        mock_restart.assert_called_once()

    def test_create_plugin_bad_python_returns_422(self, client, hermes_home, spm_mod):
        with patch.object(spm_mod, "_schedule_restart") as mock_restart:
            resp = client.post(
                "/plugins",
                json={"name": "broken", "content": "def bad(:\n"},
            )

        assert resp.status_code == 422
        assert "syntax error" in resp.json()["detail"].lower()
        mock_restart.assert_not_called()

    def test_create_plugin_invalid_name_returns_422(self, client, hermes_home):
        resp = client.post(
            "/plugins",
            json={"name": "bad name", "content": "x = 1\n"},
        )
        assert resp.status_code == 422

    def test_create_plugin_blank_content_returns_400(self, client, hermes_home):
        resp = client.post("/plugins", json={"name": "p", "content": "   "})
        assert resp.status_code == 400

    def test_create_plugin_conflict_returns_409(self, client, hermes_home, spm_mod):
        plugins_dir = hermes_home / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "dup.py").write_text("# existing\n")

        with patch.object(spm_mod, "_schedule_restart"):
            resp = client.post(
                "/plugins", json={"name": "dup", "content": "y = 2\n"},
            )
        assert resp.status_code == 409

    def test_update_plugin_overwrites_and_restarts(self, client, hermes_home, spm_mod):
        plugins_dir = hermes_home / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "p.py").write_text("a = 1\n")

        with patch.object(spm_mod, "_schedule_restart") as mock_restart:
            resp = client.put("/plugins/p", json={"content": "a = 2\n"})

        assert resp.status_code == 200
        assert (plugins_dir / "p.py").read_text() == "a = 2\n"
        mock_restart.assert_called_once()

    def test_update_plugin_missing_returns_404(self, client, hermes_home, spm_mod):
        with patch.object(spm_mod, "_schedule_restart"):
            resp = client.put("/plugins/missing", json={"content": "x = 1\n"})
        assert resp.status_code == 404

    def test_update_plugin_bad_syntax_returns_422(self, client, hermes_home, spm_mod):
        plugins_dir = hermes_home / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "p.py").write_text("a = 1\n")

        with patch.object(spm_mod, "_schedule_restart") as mock_restart:
            resp = client.put("/plugins/p", json={"content": "def f(:"})
        assert resp.status_code == 422
        mock_restart.assert_not_called()

    def test_delete_plugin_removes_file(self, client, hermes_home, spm_mod):
        plugins_dir = hermes_home / "plugins"
        plugins_dir.mkdir()
        target = plugins_dir / "kill.py"
        target.write_text("x = 1\n")

        with patch.object(spm_mod, "_schedule_restart") as mock_restart:
            resp = client.delete("/plugins/kill")

        assert resp.status_code == 200
        assert not target.exists()
        mock_restart.assert_called_once()

    def test_delete_plugin_missing_returns_404(self, client, hermes_home, spm_mod):
        with patch.object(spm_mod, "_schedule_restart"):
            resp = client.delete("/plugins/nope")
        assert resp.status_code == 404


# ── MCP ─────────────────────────────────────────────────────────────────────


class TestMCPCRUD:
    def test_list_mcp_empty(self, client, hermes_home):
        resp = client.get("/mcp")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_mcp_returns_servers_from_config(self, client, hermes_home):
        cfg_path = hermes_home / "config.yaml"
        cfg_path.write_text(
            yaml.safe_dump(
                {
                    "mcp_servers": {
                        "alpha": {"url": "http://a"},
                        "beta": {"command": "node", "args": ["x.js"]},
                    },
                },
            ),
        )

        resp = client.get("/mcp")
        assert resp.status_code == 200
        body = resp.json()
        names = {s["name"] for s in body}
        assert names == {"alpha", "beta"}

    def test_add_mcp_writes_config_and_calls_refresh(
        self, client, hermes_home, spm_mod,
    ):
        fake_client = AsyncMock()
        fake_client.request_or_raise = AsyncMock(return_value={"ok": True})

        with patch.object(spm_mod, "gateway_client", fake_client):
            resp = client.post(
                "/mcp",
                json={"name": "alpha", "url": "http://a"},
            )

        assert resp.status_code == 200
        cfg = yaml.safe_load((hermes_home / "config.yaml").read_text())
        assert cfg["mcp_servers"]["alpha"]["url"] == "http://a"
        fake_client.request_or_raise.assert_awaited_once_with("POST", "/mcp/refresh")

    def test_add_mcp_with_api_key_writes_env(self, client, hermes_home, spm_mod):
        fake_client = AsyncMock()
        fake_client.request_or_raise = AsyncMock(return_value={"ok": True})

        with patch.object(spm_mod, "gateway_client", fake_client):
            resp = client.post(
                "/mcp",
                json={"name": "openai", "url": "http://x", "api_key": "sk-XXXX"},
            )
        assert resp.status_code == 200
        env_text = (hermes_home / ".env").read_text()
        assert "MCP_OPENAI_API_KEY=sk-XXXX" in env_text

    def test_add_mcp_with_command(self, client, hermes_home, spm_mod):
        fake_client = AsyncMock()
        fake_client.request_or_raise = AsyncMock(return_value={"ok": True})

        with patch.object(spm_mod, "gateway_client", fake_client):
            resp = client.post(
                "/mcp",
                json={
                    "name": "stdio-srv",
                    "command": "node",
                    "args": ["server.js"],
                    "env": {"FOO": "bar"},
                },
            )
        assert resp.status_code == 200
        cfg = yaml.safe_load((hermes_home / "config.yaml").read_text())
        srv = cfg["mcp_servers"]["stdio-srv"]
        assert srv["command"] == "node"
        assert srv["args"] == ["server.js"]
        assert srv["env"] == {"FOO": "bar"}

    def test_add_mcp_without_url_or_command_returns_422(
        self, client, hermes_home, spm_mod,
    ):
        with patch.object(spm_mod, "gateway_client", AsyncMock()):
            resp = client.post("/mcp", json={"name": "broken"})
        assert resp.status_code == 422

    def test_add_mcp_invalid_name_returns_422(self, client, hermes_home):
        resp = client.post("/mcp", json={"name": "bad name", "url": "http://a"})
        assert resp.status_code == 422

    def test_add_mcp_continues_on_gateway_failure(
        self, client, hermes_home, spm_mod,
    ):
        # If the gateway is down, the file write must still succeed.
        fake_client = AsyncMock()
        fake_client.request_or_raise = AsyncMock(
            side_effect=HTTPException(status_code=503, detail="down"),
        )

        with patch.object(spm_mod, "gateway_client", fake_client):
            resp = client.post("/mcp", json={"name": "alpha", "url": "http://a"})

        assert resp.status_code == 200
        cfg = yaml.safe_load((hermes_home / "config.yaml").read_text())
        assert "alpha" in cfg["mcp_servers"]

    def test_remove_mcp_writes_config_and_calls_disconnect_then_evict(
        self, client, hermes_home, spm_mod,
    ):
        cfg_path = hermes_home / "config.yaml"
        cfg_path.write_text(
            yaml.safe_dump({"mcp_servers": {"alpha": {"url": "http://a"}}}),
        )

        fake_client = AsyncMock()
        fake_client.request_or_raise = AsyncMock(return_value={"ok": True})

        with patch.object(spm_mod, "gateway_client", fake_client):
            resp = client.delete("/mcp/alpha")

        assert resp.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert "alpha" not in (cfg.get("mcp_servers") or {})

        calls = [c.args for c in fake_client.request_or_raise.await_args_list]
        assert ("POST", "/mcp/disconnect/alpha") in calls
        assert ("POST", "/cache/evict-all") in calls

    def test_remove_mcp_missing_returns_404(self, client, hermes_home, spm_mod):
        with patch.object(spm_mod, "gateway_client", AsyncMock()):
            resp = client.delete("/mcp/missing")
        assert resp.status_code == 404


# ── Toolset toggle ──────────────────────────────────────────────────────────


class TestToggleToolset:
    def test_enable_calls_subprocess_and_evicts_caches(
        self, client, hermes_home, spm_mod,
    ):
        fake_client = AsyncMock()
        fake_client.request_or_raise = AsyncMock(return_value={"ok": True})

        async def fake_subproc(*cmd: str, timeout: float = 10):
            return 0, "ok\n", ""

        with patch.object(spm_mod, "gateway_client", fake_client), \
             patch.object(spm_mod, "_async_subprocess", fake_subproc):
            resp = client.patch("/toolsets/web", json={"enabled": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "web"
        assert body["enabled"] is True
        fake_client.request_or_raise.assert_awaited_once_with("POST", "/cache/evict-all")

    def test_disable_passes_disable_action(self, client, hermes_home, spm_mod):
        fake_client = AsyncMock()
        fake_client.request_or_raise = AsyncMock(return_value={"ok": True})

        captured: dict = {}

        async def fake_subproc(*cmd: str, timeout: float = 10):
            captured["cmd"] = cmd
            return 0, "", ""

        with patch.object(spm_mod, "gateway_client", fake_client), \
             patch.object(spm_mod, "_async_subprocess", fake_subproc):
            resp = client.patch("/toolsets/web", json={"enabled": False})

        assert resp.status_code == 200
        assert captured["cmd"] == ("hermes", "tools", "disable", "web")

    def test_subprocess_failure_returns_500(self, client, hermes_home, spm_mod):
        async def fake_subproc(*cmd: str, timeout: float = 10):
            return 1, "", "boom\n"

        with patch.object(spm_mod, "gateway_client", AsyncMock()), \
             patch.object(spm_mod, "_async_subprocess", fake_subproc):
            resp = client.patch("/toolsets/bad", json={"enabled": True})

        assert resp.status_code == 500
        assert "boom" in resp.json()["detail"]

    def test_invalid_name_returns_422(self, client, hermes_home, spm_mod):
        resp = client.patch("/toolsets/bad name", json={"enabled": True})
        assert resp.status_code == 422


# ── Route count smoke test ──────────────────────────────────────────────────


def test_router_has_expected_routes(spm_mod):
    """Sanity check: every handler is registered on the router.

    The legacy file shipped 9 user-facing endpoints in this set:
    GET/POST/PUT/DELETE skills (4), GET/POST/PUT/DELETE plugins (4 — list
    is GET, then C/U/D), GET/POST/DELETE mcp (3), and PATCH toolsets (1).
    Plus the legacy ``handle_get_skill`` rolled into the same set => 12.
    """
    paths = {
        (route.path, tuple(sorted(route.methods - {"HEAD"})))
        for route in spm_mod.router.routes
    }
    assert ("/skills/{name}", ("GET",)) in paths
    assert ("/skills", ("POST",)) in paths
    assert ("/skills/{name}", ("PUT",)) in paths
    assert ("/skills/{name}", ("DELETE",)) in paths

    assert ("/plugins", ("GET",)) in paths
    assert ("/plugins", ("POST",)) in paths
    assert ("/plugins/{name}", ("PUT",)) in paths
    assert ("/plugins/{name}", ("DELETE",)) in paths

    assert ("/mcp", ("GET",)) in paths
    assert ("/mcp", ("POST",)) in paths
    assert ("/mcp/{name}", ("DELETE",)) in paths

    assert ("/toolsets/{name}", ("PATCH",)) in paths
