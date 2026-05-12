"""Tests for the myah-admin sessions/lifecycle handlers.

Covers ``myah_hermes_plugin.myah_admin.dashboard._sessions_and_lifecycle``:

  * POST /sessions/{id}/title — direct SessionDB write
  * POST /sessions/{id}/append — auto-creates session row
  * GET /sessions/{key}/model — proxies to gateway, unpacks ``override``
  * PUT /sessions/{key}/model — validates + proxies to gateway
  * GET/PUT /config/model — file-system + global cache evict

The gateway client is mocked throughout — these tests do NOT spin up the
aiohttp gateway. Real end-to-end coverage of the gateway-side handlers
lives in ``tests/gateway/test_myah_runtime_admin.py`` (in the hermes
repo).

Phase 4e (2026-05-07): test was migrated from
``agent/hermes/tests/plugins/`` to the pip-plugin's tests/ directory.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import (
    _common as _common_module,
    _sessions_and_lifecycle as _lifecycle_module,
)


def _load_lifecycle_module():
    """Compatibility shim — kept for the (mod, common_mod) tuple shape used by
    fixtures below. Now returns the already-imported pip-package modules."""
    return _lifecycle_module, _common_module


# ── Fixtures ────────────────────────────────────────────────────────────────


class _FakeGatewayClient:
    """Captures gateway calls and returns canned responses.

    Per-test instance — assert against ``calls`` for verification.
    Override ``responses`` with ``(method, path) -> body`` dict to
    customise per-test, or set ``error`` to raise.
    """

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, str], Any] = {}
        self.errors: dict[tuple[str, str], Exception] = {}

    async def request_or_raise(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append(
            {"method": method, "path": path, "json_body": json_body}
        )
        key = (method, path)
        if key in self.errors:
            raise self.errors[key]
        return self.responses.get(key, {})


@pytest.fixture
def fake_gw():
    return _FakeGatewayClient()


@pytest.fixture
def lifecycle_app(monkeypatch, tmp_path, fake_gw):
    """Build a FastAPI app that mounts the lifecycle router.

    Disables auth (no ``HERMES_WEB_SESSION_TOKEN``) and replaces the
    plugin's ``gateway_client`` singleton with a capturing fake.
    """
    monkeypatch.delenv("HERMES_WEB_SESSION_TOKEN", raising=False)

    # Make sure HERMES_HOME-derived paths point inside the test tempdir
    # so ``hermes_state`` (re-imported per test if needed) writes to it.
    fake_home = tmp_path / "hermes_home"
    fake_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_home))

    mod, common_mod = _load_lifecycle_module()

    # Swap the gateway client singleton on the loaded module + the
    # _common module (handlers import via ``from ._common import gateway_client``
    # which captures a local reference, so we patch the symbol the
    # handler module sees).
    monkeypatch.setattr(common_mod, "gateway_client", fake_gw)
    monkeypatch.setattr(mod, "gateway_client", fake_gw)

    app = FastAPI()
    app.include_router(mod.router)
    return app, mod


@pytest.fixture
def client(lifecycle_app):
    app, _mod = lifecycle_app
    return TestClient(app)


@pytest.fixture
def fresh_session_db(tmp_path, monkeypatch):
    """Build a fresh ``SessionDB`` rooted at the temp HERMES_HOME.

    ``hermes_state.DEFAULT_DB_PATH`` is computed at import time, so we
    patch it at runtime to the per-test tempdir before any handler call.
    """
    from hermes_state import SessionDB
    import hermes_state

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    return SessionDB(db_path=db_path), db_path


# ── Title ───────────────────────────────────────────────────────────────────


def test_set_session_title_writes_to_session_db(client, fresh_session_db):
    db, _path = fresh_session_db
    db.create_session(session_id="s1", source="myah", model="x")

    resp = client.post("/sessions/s1/title", json={"title": "Hello world"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"session_id": "s1", "title": "Hello world"}

    sess = db.get_session("s1")
    assert sess is not None
    assert sess["title"] == "Hello world"


def test_set_session_title_returns_404_when_session_missing(
    client, fresh_session_db
):
    resp = client.post("/sessions/nope/title", json={"title": "x"})
    assert resp.status_code == 404
    assert "Session not found" in resp.json()["detail"]


# ── Append ──────────────────────────────────────────────────────────────────


def test_append_message_creates_session_if_missing(client, fresh_session_db):
    db, _ = fresh_session_db
    assert db.get_session("brand-new") is None

    resp = client.post(
        "/sessions/brand-new/append",
        json={"role": "assistant", "content": "hi from cron"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == "brand-new"
    assert body["role"] == "assistant"
    assert isinstance(body["message_id"], int)

    sess = db.get_session("brand-new")
    assert sess is not None
    assert sess["source"] == "myah"

    msgs = db.get_messages_as_conversation("brand-new")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == "hi from cron"


def test_append_message_rejects_empty_content(client, fresh_session_db):
    resp = client.post(
        "/sessions/s1/append",
        json={"role": "assistant", "content": ""},
    )
    assert resp.status_code == 400
    assert "content is required" in resp.json()["detail"]


def test_append_message_rejects_invalid_role(client, fresh_session_db):
    resp = client.post(
        "/sessions/s1/append",
        json={"role": "system", "content": "x"},
    )
    assert resp.status_code == 422
    assert "role must be one of" in resp.json()["detail"]


# ── GET session model (proxy + unpack) ──────────────────────────────────────


def test_get_session_model_unpacks_override_from_gateway(client, fake_gw):
    fake_gw.responses[("GET", "/sessions/abc/override")] = {
        "override": {
            "model": "claude-sonnet-4",
            "provider": "anthropic",
            "base_url": "",
        }
    }
    resp = client.get("/sessions/abc/model")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "claude-sonnet-4"
    assert body["provider"] == "anthropic"
    # base_url is omitted when empty
    assert "base_url" not in body
    assert fake_gw.calls == [
        {"method": "GET", "path": "/sessions/abc/override", "json_body": None}
    ]


def test_get_session_model_handles_empty_override(client, fake_gw):
    fake_gw.responses[("GET", "/sessions/abc/override")] = {"override": None}
    resp = client.get("/sessions/abc/model")
    assert resp.status_code == 200
    assert resp.json() == {"model": "", "provider": ""}


# ── PUT session model (validation + proxy) ──────────────────────────────────


def _stub_switch_model(success=True, **kwargs):
    """Build a stub for ``hermes_cli.model_switch.switch_model`` with the
    fields the handler reads."""
    from types import SimpleNamespace

    defaults = {
        "success": success,
        "new_model": "claude-sonnet-4",
        "target_provider": "anthropic",
        "provider_label": "Anthropic",
        "api_key": "",
        "base_url": "",
        "api_mode": "chat_completions",
        "warning_message": "",
        "error_message": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_put_session_model_rejects_invalid_model(
    client, fake_gw, monkeypatch
):
    # Gateway returns no existing override; switch_model returns failure.
    fake_gw.responses[("GET", "/sessions/abc/override")] = {"override": None}

    import hermes_cli.model_switch as ms_mod

    monkeypatch.setattr(
        ms_mod,
        "switch_model",
        lambda **kw: _stub_switch_model(
            success=False, error_message="Unknown provider 'badprov'."
        ),
    )

    resp = client.put(
        "/sessions/abc/model",
        json={"model": "whatever", "provider": "badprov"},
    )
    assert resp.status_code == 400
    assert "Unknown provider" in resp.json()["detail"]
    # Should NOT have called PUT — validation gates the proxy.
    assert all(c["method"] != "PUT" for c in fake_gw.calls)


def test_put_session_model_proxies_validated_override_to_gateway(
    client, fake_gw, monkeypatch
):
    fake_gw.responses[("GET", "/sessions/abc/override")] = {"override": None}
    fake_gw.responses[("PUT", "/sessions/abc/override")] = {"ok": True}

    import hermes_cli.model_switch as ms_mod

    monkeypatch.setattr(
        ms_mod,
        "switch_model",
        lambda **kw: _stub_switch_model(),
    )

    resp = client.put(
        "/sessions/abc/model",
        json={"model": "sonnet", "provider": "anthropic"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "claude-sonnet-4"
    assert body["provider"] == "anthropic"
    assert body["provider_label"] == "Anthropic"

    put_calls = [c for c in fake_gw.calls if c["method"] == "PUT"]
    assert len(put_calls) == 1
    assert put_calls[0]["path"] == "/sessions/abc/override"
    sent = put_calls[0]["json_body"]
    assert sent["model"] == "claude-sonnet-4"
    assert sent["provider"] == "anthropic"
    assert sent["api_mode"] == "chat_completions"


def test_put_session_model_requires_model_in_body(client, fake_gw):
    resp = client.put("/sessions/abc/model", json={"model": "  "})
    assert resp.status_code == 400
    assert "model is required" in resp.json()["detail"]


# ── GET/PUT global config model ─────────────────────────────────────────────


def test_get_global_model_returns_empty_when_no_config(client, tmp_path):
    # No config.yaml written — handler should return ``{"model": ""}``.
    resp = client.get("/config/model")
    assert resp.status_code == 200
    assert resp.json() == {"model": ""}


def test_put_global_model_rejects_bad_model(
    client, fake_gw, monkeypatch
):
    import hermes_cli.model_switch as ms_mod

    monkeypatch.setattr(
        ms_mod,
        "switch_model",
        lambda **kw: _stub_switch_model(
            success=False, error_message="Model not recognized"
        ),
    )

    resp = client.put("/config/model", json={"model": "bogus-model"})
    assert resp.status_code == 400
    assert "Model not recognized" in resp.json()["detail"]
    # No subprocess + no gateway evict on validation failure.
    assert all(c["method"] != "POST" for c in fake_gw.calls)


def test_put_global_model_writes_config_and_evicts_all(
    lifecycle_app, fake_gw, monkeypatch
):
    app, mod = lifecycle_app
    client = TestClient(app)

    fake_gw.responses[("POST", "/cache/evict-all")] = {
        "ok": True, "evicted": 3,
    }

    import hermes_cli.model_switch as ms_mod

    monkeypatch.setattr(
        ms_mod,
        "switch_model",
        lambda **kw: _stub_switch_model(
            new_model="claude-sonnet-4",
            target_provider="anthropic",
        ),
    )

    # Stub _async_subprocess so we don't actually shell out to the
    # ``hermes`` binary (which may not be installed in CI).
    sub_calls: list[tuple[str, ...]] = []

    async def fake_subprocess(*cmd, timeout=10.0):
        sub_calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(mod, "_async_subprocess", fake_subprocess)

    resp = client.put(
        "/config/model",
        json={"model": "sonnet", "provider": "anthropic"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "claude-sonnet-4"
    assert body["provider"] == "anthropic"

    # ``hermes config set model claude-sonnet-4`` — and a second call for
    # the provider since one was supplied.
    assert any(
        cmd[:4] == ("hermes", "config", "set", "model")
        and cmd[4] == "claude-sonnet-4"
        for cmd in sub_calls
    )
    assert any(
        cmd[:5] == ("hermes", "config", "set", "model.provider", "anthropic")
        for cmd in sub_calls
    )

    # Global change → gateway evicts every cached agent.
    evict_calls = [c for c in fake_gw.calls if c["method"] == "POST"]
    assert evict_calls and evict_calls[0]["path"] == "/cache/evict-all"
