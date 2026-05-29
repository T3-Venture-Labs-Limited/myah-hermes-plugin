"""Test for Bug B: /myah/v1/confirm/{stream_id} dispatches to the right resolver.

Two queues exist on the approval surface:

* upstream's ``tools/approval.py:_gateway_queues`` — legacy
  terminal-command approvals; resolved by
  ``tools.approval.resolve_gateway_approval(session_key, choice)``.
* the plugin's ``myah_hermes_plugin.cron_approval._action_queues`` —
  modern action confirmations (cron creation, plugin install, etc.);
  resolved by
  ``myah_hermes_plugin.cron_approval.resolve_action_confirmation(confirmation_id, choice)``.

The current ``_handle_confirm_endpoint`` only calls
``resolve_gateway_approval`` — Approve/Deny clicks for cron approval
cards arrive but go nowhere because the entry sits in
``_action_queues``.

The fix accepts an optional ``confirmation_id`` in the body:
* present  → ``resolve_action_confirmation(confirmation_id, choice)``
* absent   → ``resolve_gateway_approval(session_key, choice)`` (legacy)
"""

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig

# Adapter and runtime-admin now fail closed when auth_key is empty.
# Tests must construct adapters with a real auth_key and authed headers.
_TEST_AUTH_KEY = "test-bearer-key-for-test_myah_confirm_dispatch"
_AUTHED_HEADERS = {"Authorization": f"Bearer {_TEST_AUTH_KEY}"}


def _make_adapter(auth_key: str = _TEST_AUTH_KEY):
    """Construct a MyahAdapter.

    Tier 2A Task 2A.3 removed the dependency on
    ``gateway.platforms.api_server.register_pre_setup_hook``; the
    adapter now owns its own aiohttp runner.
    """
    extra = dict()
    if auth_key:
        extra["auth_key"] = auth_key
    config = PlatformConfig(enabled=True, extra=extra)
    from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
    return MyahAdapter(config)


def _make_app(adapter) -> web.Application:
    """Mount only the /myah/v1/confirm/{stream_id} route for testing."""
    app = web.Application()
    app.router.add_post("/myah/v1/confirm/{stream_id}", adapter._handle_confirm_endpoint)
    return app


# ── Myah: Bug B regression coverage — modern + legacy dispatch ────


class TestConfirmEndpointActionDispatch:
    @pytest.mark.asyncio
    async def test_with_confirmation_id_routes_to_action_resolver(self):
        """When body includes confirmation_id, call resolve_action_confirmation."""
        adapter = _make_adapter()
        stream_id = "stream-1"
        session_key = "sess-1"
        adapter._stream_sessions[stream_id] = session_key

        with patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation", return_value=True) as mock_action, \
             patch("tools.approval.resolve_gateway_approval", return_value=0) as mock_legacy:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    f"/myah/v1/confirm/{stream_id}",
                    json={"confirmation_id": "conf-xyz", "choice": "approve"}, headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 200, body
        assert body.get("ok") is True
        mock_action.assert_called_once_with("conf-xyz", "approve")
        mock_legacy.assert_not_called()

    @pytest.mark.asyncio
    async def test_without_confirmation_id_tries_legacy_then_action_queue(self):
        """No confirmation_id → try resolve_gateway_approval first (legacy
        terminal-command path).  When that returns 0 (nothing pending in the
        legacy queue, the common case for cron approvals) fall through to
        ``resolve_action_confirmation_by_session`` so the frontend's
        ``ConfirmationCard`` POST resolves the cron approval without needing
        to know the confirmation_id explicitly."""
        adapter = _make_adapter()
        stream_id = "stream-2"
        session_key = "sess-2"
        adapter._stream_sessions[stream_id] = session_key

        with patch("tools.approval.resolve_gateway_approval", return_value=0) as mock_legacy, \
             patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation_by_session", return_value=1) as mock_action_session, \
             patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation", return_value=False) as mock_action_byid:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    f"/myah/v1/confirm/{stream_id}",
                    json={"choice": "approve"}, headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 200, body
        assert body.get("ok") is True
        mock_legacy.assert_called_once_with(session_key, "approve")
        mock_action_session.assert_called_once_with(session_key, "approve")
        mock_action_byid.assert_not_called()  # confirmation_id branch not taken

    @pytest.mark.asyncio
    async def test_without_confirmation_id_legacy_resolves_first(self):
        """When the legacy queue has a pending entry, resolve it without
        consulting the action queue (preserves legacy single-resolution
        semantics for terminal-command approvals)."""
        adapter = _make_adapter()
        stream_id = "stream-2b"
        session_key = "sess-2b"
        adapter._stream_sessions[stream_id] = session_key

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_legacy, \
             patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation_by_session", return_value=99) as mock_action_session:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    f"/myah/v1/confirm/{stream_id}",
                    json={"choice": "approve"}, headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 200, body
        assert body.get("ok") is True
        mock_legacy.assert_called_once_with(session_key, "approve")
        # Action-queue resolver should NOT be called when legacy already resolved
        mock_action_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_confirmation_id_returns_404(self):
        """resolve_action_confirmation returning False (unknown id) → 404."""
        adapter = _make_adapter()
        stream_id = "stream-3"
        adapter._stream_sessions[stream_id] = "sess-3"

        with patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation", return_value=False):
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    f"/myah/v1/confirm/{stream_id}",
                    json={"confirmation_id": "missing", "choice": "approve"}, headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 404, body
        # error message should hint which queue was searched (debugging aid)
        assert "action" in body.get("error", "").lower() or "confirmation" in body.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_invalid_choice_rejected(self):
        """Choice must be one of approve/approve_session/deny."""
        adapter = _make_adapter()
        stream_id = "stream-4"
        adapter._stream_sessions[stream_id] = "sess-4"

        async with TestClient(TestServer(_make_app(adapter))) as cli:
            resp = await cli.post(
                f"/myah/v1/confirm/{stream_id}",
                json={"confirmation_id": "anything", "choice": "maybe"}, headers=_AUTHED_HEADERS,
            )
            body = await resp.json()

        assert resp.status == 400, body

    @pytest.mark.asyncio
    async def test_no_session_for_stream_returns_404(self):
        """Unknown stream_id with NO confirmation_id → 404 (legacy path only)."""
        adapter = _make_adapter()
        # do NOT populate _stream_sessions

        with patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation") as mock_action, \
             patch("tools.approval.resolve_gateway_approval") as mock_legacy:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    "/myah/v1/confirm/nonexistent",
                    json={"choice": "approve"}, headers=_AUTHED_HEADERS,
                )

        assert resp.status == 404
        mock_action.assert_not_called()
        mock_legacy.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmation_id_resolves_without_stream_session(self):
        """REGRESSION: action confirmations must not require _stream_sessions.

        Production hot-fix 2026-05-06.  Reproduces the exact failure mode
        observed against ``app.myah.dev``:

        1. Agent emits ``tool.confirmation_required`` for a python ``-c``
           exec_approval.
        2. The SSE stream closes (browser tab idle, network blip, or the
           dispatch task's ``finally`` block runs and pops the mapping).
        3. User clicks Approve — frontend POSTs
           ``{run_id, confirmation_id, choice}``.
        4. Old behaviour: handler did ``_stream_sessions.get(stream_id)``
           BEFORE looking at ``confirmation_id`` and returned 404 even
           though ``_action_queues`` still held the pending confirmation.

        Action confirmations are keyed by ``confirmation_id`` in the
        global ``_action_queues``; they have no dependency on the
        per-stream session_key map.  Resolving them must NOT depend on
        the SSE stream still being attached.
        """
        adapter = _make_adapter()
        # Deliberately do NOT populate _stream_sessions — simulates the
        # window between the agent blocking on approval and the user
        # clicking Approve where the dispatch task's finally block has
        # already run, OR an SSE reconnect that lost the mapping.

        with patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation", return_value=True) as mock_action, \
             patch("tools.approval.resolve_gateway_approval") as mock_legacy:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    "/myah/v1/confirm/stream-with-no-session",
                    json={"confirmation_id": "exec-approval-123", "choice": "approve"}, headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 200, body
        assert body.get("ok") is True
        mock_action.assert_called_once_with("exec-approval-123", "approve")
        # Legacy resolver MUST NOT be called when confirmation_id is present.
        mock_legacy.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmation_id_unknown_id_404_even_without_stream_session(self):
        """confirmation_id present but unknown to the action queue → 404.

        Distinct from ``test_no_session_for_stream_returns_404``: this
        case takes the action-queue branch and 404s there, not via the
        ``_stream_sessions`` guard.  The error message must mention the
        confirmation_id (debugging aid)."""
        adapter = _make_adapter()
        # No stream_sessions, no matching confirmation_id in _action_queues.

        with patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation", return_value=False) as mock_action:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    "/myah/v1/confirm/any-stream",
                    json={"confirmation_id": "stale-id", "choice": "deny"}, headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 404, body
        assert "stale-id" in body.get("error", "")
        mock_action.assert_called_once_with("stale-id", "deny")
# ─────────────────────────────────────────────────────────────────


# ── Myah: exec_approval must NOT emit a synthetic confirmation_id ──
#
# Production bug (app.myah.dev, reproduced in OSS): clicking Approve on a
# terminal-command (``exec_approval``) card 404'd and never resolved.
#
# Root cause: ``send_exec_approval`` minted a fresh ``uuid.uuid4().hex``
# and put it on the ``tool.confirmation_required`` event, but that id was
# never registered in ``cron_approval._action_queues``.  The real pending
# entry lives in upstream ``tools.approval._gateway_queues`` keyed by
# ``session_key``.  The frontend echoes whatever ``confirmation_id`` the
# event carried; the confirm endpoint then takes the action-queue branch,
# misses, and 404s — never reaching the legacy session/gateway path that
# would have resolved it.
#
# Fix: ``exec_approval`` events carry NO ``confirmation_id``.  A falsey id
# makes the confirm endpoint skip the action branch and resolve via
# ``_stream_sessions[stream_id] -> resolve_gateway_approval(session_key)``.
# Action confirmations (``send_action_confirmation``) keep their real,
# registered ``confirmation_id`` — unchanged.


class TestExecApprovalNoSyntheticConfirmationId:
    @pytest.mark.asyncio
    async def test_send_exec_approval_emits_no_confirmation_id(self):
        """exec_approval SSE event must not carry a truthy confirmation_id.

        A synthetic, unregistered confirmation_id routes the frontend's
        Approve click into the action-queue branch (which always 404s for
        exec_approval) instead of the legacy session/gateway path.
        """
        adapter = _make_adapter()
        session_key = "sess-exec-1"
        stream_id = "stream-exec-1"
        # Wire a live stream so send_exec_approval doesn't early-return.
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = True

        captured: list[dict] = []
        with patch.object(adapter, "_push_event_sync", side_effect=lambda sid, ev: captured.append(ev)):
            result = await adapter.send_exec_approval(
                chat_id="chat-1",
                command="rm -rf /tmp/x",
                session_key=session_key,
                description="dangerous command",
            )

        assert result.success is True
        assert len(captured) == 1, captured
        event = captured[0]
        assert event["action_type"] == "exec_approval"
        # The crux: no truthy confirmation_id on the event.
        assert not event.get("confirmation_id"), (
            "exec_approval must not emit a synthetic confirmation_id; "
            f"got {event.get('confirmation_id')!r}"
        )

    @pytest.mark.asyncio
    async def test_exec_approval_confirm_resolves_via_session_path(self):
        """End-to-end: an exec_approval-shaped confirm (no confirmation_id)
        resolves through the legacy gateway/session path, returning 200."""
        adapter = _make_adapter()
        session_key = "sess-exec-2"
        stream_id = "stream-exec-2"
        adapter._session_streams[session_key] = stream_id
        adapter._streams[stream_id] = True
        adapter._stream_sessions[stream_id] = session_key

        # 1. Emit the exec_approval event and derive the confirm body the
        #    frontend would send (it echoes the event's confirmation_id,
        #    which the platform drops when falsey).
        captured: list[dict] = []
        with patch.object(adapter, "_push_event_sync", side_effect=lambda sid, ev: captured.append(ev)):
            await adapter.send_exec_approval(
                chat_id="chat-2",
                command="curl https://example.dev/x.png",
                session_key=session_key,
            )
        event = captured[0]
        confirm_body: dict = {"choice": "approve"}
        # Mirror platform openai.py: only forward confirmation_id if truthy.
        if event.get("confirmation_id"):
            confirm_body["confirmation_id"] = event["confirmation_id"]

        # 2. POST the confirm — must resolve via the legacy session path.
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_legacy, \
             patch("myah_hermes_plugin.cron_approval.resolve_action_confirmation") as mock_action_byid:
            async with TestClient(TestServer(_make_app(adapter))) as cli:
                resp = await cli.post(
                    f"/myah/v1/confirm/{stream_id}",
                    json=confirm_body, headers=_AUTHED_HEADERS,
                )
                body = await resp.json()

        assert resp.status == 200, body
        assert body.get("ok") is True
        mock_legacy.assert_called_once_with(session_key, "approve")
        # Action-queue resolver must NOT be consulted for exec_approval.
        mock_action_byid.assert_not_called()
# ─────────────────────────────────────────────────────────────────
