"""
Myah web platform adapter.

Routes messages through the gateway's ``_handle_message()`` pipeline (unlike
the API server which bypasses it), giving Myah access to all slash commands,
agent caching, and session management.

In **hosted** mode the adapter registers HTTP endpoints on the shared
aiohttp Application created by the API server adapter (single-process,
shared port). In **standalone** mode (no API server adapter present) the
adapter spins up its own aiohttp ``AppRunner`` on the configured port —
this is the OSS-Myah path, where the user runs Hermes locally and Myah
talks to it on ``http://localhost:8642`` (or whatever port is configured).

Endpoints:
    POST /myah/v1/message            — dispatch a message or slash command
    GET  /myah/v1/events/{stream_id} — SSE event stream
    POST /myah/v1/confirm/{stream_id}— resolve pending approval
    POST /myah/v1/secret/{stream_id} — receive a secret value from the frontend
    GET  /myah/v1/media              — stream a cached media file
    POST /myah/v1/aux/{task}         — auxiliary LLM call passthrough
    GET  /myah/health                — health check
    /myah/v1/admin/*                  — runtime control surface (see runtime_admin)

Requires: aiohttp (provided by gateway dependencies)
"""

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]



from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.config import Platform, PlatformConfig

# ── Myah: attachments support ──────────────────────────────────────────────
import mimetypes as _myah_mimetypes
import os as _myah_os
from pathlib import Path as _myah_Path
try:
    import aiohttp as _myah_aiohttp
    _MYAH_AIOHTTP_AVAILABLE = True
except ImportError:
    _myah_aiohttp = None  # type: ignore[assignment]
    _MYAH_AIOHTTP_AVAILABLE = False
from gateway.platforms.base import (
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
)

# Plugin-owned aiohttp runner (Tier 2A Task 2A.3).
from myah_hermes_plugin.myah_platform.standalone_runner import MyahStandaloneRunner

from ._runner_state import (
    get_cached_agent_attribution_direct,
    get_session_override_direct,
    set_session_override_direct,
)

_MYAH_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # Myah: per-attachment cap (defense-in-depth)
_MYAH_PLATFORM_BASE_URL = _myah_os.environ.get('MYAH_PLATFORM_BASE_URL')  # Myah: platform URL
_MYAH_PLATFORM_BEARER = _myah_os.environ.get('MYAH_PLATFORM_BEARER')      # Myah: shared bearer


def _load_cron_jobs_safely() -> list:
    """Load cron jobs from jobs.json without raising.

    Indirection point for the cron Path A fallback recovery (Task 1.5 /
    spec §6.1). Tests patch this symbol; production reads from upstream
    ``cron.jobs.load_jobs``.

    Returns an empty list if upstream isn't importable or the read
    raises — the recovery path treats absence of jobs as "no signal"
    rather than crashing the send() call.
    """
    try:
        from cron.jobs import load_jobs  # type: ignore
        jobs = load_jobs() or []
        return [j for j in jobs if isinstance(j, dict)]
    except Exception:
        return []


def _cron_last_run_sort_key(job: dict) -> tuple:
    """Sort key for cron jobs by ``last_run_at``, defensive against mixed types.

    Upstream ``cron/jobs.py:872`` writes ``last_run_at`` as an ISO 8601
    string (``_hermes_now().isoformat()``), and newly-created jobs at
    ``cron/jobs.py:655`` start with ``last_run_at: None``. The pre-fix
    sort key ``job.get("last_run_at") or float("-inf")`` raised
    ``TypeError: '<' not supported between instances of 'str' and 'float'``
    when both shapes appeared in the same sort. Caught by reviewer of
    PR #3 (2026-05-19).

    The returned tuple ensures:

    1. Jobs with ``last_run_at`` set always rank above ``None``-valued
       jobs (the boolean primary key), regardless of how ``str()``
       coerces non-ISO inputs. This is the critical correctness property
       for the ``reverse=True`` sort caller.
    2. Among jobs that have a value, ISO 8601 strings sort
       lexicographically in chronological order (the format is designed
       for this) — newer wins under ``reverse=True``.
    3. If anyone ever passes a numeric ``last_run_at`` (none today, but
       the tests historically used ``1700000000.0``), ``str()`` coerces
       it to a digit string that still compares sensibly within a
       single same-shape sort.
    """
    value = job.get("last_run_at")
    return (value is not None, str(value) if value is not None else "")


def _recover_cron_job_id_from_session_key() -> str:
    """Read the current session key and parse a cron job_id from it.

    The upstream scheduler builds session keys as
    ``f"cron_{job_id}_{strftime}"`` at ``cron/scheduler.py:1319``.
    The ``job_id`` portion is ``uuid.uuid4().hex[:12]`` (12 hex chars,
    no underscores); the ``strftime`` portion contains underscores so we
    cannot split on the last underscore. Take index 1 of a split.

    Returns ``""`` when the key is empty, not cron-shaped, or malformed.
    """
    try:
        from myah_hermes_plugin.cron_approval import _get_current_session_key
        key = _get_current_session_key() or ''
    except Exception:
        return ''
    if not key.startswith('cron_'):
        return ''
    parts = key.split('_', 2)  # ['cron', job_id, 'strftime…']
    if len(parts) < 2:
        return ''
    candidate = parts[1]
    # job_id is uuid.uuid4().hex[:12] → 12 hex chars. Defensive shape
    # check so we don't mis-classify a malformed key.
    if not candidate or not all(c in '0123456789abcdef' for c in candidate):
        return ''
    return candidate


_ID_LIKE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _clean_observability_id(value: str) -> Optional[str]:
    """Return an opaque ID-like value, or None for sensitive/free-form text."""
    candidate = (value or "").strip()
    if not candidate:
        return None
    if not _ID_LIKE_VALUE_RE.fullmatch(candidate):
        return None
    return candidate
# ────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

_MAX_CONCURRENT_STREAMS = 20
_STREAM_TTL = 600  # seconds — orphaned streams cleaned up after this
_KEEPALIVE_INTERVAL = 30  # seconds between SSE keepalive comments


def check_myah_requirements() -> bool:
    """Check if Myah adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


def _extract_visible_message_content(response: Any) -> str:
    """Return only assistant-visible ``message.content`` from an LLM response.

    Myah's title/follow-up aux endpoint produces strict, user-facing metadata.
    Structured reasoning fields are internal scratch space and must never be
    promoted into ``choices[0].message.content`` for these tasks.
    """
    import re

    def _strip_inline_reasoning(text: str) -> str:
        return re.sub(
            r'<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>'
            r'.*?'
            r'</(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>',
            '',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()

    def _visible_text_from_part(part: Any) -> str:
        if isinstance(part, str):
            return part
        if not isinstance(part, dict):
            return ''

        part_type = str(part.get('type') or '').lower()
        if part_type not in {'text', 'output_text'}:
            return ''

        text = part.get('text')
        if isinstance(text, str):
            return text

        # Some SDK content parts use nested shapes such as
        # {'type': 'text', 'text': {'value': '...'}}.  Keep only explicit text
        # fields and avoid stringifying arbitrary objects, which can leak
        # reasoning/internal metadata as a Python repr.
        if isinstance(text, dict):
            value = text.get('value')
            if isinstance(value, str):
                return value

        return ''

    try:
        msg = response.choices[0].message
    except Exception:
        return ''

    raw = getattr(msg, 'content', None) or ''
    if isinstance(raw, str):
        visible = raw
    elif isinstance(raw, (list, tuple)):
        visible = ''.join(_visible_text_from_part(part) for part in raw)
    else:
        visible = ''

    return _strip_inline_reasoning(visible)


# ── Myah: plugin version source ────────────────────────────────────────────
# The platform's OSS first-run probe (D4 in vm-testing-followups.md) reads
# this via ``GET /myah/health`` to surface plugin-version diagnostics.
#
# Source-of-truth ordering:
#   1. ``importlib.metadata.version('myah-hermes-plugin')`` — what pip
#      actually installed. Always matches ``pyproject.toml``'s
#      ``[project] version`` (1.1.0 today).
#   2. ``myah_hermes_plugin.__version__`` — fallback when metadata is
#      missing (editable install with no wheel built yet). Currently
#      ``0.3.0`` — diverges from pyproject. Aligning the three sources
#      (``pyproject.toml`` / ``plugin.yaml`` / ``__init__.__version__``)
#      is a plugin-release-process decision and intentionally out-of-scope
#      for this fix; the health endpoint just reports what's actually
#      loaded.
#   3. ``"unknown"`` — last-ditch safety, never None (the platform probe
#      uses ``r.json().get("version")`` so None would round-trip back to
#      the regression we're fixing).
def _plugin_version() -> str:
    """Return the running plugin's version string."""
    try:
        from importlib.metadata import PackageNotFoundError, version as _v
        return _v('myah-hermes-plugin')
    except PackageNotFoundError:
        pass
    except Exception:
        pass
    try:
        from myah_hermes_plugin import __version__ as _v
        if _v:
            return str(_v)
    except Exception:
        pass
    return 'unknown'
# ────────────────────────────────────────────────────────────────────────────


# ── Myah: mime/extension helper ────────────────────────────────────────────
def _myah_ext(mime: str, filename: str, default: str) -> str:
    """Return a file extension (with leading dot) from MIME, filename, or default."""
    if filename and '.' in filename:
        ext = '.' + filename.rsplit('.', 1)[1].lower()
        if 2 <= len(ext) <= 6:
            return ext
    guessed = _myah_mimetypes.guess_extension(mime) if mime else None
    return guessed or default
# ────────────────────────────────────────────────────────────────────────────


# Late-bound pointer to the most-recently-constructed MyahAdapter. Used
# by the plugin's global secret-capture wrapper to dispatch into the
# active adapter — see myah_platform/__init__._wire_secret_capture_callback
# for the contract. Set in MyahAdapter.__init__; never cleared.
_LATEST_ADAPTER: Optional["MyahAdapter"] = None


class MyahAdapter(BasePlatformAdapter):
    """
    Gateway platform adapter for the Myah web frontend.

    Messages flow through the gateway's full _handle_message() pipeline,
    which provides slash command dispatch, session management, agent
    caching, voice transcription, image analysis — everything that
    Telegram/Discord/Slack adapters get automatically.

    Responses are delivered via SSE streams. Each POST /myah/v1/message
    returns a stream_id; the frontend subscribes to
    GET /myah/v1/events/{stream_id} for real-time events.
    """

    CONTENTFUL_STREAM_EVENTS = frozenset({
        "message.delta",
        "tool.started",
        "tool.completed",
        "tool.confirmation_required",
        "clarify.required",
        "clarify.resolved",
        "secret.required",
        "secret.resolved",
        "reasoning.delta",
        "reasoning.available",
        "status",
    })

    def __init__(self, config: PlatformConfig):
        # ``Platform.MYAH`` was removed from the core enum in Phase 4d
        # (2026-05-04). ``Platform("myah")`` resolves through the enum's
        # ``_missing_`` hook to a cached pseudo-member with value="myah",
        # mirroring how ``IRCAdapter`` constructs its platform identifier.
        super().__init__(config, Platform("myah"))
        # Late-bound module-level pointer used by the plugin's global
        # secret-capture wrapper (myah_platform/__init__._wire_secret_capture_callback)
        # to dispatch tools/skills_tool.set_secret_capture_callback's
        # vanilla-shaped (name, prompt, metadata) call into this
        # adapter's per-stream _secret_capture_callback. Single-adapter
        # / single-process assumption — fine for hosted Myah's per-user
        # container model and fine for OSS single-tenant; multi-tenant
        # in-process would need a contextvar-keyed lookup instead.
        global _LATEST_ADAPTER
        _LATEST_ADAPTER = self
        # Auth key resolution order: config.extra.auth_key (yaml) →
        # MYAH_ADAPTER_AUTH_KEY env (legacy hosted-mode path that used to
        # be wired up by the deleted ``_apply_env_overrides`` block in
        # ``gateway/config.py``).
        _extra = config.extra or {}
        self._auth_key: str = _extra.get("auth_key") or _myah_os.environ.get(
            "MYAH_ADAPTER_AUTH_KEY", ""
        )
        # Standalone-mode runtime state — populated lazily in connect() when
        # there's no shared aiohttp app to attach to.
        self._standalone_mode: bool = False
        self._own_app: Optional["web.Application"] = None
        self._own_runner: Optional["web.AppRunner"] = None
        self._own_site: Optional["web.TCPSite"] = None
        # Port resolution order: config.extra.port (yaml) → MYAH_ADAPTER_PORT
        # env → MYAH_GATEWAY_PORT env (Tier 2A Task 2A.3) → 8643 default.
        from myah_hermes_plugin.myah_platform.standalone_runner import (
            resolve_default_port as _myah_resolve_default_port,
        )

        _port_str = str(_extra.get("port") or _myah_os.environ.get("MYAH_ADAPTER_PORT", ""))
        try:
            self._port: int = int(_port_str) if _port_str else _myah_resolve_default_port()
        except ValueError:
            self._port = _myah_resolve_default_port()

        # ── Stream state ────────────────────────────────────────────────
        # stream_id → asyncio.Queue of SSE event dicts (None = sentinel)
        self._streams: Dict[str, asyncio.Queue] = {}
        # stream_id → creation timestamp (for TTL sweep)
        self._streams_created: Dict[str, float] = {}

        # ── Dual session mapping (Fix 1) ────────────────────────────────
        # The gateway calls adapter.send(chat_id=source.chat_id) where
        # chat_id is the RAW session_id from the source.  But the
        # approval system uses the FULL session_key (like
        # "agent:main:myah:dm:{session_id}").  We maintain two maps:
        #
        #   _session_streams  : session_key → stream_id  (for approvals)
        #   _chat_id_streams  : raw chat_id → stream_id  (for send/send_typing)
        self._session_streams: Dict[str, str] = {}
        self._chat_id_streams: Dict[str, str] = {}

        # stream_id → session_key (reverse lookup for confirm endpoint)
        self._stream_sessions: Dict[str, str] = {}
        # raw chat_id -> latest platform assistant message_id. Used by the
        # durable final-message fallback when the live SSE queue disappears.
        self._chat_id_message_ids: Dict[str, str] = {}

        # Pending secret captures: stream_id → { event: threading.Event, result: dict }
        self._pending_secrets: Dict[str, Dict] = {}
        # Pending clarify prompts: stream_id → clarify_id → metadata. The
        # blocking primitive lives in upstream tools.clarify_gateway; this map
        # lets the HTTP endpoint validate stream/request ownership before
        # resolving the upstream waiting Event.
        self._pending_clarifies: Dict[str, Dict[str, Dict[str, Any]]] = {}

        # ── Thread safety (Fix 2) ──────────────────────────────────────
        # Captured in connect() so callbacks from the agent worker thread
        # can safely push events to asyncio.Queue via call_soon_threadsafe.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── GatewayRunner backref ─────────────────────────────────────
        # ``GatewayRunner._create_adapter`` sets this for *built-in* adapters
        # (Discord, Webhook) but NOT for plugin-registered platforms — see
        # ``gateway/run.py:4592-4594`` where ``platform_registry.create_adapter()``
        # is returned directly without ``adapter.gateway_runner = self``. As a
        # result, on stock hermes (both this branch and upstream pip-installed)
        # the attribute stays ``None`` indefinitely, silently disabling:
        #   - Phase B model override (``_handle_message_endpoint`` line ~354)
        #   - per-message model attribution (``_dispatch_message`` finally)
        #   - Phase F ``pre_llm_call`` hook (streaming_callbacks.py)
        #
        # The plugin self-discovers the runner via the module-level weakref
        # ``gateway.run._gateway_runner_ref`` upstream exposes (lines 1109/1206
        # of run.py). Same direct-access pattern Tier 2B established for
        # ``_session_model_overrides`` / ``_agent_cache``. Removable when
        # upstream sets ``gateway_runner`` on plugin adapters too — then
        # ``self.gateway_runner`` becomes the cached fast-path.
        self.gateway_runner = None

        # ── Phase F: plugin-side structured-streaming workaround ─────
        # Reverse mapping populated in _handle_message_endpoint so the
        # pre_llm_call hook can resolve session_key from chat_id.
        self._chat_id_session_keys: Dict[str, str] = {}
        # Set of session_keys for which our pre_llm_call hook installed
        # structured streaming callbacks. send() consults this set to
        # suppress the gateway's final "send full response" duplicate.
        self._native_streaming_used: set[str] = set()
        # Set of session_keys for which stream_delta callback actually
        # fired (i.e. tokens were streamed). The post_llm_call hook in
        # streaming_callbacks.py reads this to detect the gateway's
        # response-suppression bug (gateway/run.py:14701 drops the
        # agent's failed=True flag → gateway/run.py:15326 suppresses
        # send-final → user sees "Thinking..." forever). When stream
        # never fired but post_llm_call has a final response, the hook
        # emits the response as a synthetic message.delta event so the
        # user sees the error message inline.
        self._stream_delta_invoked: set[str] = set()
        # Set of stream_ids that received visible stream activity:
        # message.delta text, structured tool rows, reasoning/status rows,
        # slash command output via adapter.send, cron live-preview, etc.
        # Tracked at push time so all paths get unified accounting. The
        # gateway-suppression workaround reads this to avoid replacing an
        # already-useful Myah timeline with a generic provider-warning fallback.
        self._stream_had_content: set[str] = set()
        # ─────────────────────────────────────────────────────────────

        # ── Route registration state ──────────────────────────────────
        # Tier 2A Task 2A.3 (2026-05-07): the adapter ALWAYS runs in
        # standalone mode now — it owns its own ``aiohttp.AppRunner``
        # via :class:`MyahStandaloneRunner`.  No shared app, no
        # ``register_pre_setup_hook``, no dependency on
        # ``gateway/platforms/api_server.py`` internals.  See
        # ``docs/superpowers/specs/2026-05-06-myah-oss-completion-design.md``
        # §3 Task 2A.3.
        self._routes_registered = False
        self._runner_helper: Optional["MyahStandaloneRunner"] = None

    # ── Runner self-discovery ───────────────────────────────────────────

    def _resolve_runner(self):
        """Return the active ``GatewayRunner`` or ``None``.

        Fast path: ``self.gateway_runner`` was set externally (built-in
        adapters get this from ``_create_adapter``; future hermes versions
        may set it for plugin adapters too).

        Fallback: read ``gateway.run._gateway_runner_ref`` — a module-level
        weakref upstream populates at ``gateway/run.py:1206`` whenever the
        ``GatewayRunner.__init__`` runs. Once resolved, cache the ref on
        ``self.gateway_runner`` so subsequent calls take the fast path.

        Returns ``None`` if no gateway is running (e.g. plugin imported in
        isolation by a unit test). Callers must handle ``None`` gracefully
        — same contract as the original ``self.gateway_runner`` attribute.
        """
        if self.gateway_runner is not None:
            return self.gateway_runner
        try:
            from gateway.run import _gateway_runner_ref as _ref
        except ImportError:
            return None
        try:
            runner = _ref()
        except Exception:
            return None
        if runner is not None:
            # Cache so the next call short-circuits — same lifecycle as the
            # GatewayRunner instance (a new runner replaces _gateway_runner_ref
            # before this adapter could be re-used in the same process).
            self.gateway_runner = runner
        return runner

    # ── Auth ────────────────────────────────────────────────────────────

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """Validate Bearer token. Returns None if OK, an error response otherwise.

        Three states:

        - ``_auth_key`` is unset → return 503 with an actionable error message.
          Previously this returned ``None`` ("allow all"), which silently
          turned every authed endpoint into an open relay. Anyone who
          could reach the adapter's port (``MYAH_GATEWAY_PORT``, default
          8643) could call ``/myah/v1/admin/*`` without credentials.
          Fail-closed is correct: the routes still bind so platform-side
          probes can see ``plugin_installed=true``, but no admin or chat
          surface is reachable until the operator wires the bearer token.

        - Bearer token present + matches ``_auth_key`` → return ``None``
          (auth passed; handler continues).

        - Anything else (wrong token, missing header, non-Bearer scheme) →
          return 401.

        ``/myah/health`` is registered without going through this gate so
        the platform's OSS first-run probe can still surface a useful
        diagnostic when the bearer isn't set.
        """
        if not self._auth_key:
            return web.json_response(
                {
                    "error": "Myah plugin auth not configured",
                    "detail": (
                        "MYAH_ADAPTER_AUTH_KEY is unset in ~/.hermes/.env. "
                        "Run scripts/setup-myah-oss.sh in your Myah platform "
                        "clone to generate one and sync it to both sides, "
                        "then restart hermes."
                    ),
                },
                status=503,
            )

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, self._auth_key):
                return None

        return web.json_response(
            {"error": "Invalid or missing auth token"},
            status=401,
        )

    # ── SSE helpers ─────────────────────────────────────────────────────

    def _push_event(self, stream_id: str, event: Dict[str, Any]) -> None:
        """Thread-safe push of an event dict to a stream's queue.

        Uses call_soon_threadsafe (Fix 2) because the agent runs in a
        worker thread (via run_in_executor) and callbacks fire from
        that thread.  asyncio.Queue is NOT thread-safe for cross-thread
        put_nowait calls.
        """
        q = self._streams.get(stream_id)
        if q is None:
            return
        if event.get("event") in self.CONTENTFUL_STREAM_EVENTS:
            self._stream_had_content.add(stream_id)
        try:
            self._loop.call_soon_threadsafe(q.put_nowait, event)
        except RuntimeError:
            pass  # Event loop closed

    def _push_event_sync(self, stream_id: str, event: Dict[str, Any]) -> None:
        """Direct push — only safe from the event loop thread."""
        q = self._streams.get(stream_id)
        if q is None:
            return
        # Track that a user-visible content event was delivered for this
        # stream so the suppression-bug workaround in _dispatch_message's
        # finally can distinguish "real failure with no output" from
        # "slash command response delivered via adapter.send()". Without
        # this tracking, slash commands like /model would falsely trigger
        # the gateway-suppression warning because their response is
        # emitted via send() → _push_event_sync directly, bypassing the
        # Mark any user-visible stream activity so the final fallback does not
        # replace detailed tool/status UI with a generic Working/error message.
        if event.get("event") in self.CONTENTFUL_STREAM_EVENTS:
            self._stream_had_content.add(stream_id)
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    # ── HTTP endpoint handlers ──────────────────────────────────────────

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /myah/health — health check.

        Includes the plugin's installed version so the platform's OSS
        first-run probe (D4 in docs/oss-launch/vm-testing-followups.md)
        can surface a "plugin too old" diagnostic instead of an opaque
        ``plugin_version: null``. The version is resolved from the
        installed wheel's metadata via ``importlib.metadata`` — this is
        the same source ``pip show`` reads, so it always matches what
        pip actually installed (currently ``1.1.0`` per ``pyproject.toml``).

        Falls back to ``myah_hermes_plugin.__version__`` if metadata is
        missing (editable install with no wheel built yet). Note that
        ``plugin.yaml`` and ``__init__.py`` may disagree in the source
        tree — version-string alignment is a release-process concern,
        not something this endpoint can fix; we report whatever the
        running interpreter actually loaded.
        """
        return web.json_response({
            "status": "ok" if self._running else "disconnected",
            "platform": "myah",
            "streams_active": len(self._streams),
            "version": _plugin_version(),
        })

    async def _handle_message_endpoint(self, request: "web.Request") -> "web.Response":
        """POST /myah/v1/message — dispatch a message or slash command.

        Returns 202 with {stream_id} immediately.  The frontend subscribes
        to /myah/v1/events/{stream_id} for the response.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Enforce concurrency limit
        if len(self._streams) >= _MAX_CONCURRENT_STREAMS:
            return web.json_response(
                {"error": f"Too many concurrent streams (max {_MAX_CONCURRENT_STREAMS})"},
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        message = (body.get("message") or "").strip()
        if not message:
            return web.json_response({"error": "Missing 'message' field"}, status=400)

        session_id = (body.get("session_id") or "").strip() or str(uuid.uuid4())
        user_name = body.get("user_name")
        user_id = body.get("user_id")
        chat_name = body.get("chat_name")
        message_id = _clean_observability_id(str(body.get("message_id") or ""))

        # ── Myah: one-shot session-scoped model override (T3-932) ────
        # If the client supplies a 'model' field, apply it via
        # set_session_override_direct(runner, ...) BEFORE dispatching the
        # message so the gateway picks it up when constructing/resolving
        # the agent. This is the inline equivalent of calling
        # PUT /myah/api/sessions/{id}/model, useful for
        # "regenerate with different model" flows.
        _override_model = (body.get("model") or "").strip()
        # Optional 'provider' tag from the platform — when present it pins
        # the target provider so switch_model skips auto-detect. Needed for
        # OAuth-only providers (openai-codex, anthropic-claude-code) where
        # the env-var heuristic PROVIDER_REGISTRY uses would otherwise fall
        # back to OpenRouter on the very first message of a new chat.
        _override_provider = (body.get("provider") or "").strip()
        # ────────────────────────────────────────────────────────────

        # Create the SSE stream
        stream_id = f"myah_{uuid.uuid4().hex}"
        q: asyncio.Queue = asyncio.Queue()
        self._streams[stream_id] = q
        self._streams_created[stream_id] = time.time()

        # Build source and compute session key BEFORE spawning the task
        source = self.build_source(
            chat_id=session_id,
            chat_name=chat_name,
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
            message_id=message_id or None,
        )

        # Compute the full session key the same way the gateway does
        from gateway.session import build_session_key
        session_key = build_session_key(
            source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

        # Phase F: stash chat_id → session_key for the pre_llm_call hook
        self._chat_id_session_keys[session_id] = session_key

        # ── Myah: apply one-shot model override if present ───────────
        runner = self._resolve_runner()
        if _override_model and runner is not None:
            try:
                from hermes_cli.model_switch import switch_model
                # Load current config for switch_model context
                import yaml as _yaml
                from hermes_constants import get_hermes_home as _ghm
                _cfg_path = _ghm() / "config.yaml"
                _cfg = {}
                if _cfg_path.exists():
                    try:
                        _cfg = _yaml.safe_load(_cfg_path.read_text()) or {}
                    except Exception:
                        _cfg = {}
                _mc = _cfg.get("model", {}) if isinstance(_cfg.get("model"), dict) else {}
                _current_model = _mc.get("default") or (_cfg.get("model") if isinstance(_cfg.get("model"), str) else "")
                _current_provider = _mc.get("provider", "") or "openrouter"
                _current_base_url = _mc.get("base_url", "") or ""
                # Layer current session override on top if present
                _existing_override = get_session_override_direct(runner, session_key) or {}
                if _existing_override:
                    _current_model = _existing_override.get('model', _current_model)
                    _current_provider = _existing_override.get('provider', _current_provider)
                    _current_base_url = _existing_override.get('base_url', _current_base_url)

                logger.info(
                    "[myah-modelswitch] requesting switch session=%s "
                    "raw_input=%r explicit_provider=%r current_provider=%r "
                    "current_model=%r",
                    session_key, _override_model, _override_provider,
                    _current_provider, _current_model,
                )
                _result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: switch_model(
                        raw_input=_override_model,
                        current_provider=_current_provider,
                        current_model=_current_model,
                        current_base_url=_current_base_url,
                        current_api_key="",
                        is_global=False,
                        explicit_provider=_override_provider,  # Myah: pin provider when platform supplies it
                        user_providers=_cfg.get("providers"),
                        custom_providers=_cfg.get("custom_providers"),
                    ),
                )
                logger.info(
                    "[myah-modelswitch] switch_model result session=%s "
                    "success=%s new_model=%r target_provider=%r api_mode=%r "
                    "error=%r",
                    session_key,
                    getattr(_result, "success", None),
                    getattr(_result, "new_model", None),
                    getattr(_result, "target_provider", None),
                    getattr(_result, "api_mode", None),
                    getattr(_result, "error_message", None),
                )
                if getattr(_result, "success", False):
                    # set_session_override evicts the cached agent atomically.
                    set_session_override_direct(runner, session_key, {
                        "model": _result.new_model,
                        "provider": _result.target_provider,
                        "api_key": getattr(_result, "api_key", "") or "",
                        "base_url": getattr(_result, "base_url", "") or "",
                        "api_mode": getattr(_result, "api_mode", "") or "",
                    })
                    logger.info(
                        "[myah-modelswitch] override written session=%s "
                        "model=%s provider=%s",
                        session_key, _result.new_model, _result.target_provider,
                    )
                    # ── Myah: heal auth.json + .env via vanilla-style provider sync ────
                    # Synchronously updating auth.json:active_provider (and writing
                    # OPENROUTER_API_KEY to .env when applicable) whenever the user
                    # chats fixes the production case where state is stale or missing.
                    # Vanilla rule: PROVIDER_REGISTRY providers set active_provider=<id>;
                    # non-registry providers (openrouter) set active_provider=None and
                    # rely on the .env env-var fallback branch in resolve_provider("auto").
                    # See myah_hermes_plugin/_provider_sync.py for the full rationale.
                    _new_provider = (_result.target_provider or "").strip()
                    if _new_provider:
                        try:
                            from myah_hermes_plugin._provider_sync import sync_provider_state
                            _sync_result = sync_provider_state(
                                _new_provider,
                                api_key=getattr(_result, "api_key", "") or None,
                            )
                            logger.info(
                                "[myah] chat heal complete for session %s: %s",
                                session_key,
                                _sync_result,
                            )
                        except Exception:
                            logger.debug(
                                "[myah] chat-side provider sync failed (non-fatal)",
                                exc_info=True,
                            )
                    # ────────────────────────────────────────────────────────────────────
                else:
                    # ── Myah: clear stale override + surface failure (Bug B follow-up) ──
                    _err_msg = getattr(_result, "error_message", "unknown")
                    logger.warning(
                        f"[myah] one-shot model override failed for session {session_key}: {_err_msg}"
                    )
                    # Direct-access pattern (Tier 2B.0): same defensive style as
                    # _runner_state.py helpers.
                    try:
                        _overrides = getattr(runner, "_session_model_overrides", None)
                        if _overrides is not None:
                            _overrides.pop(session_key, None)
                    except Exception:
                        logger.debug(
                            "[myah] failed to clear session override for %s",
                            session_key,
                            exc_info=True,
                        )

                    # ── OSS Issue #2: graceful fallback ──
                    # In OSS mode the platform's user.default_model can be a
                    # model name hermes can't resolve (e.g. openai/gpt-4o-mini
                    # when the user only has openrouter+opencode-go pool).
                    # Returning 400 fails the entire chat turn, which is too
                    # strict — the user just wants their message answered by
                    # whatever default the agent has. Fall through and let
                    # the agent run with its existing default (override has
                    # been cleared above so this is safe).
                    import os as _os
                    _is_oss = _os.environ.get("MYAH_DEPLOYMENT_MODE", "").strip().lower() == "oss"
                    if _is_oss:
                        logger.info(
                            "[myah] OSS mode: falling back to agent default after "
                            "unresolvable model %r (error: %s)",
                            _override_model, _err_msg,
                        )
                        # Don't return — fall through to dispatch
                    else:
                        # Hosted: preserve strict 400-on-failure behaviour
                        self._streams.pop(stream_id, None)
                        self._streams_created.pop(stream_id, None)
                        # Phase F: dispatch never spawns, so finally cleanup
                        # never fires; pop the session-key map entry here.
                        self._chat_id_session_keys.pop(session_id, None)
                        return web.json_response(
                            {"error": f"Failed to switch to model {_override_model}: {_err_msg}"},
                            status=400,
                        )
                    # ────────────────────────────────────────
            except Exception:
                logger.exception("[myah] one-shot model override error")
        # ────────────────────────────────────────────────────────────

        # Dual mapping (Fix 1): map both the raw chat_id and full session_key
        self._chat_id_streams[session_id] = stream_id
        self._session_streams[session_key] = stream_id
        self._stream_sessions[stream_id] = session_key
        if message_id:
            self._chat_id_message_ids[session_id] = message_id

        # ── Myah: media attachments ingestion ──────────────────────────────
        attachments = body.get('attachments') or []
        _myah_media_urls: list = []
        _myah_media_types: list = []
        _myah_msg_type = MessageType.TEXT  # will be upgraded by mime routing below

        if attachments and not (_MYAH_PLATFORM_BASE_URL and _MYAH_PLATFORM_BEARER):
            return web.json_response(
                {'error': 'Adapter missing MYAH_PLATFORM_BASE_URL / MYAH_PLATFORM_BEARER env'},
                status=500,
            )

        if attachments:
            async def _fetch_one(att: dict) -> tuple:
                file_id = att.get('file_id')
                filename = att.get('filename') or 'attachment'
                declared_mime = (att.get('mime_type') or 'application/octet-stream').lower()
                declared_size = int(att.get('size') or 0)
                if not file_id:
                    raise ValueError(f"Attachment '{filename}' missing file_id")
                if declared_size > _MYAH_MAX_ATTACHMENT_BYTES:
                    raise ValueError(
                        f'{filename} exceeds {_MYAH_MAX_ATTACHMENT_BYTES // (1024*1024)} MB limit'
                    )
                fetch_url = (
                    f"{_MYAH_PLATFORM_BASE_URL.rstrip('/')}"
                    f"/api/v1/files/{file_id}/content"
                )
                timeout = _myah_aiohttp.ClientTimeout(total=30)
                async with _myah_aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(
                        fetch_url,
                        headers={'Authorization': f'Bearer {_MYAH_PLATFORM_BEARER}'},
                    ) as r:
                        if r.status != 200:
                            raise ValueError(
                                f'Platform returned {r.status} for {filename}'
                            )
                        raw = await r.read()
                if len(raw) > _MYAH_MAX_ATTACHMENT_BYTES:
                    raise ValueError(
                        f'{filename} body exceeds {_MYAH_MAX_ATTACHMENT_BYTES // (1024*1024)} MB'
                    )
                return raw, declared_mime, filename

            try:
                fetched = await asyncio.gather(
                    *(_fetch_one(a) for a in attachments), return_exceptions=False,
                )
            except Exception as _att_err:
                return web.json_response(
                    {'error': f'Attachment fetch failed: {_att_err}'},
                    status=502,
                )

            for _raw, _mime, _fname in fetched:
                if _mime.startswith('image/'):
                    _ext = _myah_ext(_mime, _fname, '.jpg')
                    _cached = cache_image_from_bytes(_raw, ext=_ext)
                    if _myah_msg_type == MessageType.TEXT:
                        _myah_msg_type = MessageType.PHOTO
                elif _mime.startswith('audio/'):
                    _ext = _myah_ext(_mime, _fname, '.ogg')
                    _cached = cache_audio_from_bytes(_raw, ext=_ext)
                    if _myah_msg_type == MessageType.TEXT:
                        _myah_msg_type = MessageType.VOICE
                else:
                    _cached = cache_document_from_bytes(_raw, _fname)
                    if _myah_msg_type == MessageType.TEXT:
                        _myah_msg_type = MessageType.DOCUMENT
                _myah_media_urls.append(_cached)
                _myah_media_types.append(_mime)
        # ────────────────────────────────────────────────────────────────────

        # Build the message event
        msg_type = MessageType.COMMAND if message.startswith('/') else _myah_msg_type  # Myah: upgraded by attachments
        event = MessageEvent(
            text=message,
            message_type=msg_type,
            source=source,
            message_id=stream_id,
            media_urls=_myah_media_urls,    # Myah: propagate attachments
            media_types=_myah_media_types,  # Myah: propagate attachments
        )

        # Dispatch in background — the gateway's handle_message spawns its
        # own background task, so we wrap to capture completion/failure.
        task = asyncio.create_task(self._dispatch_message(
            event, stream_id, session_id, session_key,
        ))
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        return web.json_response(
            {"stream_id": stream_id, "session_id": session_id},
            status=202,
        )

    def _coerce_positive_float(self, value: Any) -> Optional[float]:
        """Return value as a positive float, or None when unset/invalid."""
        if value is None:
            return None
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    def _get_long_run_status_interval_seconds(self) -> float:
        """Diagnostic interval for long dispatch waits; never a completion cap."""
        extra = self.config.extra or {}
        candidates = (
            extra.get("long_run_status_interval"),
            _myah_os.environ.get("MYAH_LONG_RUN_STATUS_INTERVAL"),
            extra.get("gateway_timeout"),
            extra.get("agent_gateway_timeout"),
            _myah_os.environ.get("HERMES_AGENT_TIMEOUT"),
        )
        for candidate in candidates:
            parsed = self._coerce_positive_float(candidate)
            if parsed is not None:
                return parsed
        return 1800.0

    async def _wait_for_session_completion(self, *, session_key: str, stream_id: str) -> None:
        """Wait until the gateway removes ``session_key`` from active sessions.

        The diagnostic interval only controls rate-limited logging. It must not
        terminate the stream or cause ``run.completed`` while Hermes is still
        running.
        """
        # ``handle_message`` schedules background work; give it a few
        # event-loop turns to publish ``_active_sessions`` before deciding
        # there is nothing to wait for. This is a grace window, not a runtime
        # cap; once active, the loop below waits until Hermes really clears it.
        for _ in range(3):
            if session_key in self._active_sessions:
                break
            await asyncio.sleep(0)
        interval = self._get_long_run_status_interval_seconds()
        next_log_at = time.monotonic() + interval
        while session_key in self._active_sessions:
            now = time.monotonic()
            if now >= next_log_at:
                logger.info(
                    "[myah] dispatch still running session=%s stream=%s active_for>=%.0fs",
                    session_key,
                    stream_id,
                    interval,
                )
                next_log_at = now + interval
            await asyncio.sleep(0.1)

    async def _dispatch_message(
        self,
        event: MessageEvent,
        stream_id: str,
        chat_id: str,
        session_key: str,
    ) -> None:
        """Dispatch a message through the gateway pipeline and emit run events.

        The gateway's _handle_message() returns the final response text (or
        None if the adapter already sent it via send()).  We emit a
        run.completed event when done, or run.failed on error.
        """
        terminal_event_sent = False
        cancelled = False
        try:
            if not self._message_handler:
                self._push_event_sync(stream_id, {
                    "event": "run.failed",
                    "stream_id": stream_id,
                    "run_id": stream_id,
                    "timestamp": time.time(),
                    "error": "No message handler registered (gateway not ready)",
                })
                terminal_event_sent = True
                return

            # The gateway's handle_message() calls _process_message_background
            # which calls _message_handler (the GatewayRunner._handle_message).
            # That method returns the final response OR None if already sent.
            #
            # BUT: BasePlatformAdapter.handle_message() is what we should call
            # because it manages session locking, interrupt events, and pending
            # messages.  It spawns its own background task via
            # _process_message_background which calls _message_handler and then
            # adapter.send() with the response.
            #
            # So we call handle_message() and let the base class manage
            # the lifecycle.  Our send() method pushes events to the SSE stream.
            await self.handle_message(event)

            # Wait briefly for the background processing to start and complete.
            # The base handle_message() spawns a background task — we need to
            # let it finish before emitting run.completed.  We do this by
            # watching the _active_sessions dict: the session is removed when
            # processing completes.
            from gateway.session import build_session_key as _bsk
            _sk = _bsk(
                event.source,
                group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            )
            await self._wait_for_session_completion(session_key=_sk, stream_id=stream_id)

        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            logger.exception("[myah] dispatch failed for stream %s", stream_id)
            self._push_event_sync(stream_id, {
                "event": "run.failed",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "error": str(exc),
            })
            terminal_event_sent = True
        finally:
            pending_clarifies = list((self._pending_clarifies.pop(stream_id, {}) or {}).keys())
            if pending_clarifies:
                try:
                    from tools.clarify_gateway import resolve_gateway_clarify
                    for clarify_id in pending_clarifies:
                        resolve_gateway_clarify(clarify_id, '')
                        self._push_event_sync(stream_id, {
                            'event': 'clarify.resolved',
                            'stream_id': stream_id,
                            'run_id': stream_id,
                            'timestamp': time.time(),
                            'clarify_id': clarify_id,
                            'status': 'timeout',
                        })
                except Exception:
                    logger.debug(
                        '[myah] Failed to clear pending clarifies for stream %s',
                        stream_id,
                        exc_info=True,
                    )

            # Emit run.completed if no explicit failure was sent
            q = self._streams.get(stream_id)
            if q is not None and not terminal_event_sent and not cancelled:
                # The queue still exists and the background session has ended;
                # only now is it safe to send the successful terminal event.

                # ── Myah: per-message model attribution (T3-932) ──
                # Read the cached agent's model + provider so the
                # frontend can show "answered by X via Y" per message.
                _attribution_model = ""
                _attribution_provider = ""
                try:
                    runner = self._resolve_runner()
                    if runner is not None:
                        attribution = get_cached_agent_attribution_direct(runner, session_key)
                        if attribution is not None:
                            _attribution_model = attribution.get("model", "") or ""
                            _attribution_provider = attribution.get("provider", "") or ""
                        # Fallback to session override if cache was evicted mid-flight
                        if not _attribution_model:
                            _override = get_session_override_direct(runner, session_key) or {}
                            _attribution_model = _override.get("model", "")
                            _attribution_provider = _override.get("provider", "")
                except Exception:
                    logger.debug("[myah] model attribution lookup failed", exc_info=True)
                # ────────────────────────────────────────────────

                # ── Phase F follow-up: gateway-suppression-bug workaround ──
                # If the agent's LLM call failed (e.g. provider 402, fallback
                # exhausted), the agent returns a response dict with
                # ``failed=True`` and ``final_response="API call failed ..."``.
                # But gateway/run.py:14701 constructs a new response dict
                # WITHOUT preserving the ``failed`` field, so the suppression
                # check at gateway/run.py:15326 sees failed=None → treats run
                # as successful → sees native_streamed=True (set
                # optimistically when our structured callbacks were wired) →
                # sets already_sent=True → _process_message_background skips
                # adapter.send entirely. User sees "Thinking..." forever.
                #
                # Detect this by checking ``_stream_had_content`` — a set of
                # stream_ids that received visible stream activity (assistant
                # text, structured tool rows, reasoning/status rows, slash
                # command output via adapter.send, cron preview, etc.).
                # If the stream got NO visible activity, the response was
                # swallowed by the gateway bug — emit a generic warning so
                # the user sees something instead of an empty Thinking
                # spinner.
                #
                # Critical: we check ``_stream_had_content``, NOT just
                # ``_stream_delta_invoked``. The latter only covers the
                # LLM streaming path. Slash commands like /model deliver
                # via adapter.send → _push_event_sync directly (no
                # stream_delta callback), so checking only
                # _stream_delta_invoked would false-positive on slash
                # commands and append the warning to /model's response.
                if stream_id not in self._stream_had_content:
                    logger.warning(
                        "[myah] gateway-suppression workaround firing for "
                        "session %s (stream %s): no message.delta event "
                        "was emitted, response would have been swallowed",
                        session_key, stream_id,
                    )
                    self._push_event_sync(stream_id, {
                        "event": "message.delta",
                        "stream_id": stream_id,
                        "run_id": stream_id,
                        "timestamp": time.time(),
                        "delta": (
                            "⚠️ The agent's LLM call did not produce a "
                            "response. This usually means the configured "
                            "provider returned an error (rate limit, "
                            "insufficient credits, authentication failure, "
                            "etc.). Check `~/.hermes/logs/agent.log` for "
                            "the specific provider error."
                        ),
                    })
                # ──────────────────────────────────────────────────────────

                self._push_event_sync(stream_id, {
                    "event": "run.completed",
                    "stream_id": stream_id,
                    "run_id": stream_id,
                    "timestamp": time.time(),
                    "model": _attribution_model,       # Myah: per-message attribution
                    "provider": _attribution_provider,  # Myah: per-message attribution
                })
                # Sentinel to close the SSE stream
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            elif q is not None and terminal_event_sent and not cancelled:
                # Failure is already terminal; close the SSE stream without
                # incorrectly emitting a successful run.completed event.
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

            # Unblock any pending secret capture (let callback thread handle cleanup)
            pending_secret = self._pending_secrets.get(stream_id)
            if pending_secret:
                pending_secret['event'].set()

            # ── Deferred cleanup when approvals pending (Phase 1 PR 1 §1.3) ──
            # If an action confirmation (e.g. plugin cronjob approval)
            # is still pending for this session, popping the dual
            # mapping now would orphan the eventual /myah/v1/confirm
            # callback — it looks up _stream_sessions[stream_id] to
            # find the session_key and would get None → 404. Defer the
            # cleanup until the approval resolves; the
            # resolve_action_confirmation* path will _schedule_resolved_expiry
            # which keeps the entry just long enough that the next user
            # action drives a fresh dispatch + new mapping.
            try:
                from myah_hermes_plugin.cron_approval import _has_pending_approvals
                _approvals_pending = _has_pending_approvals(session_key)
            except Exception:  # noqa: BLE001 — never block cleanup on a queue read
                logger.debug(
                    "[myah] _has_pending_approvals lookup failed; defaulting to cleanup",
                    exc_info=True,
                )
                _approvals_pending = False

            if _approvals_pending:
                logger.info(
                    "[myah] deferring _stream_sessions cleanup for session=%s "
                    "stream=%s (approval pending)",
                    session_key, stream_id,
                )
            elif cancelled and session_key in self._active_sessions:
                logger.info(
                    "[myah] preserving stream mappings for cancelled dispatch "
                    "with active session=%s stream=%s",
                    session_key,
                    stream_id,
                )
            else:
                # Clean up dual mappings (Fix 1)
                self._chat_id_streams.pop(chat_id, None)
                self._session_streams.pop(session_key, None)
                self._stream_sessions.pop(stream_id, None)

                # Phase F: clean up streaming workaround state. Keep
                # _chat_id_message_ids past stream cleanup: gateway final
                # adapter.send(...) can arrive after the SSE stream/queue has
                # disappeared, and the durable /messages/final fallback needs
                # the original platform assistant message_id. It is overwritten
                # on the next turn for this chat and swept on adapter shutdown.
                self._chat_id_session_keys.pop(chat_id, None)
                self._native_streaming_used.discard(session_key)
                self._stream_delta_invoked.discard(session_key)
                self._stream_had_content.discard(stream_id)

    async def _handle_events_endpoint(self, request: "web.Request") -> "web.StreamResponse":
        """GET /myah/v1/events/{stream_id} — SSE event stream."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        stream_id = request.match_info["stream_id"]

        # Allow subscribing slightly before the stream is registered
        for _ in range(20):
            if stream_id in self._streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(
                {"error": f"Stream not found: {stream_id}"},
                status=404,
            )

        q = self._streams[stream_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_INTERVAL)
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    await response.write(b": keepalive\n\n")
                    continue

                if event is None:
                    # Stream finished
                    await response.write(b": stream closed\n\n")
                    break

                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            logger.debug("[myah] SSE client disconnected: %s", stream_id)
        finally:
            # Clean up the stream
            self._streams.pop(stream_id, None)
            self._streams_created.pop(stream_id, None)

        return response

    async def _handle_confirm_endpoint(self, request: "web.Request") -> "web.Response":
        """POST /myah/v1/confirm/{stream_id} — resolve a pending approval.

        Body shape (Bug B fix — accept both queues):

        * Modern action confirmation (cron creation, plugin install, etc.):
          ``{"confirmation_id": "<uuid>", "choice": "approve|approve_session|deny"}``.
          Routes to ``resolve_action_confirmation`` (`_action_queues`).

        * Legacy terminal-command approval:
          ``{"choice": "approve|approve_session|deny"}``.  Routes to
          ``resolve_gateway_approval`` (`_gateway_queues`) using the
          session_key bound to the stream — preserved for backwards
          compatibility while the legacy approval flow is still
          referenced by ``send_exec_approval``.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        stream_id = request.match_info["stream_id"]

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        choice = body.get("choice", "deny")
        if choice not in ("approve", "approve_session", "deny"):
            return web.json_response(
                {"error": "choice must be 'approve', 'approve_session', or 'deny'"},
                status=400,
            )

        # ── Myah: Bug B — dispatch on confirmation_id ────────────────────
        # Action confirmations live in the global ``_action_queues`` keyed
        # by ``confirmation_id``.  They have NO dependency on the stream →
        # session_key mapping, so resolve them BEFORE the legacy
        # ``_stream_sessions`` lookup.  Otherwise an exec_approval whose
        # SSE stream has been closed (or whose run finished and cleaned up
        # the mapping in ``_dispatch_message``) returns a spurious 404
        # even though the action queue still has the pending confirmation
        # waiting to be resolved.  Production hot-fix 2026-05-06.
        confirmation_id = body.get("confirmation_id")
        if confirmation_id:
            from myah_hermes_plugin.cron_approval import resolve_action_confirmation

            ok = resolve_action_confirmation(confirmation_id, choice)
            if not ok:
                return web.json_response(
                    {
                        "error": (
                            f"No pending action confirmation matching "
                            f"confirmation_id={confirmation_id!r}"
                        )
                    },
                    status=404,
                )
            return web.json_response({"ok": True, "resolved": 1})
        # ─────────────────────────────────────────────────────────────────

        # Legacy gateway-queue path needs the stream → session mapping.
        # Action-queue path above already returned, so reaching here means
        # the frontend sent ``{choice}`` only (no ``confirmation_id``).
        session_key = self._stream_sessions.get(stream_id)
        if not session_key:
            return web.json_response(
                {"error": f"No active stream or session for stream_id={stream_id}"},
                status=404,
            )

        from tools.approval import resolve_gateway_approval
        from myah_hermes_plugin.cron_approval import resolve_action_confirmation_by_session

        resolved = resolve_gateway_approval(session_key, choice)
        # ── Myah: fall back to action queue when frontend POSTs without confirmation_id ──
        # The Myah frontend's ``ConfirmationCard.svelte`` posts ``{run_id, choice}``
        # — it doesn't yet send ``confirmation_id``.  When the legacy queue
        # has nothing for this session_key (the common case for cron approvals),
        # resolve the oldest pending action confirmation belonging to the same
        # session.  Mirrors the dual-queue resolution in ``/approve`` /
        # ``/deny`` slash commands so HTTP and chat paths behave the same.
        if resolved == 0:
            resolved = resolve_action_confirmation_by_session(session_key, choice)
        # ────────────────────────────────────────────────────────────────────────────────
        if resolved == 0:
            return web.json_response(
                {"error": "No pending confirmation to resolve"},
                status=404,
            )

        return web.json_response({"ok": True, "resolved": resolved})


    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a Hermes clarify request as a structured Myah SSE event."""
        stream_id = self._session_streams.get(session_key) or self._chat_id_streams.get(str(chat_id))
        if not stream_id:
            return SendResult(success=False, error=f"No active stream for clarify session={session_key}")

        self._pending_clarifies.setdefault(stream_id, {})[clarify_id] = {
            'session_key': session_key,
            'question': question,
            'choices': list(choices) if choices else None,
            'created_at': time.time(),
        }
        self._push_event(stream_id, {
            'event': 'clarify.required',
            'stream_id': stream_id,
            'run_id': stream_id,
            'timestamp': time.time(),
            'clarify_id': clarify_id,
            'question': question,
            'choices': list(choices) if choices else None,
        })
        return SendResult(success=True)

    async def _handle_clarify_endpoint(self, request: "web.Request") -> "web.Response":
        """POST /myah/v1/clarify/{stream_id} — resolve a pending clarify prompt."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        stream_id = request.match_info["stream_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        clarify_id = str(body.get('clarify_id') or '')
        response = str(body.get('response') or '').strip()
        if not clarify_id:
            return web.json_response({'error': 'clarify_id is required'}, status=400)
        if not response:
            return web.json_response({'error': 'response is required'}, status=400)
        if len(response) > 4096:
            return web.json_response({'error': 'response too long'}, status=400)

        pending_for_stream = self._pending_clarifies.get(stream_id) or {}
        if clarify_id not in pending_for_stream:
            return web.json_response(
                {'error': 'No pending clarify request for this stream'},
                status=404,
            )

        try:
            from tools.clarify_gateway import resolve_gateway_clarify
            ok = resolve_gateway_clarify(clarify_id, response)
        except Exception as exc:
            logger.error('[myah] Failed to resolve clarify %s: %s', clarify_id, exc)
            return web.json_response({'error': f'Failed to resolve clarify: {exc}'}, status=500)

        if not ok:
            pending_for_stream.pop(clarify_id, None)
            if not pending_for_stream:
                self._pending_clarifies.pop(stream_id, None)
            return web.json_response(
                {'error': 'No pending clarify request for this stream'},
                status=404,
            )

        pending_for_stream.pop(clarify_id, None)
        if not pending_for_stream:
            self._pending_clarifies.pop(stream_id, None)
        self._push_event(stream_id, {
            'event': 'clarify.resolved',
            'stream_id': stream_id,
            'run_id': stream_id,
            'timestamp': time.time(),
            'clarify_id': clarify_id,
            'status': 'answered',
            'response': response,
        })
        return web.json_response({'ok': True, 'resolved': 1})

    def _secret_capture_callback(
        self, var_name: str, prompt: str, metadata=None, stream_id: str = '',
    ) -> dict:
        """Prompt the user for a secret via inline SSE card.

        Called from the agent worker thread.  Blocks until the user submits
        the value via POST /myah/v1/secret/{stream_id}, or until timeout.
        """
        import threading
        if not stream_id:
            return {
                'success': True,
                'skipped': True,
                'stored_as': var_name,
                'validated': False,
                'message': 'No stream for secret capture',
            }

        event = threading.Event()
        self._pending_secrets[stream_id] = {
            'event': event,
            'var_name': var_name,
            'result': None,
        }

        # Emit SSE event to frontend (thread-safe — we're in agent thread)
        meta = metadata or {}
        self._push_event(stream_id, {
            'event': 'secret.required',
            'stream_id': stream_id,
            'run_id': stream_id,
            'timestamp': time.time(),
            'var_name': var_name,
            'prompt': prompt,
            'help': meta.get('help', ''),
            'skill_name': meta.get('skill_name', ''),
        })

        # Block agent thread (same pattern as approval system)
        resolved = event.wait(timeout=120)

        pending = self._pending_secrets.pop(stream_id, None)
        if not resolved or not pending or not pending.get('result'):
            # Timeout or cancelled
            self._push_event(stream_id, {
                'event': 'secret.resolved',
                'stream_id': stream_id,
                'run_id': stream_id,
                'timestamp': time.time(),
                'var_name': var_name,
                'status': 'timeout',
            })
            return {
                'success': True,
                'skipped': True,
                'stored_as': var_name,
                'validated': False,
                'message': 'Secret setup timed out.',
            }

        result = pending['result']

        # Emit resolved event
        self._push_event(stream_id, {
            'event': 'secret.resolved',
            'stream_id': stream_id,
            'run_id': stream_id,
            'timestamp': time.time(),
            'var_name': var_name,
            'status': 'cancelled' if result.get('cancelled') else 'stored',
        })

        return result

    async def _handle_secret_endpoint(self, request: 'web.Request') -> 'web.Response':
        """POST /myah/v1/secret/{stream_id} — receive a secret value from the frontend."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        stream_id = request.match_info['stream_id']
        pending = self._pending_secrets.get(stream_id)
        if not pending:
            return web.json_response(
                {'error': 'No pending secret capture for this stream'}, status=404
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        var_name = body.get('var_name', '')
        value = body.get('value', '')
        cancel = bool(body.get('cancel', False))

        if cancel:
            if var_name != pending['var_name']:
                return web.json_response(
                    {'error': f"var_name mismatch: expected {pending['var_name']}"}, status=400
                )
            pending['result'] = {
                'success': True,
                'skipped': True,
                'stored_as': var_name,
                'validated': False,
                'message': 'Secret entry cancelled.',
                'cancelled': True,
            }
            pending['event'].set()
            return web.json_response({'ok': True, 'cancelled': True, 'stored_as': var_name})

        if not value:
            return web.json_response({'error': 'value is required'}, status=400)
        if len(value) > 4096:
            return web.json_response({'error': 'value too long'}, status=400)
        if var_name != pending['var_name']:
            return web.json_response(
                {'error': f"var_name mismatch: expected {pending['var_name']}"}, status=400
            )

        # Write to .env using the same function the CLI uses
        try:
            from hermes_cli.config import save_env_value_secure
            result = save_env_value_secure(var_name, value)
            result['skipped'] = False
            result['message'] = 'Secret stored securely. The value was not exposed to the model.'
        except Exception as e:
            logger.error('[myah] Failed to save env value %s: %s', var_name, e)
            # Unblock the agent thread (leave result=None → callback treats as skip)
            pending['event'].set()
            return web.json_response({'error': f'Failed to store: {e}'}, status=500)

        # Unblock the agent thread
        pending['result'] = result
        pending['event'].set()

        return web.json_response({'ok': True, 'stored_as': var_name})

    # ── Myah: HTTP wrapper for auxiliary_client.call_llm ──────────────────
    _AUX_ALLOWED_TASKS = frozenset({
        'title_generation',
        'follow_up_generation',
    })

    async def _handle_aux_endpoint(self, request: 'web.Request') -> 'web.Response':
        """POST /myah/v1/aux/{task} — forward to auxiliary_client.call_llm."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        task = request.match_info.get('task', '')
        if task not in self._AUX_ALLOWED_TASKS:
            return web.json_response(
                {'error': f'unknown aux task: {task}'},
                status=400,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid JSON'}, status=400)

        messages = body.get('messages')
        if not isinstance(messages, list) or not messages:
            return web.json_response(
                {'error': 'messages field is required and must be a non-empty list'},
                status=400,
            )

        extra_body = {}
        if 'response_format' in body:
            extra_body['response_format'] = body['response_format']

        # ── Myah: aux router import for /myah/v1/aux/{task} ──────────────
        from agent.auxiliary_client import async_call_llm
        # ──────────────────────────────────────────────────────────────────
        try:
            response = await async_call_llm(
                task=task,
                messages=messages,
                temperature=body.get('temperature'),
                max_tokens=body.get('max_tokens'),
                extra_body=extra_body or None,
            )
        except Exception as e:
            return web.json_response({'error': str(e)}, status=502)

        usage_dict = {}
        if hasattr(response, 'usage') and response.usage is not None:
            usage_dict = {
                'prompt_tokens': getattr(response.usage, 'prompt_tokens', 0),
                'completion_tokens': getattr(response.usage, 'completion_tokens', 0),
                'total_tokens': getattr(response.usage, 'total_tokens', 0),
            }

        # Myah title/follow-up aux tasks are strict user-facing metadata tasks.
        # Only visible assistant content is authoritative; structured reasoning
        # fields (`reasoning`, `reasoning_content`, `reasoning_details`) are
        # internal scratch space and must never be promoted into `content`.
        content = _extract_visible_message_content(response)

        return web.json_response({
            'choices': [{
                'message': {
                    'role': 'assistant',
                    'content': content,
                },
                'finish_reason': getattr(response.choices[0], 'finish_reason', 'stop'),
            }],
            'usage': usage_dict,
        })
    # ─────────────────────────────────────────────────────────────────────

    # ── Myah: POST /myah/v1/active-provider — sync auth.json:active_provider ──
    async def _handle_active_provider_endpoint(self, request: 'web.Request') -> 'web.Response':
        """POST /myah/v1/active-provider — sync provider state, vanilla-style.

        Bug B follow-up (PR #74, May 1): Myah's onboarding handlers add a
        credential to ``auth.json:credential_pool`` but never sync the
        downstream provider state. Cron jobs that auto-resolve a provider
        read a stale ``active_provider`` value and pair it with
        config.yaml's model, producing requests to the wrong upstream.

        This endpoint delegates to ``sync_provider_state`` which mirrors
        vanilla hermes' two-category model:
          * PROVIDER_REGISTRY providers → ``active_provider=<id>``
          * non-registry providers (openrouter) → ``active_provider=None``
            plus ``OPENROUTER_API_KEY`` written to ``.env`` (resolve_provider
            falls through to the env-var branch).

        Request body: ``{"provider": "<provider_id>"}``
        Response (200): ``{"active_provider": <id|null>, "previous": <old|null>,
                            "env_var_written": <name|null>}``
        Errors: 400 (missing/empty/unknown provider), 401 (auth), 500.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid JSON'}, status=400)

        provider_id = (body.get('provider') or '').strip() if isinstance(body, dict) else ''
        if not provider_id:
            return web.json_response(
                {'error': "missing or empty 'provider' field"},
                status=400,
            )

        try:
            from hermes_cli.auth import _load_auth_store
            auth_store = _load_auth_store()

            credential_pool = auth_store.get('credential_pool')
            if not isinstance(credential_pool, dict) or provider_id not in credential_pool:
                return web.json_response(
                    {'error': f'Provider {provider_id} not in credential pool'},
                    status=400,
                )

            # Capture the pre-sync value for the response payload.
            previous = auth_store.get('active_provider')

            from myah_hermes_plugin._provider_sync import sync_provider_state
            result = sync_provider_state(provider_id)

            logger.info(
                f'[myah] active-provider endpoint: provider={provider_id!r} '
                f'previous={previous!r} result={result}'
            )
            return web.json_response({
                'active_provider': result['active_provider'],
                'previous': previous,
                'env_var_written': result['env_var_written'],
            })
        except Exception as exc:
            logger.exception('[myah] active-provider endpoint error')
            return web.json_response({'error': str(exc)}, status=500)
    # ──────────────────────────────────────────────────────────────────────────

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = 'dangerous command',
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Emit a structured approval SSE event instead of plain text.

        Called by the gateway runner when a dangerous command requires
        approval.  Emits a tool.confirmation_required event so the
        frontend can render the structured ConfirmationCard UI.
        """
        stream_id = self._session_streams.get(session_key)
        if not stream_id or stream_id not in self._streams:
            return SendResult(success=False, error='No active stream')

        cmd_preview = command[:500] + '...' if len(command) > 500 else command

        # ── Myah: exec_approval carries NO confirmation_id ──────────────────
        # The pending exec approval lives in upstream
        # ``tools.approval._gateway_queues`` keyed by ``session_key`` and is
        # resolved by ``resolve_gateway_approval(session_key, choice)`` — it
        # has no confirmation_id. Emitting a synthetic id here routed the
        # frontend's Approve click into the action-queue branch of
        # ``_handle_confirm_endpoint`` (which always 404s for exec_approval).
        # Omitting the field lets the confirm endpoint take the legacy
        # session/gateway path. ``send_action_confirmation`` keeps its real,
        # registered confirmation_id.
        self._push_event_sync(stream_id, {
            'event': 'tool.confirmation_required',
            'stream_id': stream_id,
            'run_id': stream_id,
            'timestamp': time.time(),
            'action_type': 'exec_approval',
            'description': f'Command requires approval:\n{cmd_preview}\n\nReason: {description}',
            'options': ['approve', 'approve_session', 'deny'],
            'metadata': metadata or {},
        })

        return SendResult(success=True)

    # ── Myah: Bug A follow-on — structured action confirmation card ─────
    async def send_action_confirmation(
        self,
        session_key: str,
        payload: Dict[str, Any],
    ) -> SendResult:
        """Emit a ``tool.confirmation_required`` SSE event for a generic
        action confirmation (cron creation, plugin install, ...).

        Mirrors ``send_exec_approval`` but accepts the payload from
        ``myah_hermes_plugin.cron_approval.request_action_confirmation``
        directly so the frontend's ``ConfirmationCard`` renders an
        interactive Approve / Deny card with the same ``confirmation_id``
        the agent is blocked on.  No text fallback — callers should fall
        back to ``adapter.send`` if this returns ``success=False`` so
        users without a live stream still see the prompt as plain text.

        Payload contract (from ``cron_approval.request_action_confirmation``):
            ``type``           — always ``"tool.confirmation_required"``
            ``confirmation_id`` — uuid the gateway resolves against
            ``action_type``    — e.g. ``"cron_create"``
            ``description``    — one-line human-readable summary
            ``options``        — usually ``["approve", "approve_session", "deny"]``
            ``metadata``       — optional structured fields for the card
                                  (``schedule_display``, ``prompt_preview``, …)
        """
        stream_id = self._session_streams.get(session_key)
        if not stream_id or stream_id not in self._streams:
            return SendResult(success=False, error='No active stream')

        self._push_event_sync(stream_id, {
            'event': 'tool.confirmation_required',
            'stream_id': stream_id,
            'run_id': stream_id,
            'timestamp': time.time(),
            'confirmation_id': payload.get('confirmation_id', ''),
            'action_type': payload.get('action_type', 'confirmation'),
            'description': payload.get('description', ''),
            'options': payload.get('options') or ['approve', 'deny'],
            'metadata': payload.get('metadata') or {},
        })
        return SendResult(success=True)
    # ────────────────────────────────────────────────────────────────────

    # ── Structured callbacks for gateway runner ───────────────────────────

    def get_structured_callbacks(self, session_key: str) -> Optional[Dict]:
        """Return callbacks that push structured SSE events to the stream.

        Called by gateway/run.py before each agent turn.  If this returns
        a dict, the gateway uses these callbacks instead of the default
        text-based GatewayStreamConsumer.

        The callbacks fire from the agent's worker thread, so they use
        call_soon_threadsafe (Fix 2) to push events safely.

        tool_progress_callback receives these invocation patterns (Fix 3):
            ("tool.started", tool_name, preview, args_dict)
            ("tool.completed", tool_name, None, None, duration=float, is_error=bool)
            ("_thinking", first_line_text)
            ("reasoning.available", "_thinking", text_preview, None)
        """
        stream_id = self._session_streams.get(session_key)
        if not stream_id or stream_id not in self._streams:
            return None

        q = self._streams[stream_id]

        def _put(event_data: dict):
            """Thread-safe push from agent worker thread.

            Mirrors ``_push_event_sync``'s ``_stream_had_content`` tracking
            (BONUS-2 contract). The LLM streaming path emits message.delta
            events through this closure — bypassing ``_push_event_sync``
            entirely — so the content tracker was empty even on
            fully-successful streams. The ``_dispatch_message`` finally
            block at line 807 then saw an empty set and falsely appended
            the gateway-suppression warning ("did not produce a response")
            to every streamed reply.

            Marking here keeps the BONUS-2 design intact: any path that
            actually delivers a message.delta to the SSE stream marks
            the stream as having had content, regardless of whether the
            push went through the sync helper or the threadsafe queue
            primitive.
            """
            # Structured tool/reasoning/status events are also visible user
            # activity. They should keep the stream from degrading to the
            # gateway-suppression fallback after /steer or long tool-only
            # phases where no assistant text has streamed yet.
            if event_data.get("event") in self.CONTENTFUL_STREAM_EVENTS:
                self._stream_had_content.add(stream_id)
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, event_data)
            except RuntimeError:
                pass  # Loop closed

        # Track that streaming actually fired for this session. The
        # post_llm_call hook (streaming_callbacks.py) reads this set —
        # if stream_delta never fired but post_llm_call has an
        # assistant_response, the gateway's suppression bug
        # (gateway/run.py:14701 drops the agent's failed=True flag, then
        # gateway/run.py:15326 suppresses send-final because failed is
        # falsy) is about to swallow the response. The hook then emits
        # the response as a synthetic message.delta so the user sees
        # the error instead of "Thinking..." forever.
        _captured_session_key = session_key

        def _stream_delta(text):
            if text is None:
                return  # Tool boundary signal — ignore for SSE
            # Mark this session as "stream actually fired" so the
            # post_llm_call hook knows the response was streamed normally
            # and doesn't need to be re-emitted.
            _invoked = getattr(self, "_stream_delta_invoked", None)
            if _invoked is not None:
                _invoked.add(_captured_session_key)
            _put({
                "event": "message.delta",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "delta": text,
            })

        def _tool_progress(*args, **kwargs):
            _put(self._format_tool_event(stream_id, args, kwargs))

        def _tool_start(tool_call_id, function_name, tool_args):
            _put(self._format_tool_start_event(
                stream_id, tool_call_id, function_name, tool_args
            ))

        def _tool_complete(tool_call_id, function_name, tool_args, result):
            _put(self._format_tool_complete_event(
                stream_id, tool_call_id, function_name, tool_args, result
            ))

        def _reasoning(text):
            if not text:
                return
            _put({
                "event": "reasoning.delta",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "text": text,
            })

        def _status(text):
            _put({
                "event": "status",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "text": text,
            })

        return {
            "stream_delta": _stream_delta,
            "tool_progress": _tool_progress,
            "tool_start": _tool_start,
            "tool_complete": _tool_complete,
            "tool_start_callback": _tool_start,
            "tool_complete_callback": _tool_complete,
            "reasoning": _reasoning,
            "status": _status,
        }

    @staticmethod
    def _format_tool_start_event(
        stream_id: str,
        tool_call_id: str,
        function_name: str,
        tool_args: Any,
    ) -> dict:
        """Format Hermes' structured tool_start_callback into an SSE event."""
        return {
            "event": "tool.started",
            "stream_id": stream_id,
            "run_id": stream_id,
            "timestamp": time.time(),
            "tool": function_name,
            "call_id": tool_call_id,
            "args": tool_args if isinstance(tool_args, dict) else {},
            "preview": function_name or "",
        }

    @staticmethod
    def _format_tool_complete_event(
        stream_id: str,
        tool_call_id: str,
        function_name: str,
        tool_args: Any,
        result: Any,
    ) -> dict:
        """Format Hermes' structured tool_complete_callback into an SSE event."""
        return {
            "event": "tool.completed",
            "stream_id": stream_id,
            "run_id": stream_id,
            "timestamp": time.time(),
            "tool": function_name,
            "call_id": tool_call_id,
            "args": tool_args if isinstance(tool_args, dict) else {},
            "result": result if isinstance(result, str) else str(result),
            "duration": None,
            "error": False,
        }

    @staticmethod
    def _format_tool_event(stream_id: str, args: tuple, kwargs: dict) -> dict:
        """Format tool_progress_callback arguments into an SSE event dict.

        Handles all four invocation patterns from run_agent.py (Fix 3).
        """
        if not args:
            return {
                "event": "status",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "text": "working",
            }

        event_type = args[0]

        if event_type in {"tool.started", "tool.completed"}:
            # Hermes now emits structured tool_start/tool_complete callbacks
            # with the real tool_call.id. Ignore legacy name-keyed progress for
            # those phases so the UI does not render duplicate tool rows.
            return {
                "event": "status",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "text": str(args[2] if len(args) >= 3 else event_type),
            }
        elif event_type == "_thinking" and len(args) >= 2:
            return {
                "event": "reasoning.delta",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "text": args[1],
            }
        elif event_type == "reasoning.available" and len(args) >= 3:
            return {
                "event": "reasoning.available",
                "stream_id": stream_id,
                "run_id": stream_id,
                "timestamp": time.time(),
                "text": args[2] or "",
            }
        # Fallback for unknown event types
        return {
            "event": "status",
            "stream_id": stream_id,
            "run_id": stream_id,
            "timestamp": time.time(),
            "text": str(args[0]) if args else "unknown",
        }

    # ── Orphaned stream sweeper ─────────────────────────────────────────

    def _sweep_orphaned_streams_once(self, *, now: Optional[float] = None) -> None:
        """Clean up stale orphaned streams, skipping active gateway sessions."""
        current_time = time.time() if now is None else now
        stale = [
            sid
            for sid, created_at in list(self._streams_created.items())
            if current_time - created_at > _STREAM_TTL
            and self._stream_sessions.get(sid) not in self._active_sessions
        ]
        for sid in stale:
            logger.debug("[myah] sweeping orphaned stream %s", sid)
            q = self._streams.pop(sid, None)
            self._streams_created.pop(sid, None)
            # Also clean up any lingering mappings
            session_key = self._stream_sessions.pop(sid, None)
            if session_key:
                self._session_streams.pop(session_key, None)
                try:
                    from myah_hermes_plugin.dispatcher import unregister_gateway_notify
                    unregister_gateway_notify(session_key)
                except Exception:
                    pass
            # Remove reverse chat_id mapping
            stale_chat_ids = [
                cid for cid, s in self._chat_id_streams.items() if s == sid
            ]
            for cid in stale_chat_ids:
                self._chat_id_streams.pop(cid, None)
                # Keep _chat_id_message_ids for the durable final-message
                # fallback; stale-stream sweeping can race with gateway
                # final adapter.send(...) on long/non-streaming turns.
            # Close the queue if anyone is listening
            if q is not None:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

    async def _sweep_orphaned_streams(self) -> None:
        """Periodically clean up streams that were never consumed."""
        while True:
            await asyncio.sleep(60)
            self._sweep_orphaned_streams_once()

    # ── BasePlatformAdapter interface ───────────────────────────────────

    # ── Myah: media streaming endpoint ─────────────────────────────────────────
    def _myah_allowed_media_roots(self) -> 'list[_myah_Path]':
        """Return the list of allowed media-streaming roots.

        Derived from FIVE sources:
          1. Hardcoded Hermes cache directories (always included for back-compat).
          2. Default artifact roots used by hosted/OSS agent runs: /tmp, /data,
             /workspace, and /root.
          3. Hermes' configured terminal.cwd from config.yaml. This is where
             the agent's bash/execute_code tools land by default. For hosted
             Myah this is /root (Hermes' Docker default). For OSS-Myah this is
             whatever the user configured (e.g. ~/workspace).
          4. TERMINAL_CWD / MESSAGING_CWD env bridges for the effective tool
             runner cwd when terminal.cwd is relative.
          5. Optional MYAH_MEDIA_ALLOWED_ROOTS env var (colon-separated paths)
             for explicit additions beyond the defaults.

        Each root is resolved (symlinks followed, strict=False so non-existent
        paths don't raise) so the subpath check in _handle_media_get is
        consistent with what the OS sees on disk.
        """
        import os as _myah_os_mod
        from hermes_constants import get_hermes_home, get_hermes_dir

        base = get_hermes_home()
        roots: 'list[_myah_Path]' = [
            # ── Myah: hermes cache directories (always allowed) ─────────────
            # The four canonical cache dirs Hermes' own tools write to plus
            # the cache root for tools that default to the root cache dir.
            # T3-1001 dogfooding 2026-04-24.
            (base / 'cache').resolve(),
            get_hermes_dir('cache/images', 'image_cache').resolve(),
            get_hermes_dir('cache/audio', 'audio_cache').resolve(),
            get_hermes_dir('cache/documents', 'document_cache').resolve(),
            get_hermes_dir('cache/screenshots', 'browser_screenshots').resolve(),
            # ────────────────────────────────────────────────────────────────
            # ── Myah: default artifact roots (OSS + hosted parity) ──────────
            # Hosted per-user containers inject MYAH_MEDIA_ALLOWED_ROOTS with
            # /data:/tmp:/workspace. Public OSS/local-Hermes installs may not
            # have that container env injection, but Hermes tools still commonly
            # write generated artifacts to these paths (and /root in Docker).
            # Keep these defaults in the adapter itself so MEDIA:/tmp/foo.mp4
            # works the same way in OSS as it does in hosted production.
            _myah_Path('/tmp').resolve(strict=False),
            _myah_Path('/data').resolve(strict=False),
            _myah_Path('/workspace').resolve(strict=False),
            _myah_Path('/root').resolve(strict=False),
            # ────────────────────────────────────────────────────────────────
        ]

        # ── Myah: derive allowed roots from terminal.cwd + env var (2026-04-28) ─
        # Topology coverage:
        #   - Hosted Myah: terminal.cwd: /root (Hermes default in our Docker
        #     image) → /root is in the allowlist automatically.
        #   - OSS Myah: user configures terminal.cwd to ~/workspace or similar
        #     → that path is in the allowlist automatically.
        #   - Anything else: MYAH_MEDIA_ALLOWED_ROOTS env var (colon-separated).
        try:
            from hermes_cli.config import load_config
            _myah_cfg = load_config() or {}
            _myah_terminal_cwd = (_myah_cfg.get('terminal', {}) or {}).get('cwd')
            if _myah_terminal_cwd:
                try:
                    roots.append(_myah_Path(_myah_terminal_cwd).expanduser().resolve(strict=False))
                except (OSError, ValueError):
                    logger.warning(
                        'Skipping unresolvable terminal.cwd in allowed media roots: %r',
                        _myah_terminal_cwd,
                    )
        except ImportError:
            # hermes_cli.config not importable in this context — fall back to env var only.
            pass

        # The gateway/tool runner bridges the *effective* cwd into env vars.
        # This matters when config has terminal.cwd='.' — resolving '.' inside
        # the adapter process can point at the plugin install dir, while the
        # agent's file tools actually used TERMINAL_CWD / MESSAGING_CWD.
        for _myah_cwd_env in ('TERMINAL_CWD', 'MESSAGING_CWD'):
            _myah_cwd = _myah_os_mod.environ.get(_myah_cwd_env, '').strip()
            if not _myah_cwd:
                continue
            try:
                roots.append(_myah_Path(_myah_cwd).expanduser().resolve(strict=False))
            except (OSError, ValueError):
                logger.warning(
                    'Skipping unresolvable %s in allowed media roots: %r',
                    _myah_cwd_env,
                    _myah_cwd,
                )

        _myah_extra_roots = _myah_os_mod.environ.get('MYAH_MEDIA_ALLOWED_ROOTS', '')
        for _myah_extra in _myah_extra_roots.split(':'):
            _myah_extra = _myah_extra.strip()
            if not _myah_extra:
                continue
            try:
                roots.append(_myah_Path(_myah_extra).expanduser().resolve(strict=False))
            except (OSError, ValueError):
                logger.warning(
                    'Skipping unresolvable path in MYAH_MEDIA_ALLOWED_ROOTS: %r',
                    _myah_extra,
                )
        # ────────────────────────────────────────────────────────────────────────

        return roots

    async def _handle_media_get(self, request: 'web.Request') -> 'web.Response':
        """GET /myah/v1/media?path=<path> — stream a cached media file.

        Security:
        - Bearer auth (same as other /myah/v1/* routes)
        - strict path resolution defeats symlink traversal
        - whitelist of allowed cache directories
        - no directory listing, no write, no delete
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        path_str = request.rel_url.query.get('path', '')
        if not path_str:
            return web.json_response({'error': "Missing 'path' query parameter"}, status=400)

        try:
            resolved = _myah_Path(path_str).resolve(strict=True)
        except (FileNotFoundError, RuntimeError, OSError):
            return web.json_response({'error': 'File not found'}, status=404)

        allowed_roots = self._myah_allowed_media_roots()

        def _is_subpath(child: '_myah_Path', parent: '_myah_Path') -> bool:
            try:
                child.relative_to(parent)
                return True
            except ValueError:
                return False

        if not any(_is_subpath(resolved, root) for root in allowed_roots):
            return web.json_response(
                {'error': 'Path not in an allowed cache directory'}, status=403,
            )

        mime, _ = _myah_mimetypes.guess_type(str(resolved))
        if not mime:
            mime = 'application/octet-stream'

        try:
            file_size = resolved.stat().st_size
        except OSError:
            return web.json_response({'error': 'File not accessible'}, status=404)

        response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': mime,
                'Content-Length': str(file_size),
                'Cache-Control': 'private, max-age=300',
            },
        )
        await response.prepare(request)
        try:
            with resolved.open('rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    await response.write(chunk)
        except Exception:
            pass  # client disconnected mid-stream
        await response.write_eof()
        return response
    # ────────────────────────────────────────────────────────────────────────────

    def _register_routes_on_app(self, app: "web.Application") -> None:
        """Attach Myah routes to a freshly created aiohttp ``Application``.

        Called once per ``connect()`` from
        :class:`MyahStandaloneRunner.start` BEFORE ``AppRunner.setup``
        freezes the router.  The plugin owns its own app — there is no
        shared app to attach to (Tier 2A Task 2A.3, 2026-05-07).
        """
        app["myah_adapter"] = self
        app.router.add_get("/myah/health", self._handle_health)
        app.router.add_post("/myah/v1/message", self._handle_message_endpoint)
        app.router.add_get("/myah/v1/events/{stream_id}", self._handle_events_endpoint)
        app.router.add_post("/myah/v1/confirm/{stream_id}", self._handle_confirm_endpoint)
        app.router.add_post("/myah/v1/clarify/{stream_id}", self._handle_clarify_endpoint)
        app.router.add_post("/myah/v1/secret/{stream_id}", self._handle_secret_endpoint)
        app.router.add_get("/myah/v1/media", self._handle_media_get)  # Myah: media endpoint
        # ── Myah: aux router HTTP wrapper ────────────────────────────────
        app.router.add_post('/myah/v1/aux/{task}', self._handle_aux_endpoint)
        # ─────────────────────────────────────────────────────────────────

        # ── Myah: active-provider sync endpoint (Bug B follow-up) ────────
        app.router.add_post('/myah/v1/active-provider', self._handle_active_provider_endpoint)
        # ─────────────────────────────────────────────────────────────────

        # ── Myah: runtime-control admin surface ──────────────────────────
        # Mounts /myah/v1/admin/* — the small set of admin operations that
        # MUST run in the gateway process because they touch GatewayRunner
        # state (session model overrides, cache eviction, busy-check, MCP
        # refresh). Everything else (file-system admin: SOUL, skills, plugins,
        # MCP CRUD, providers, reset) lives in the myah-admin DASHBOARD plugin
        # at myah_hermes_plugin/myah_admin/dashboard/plugin_api.py
        # (materialized into /opt/myah/plugins/myah-admin/ at image build
        # time by ``myah-hermes-plugin install --dashboard-only``).
        from .runtime_admin import register_runtime_admin_routes
        register_runtime_admin_routes(
            app,
            # _resolve_runner discovers the runner lazily so plugin-platform
            # adapters (where gateway_runner is never set by upstream) still
            # get full admin functionality. None is acceptable too — admin
            # routes that need it check defensively.
            runner=self._resolve_runner(),
            auth_key=self._auth_key,
        )
        # ─────────────────────────────────────────────────────────────────

        self._routes_registered = True
        logger.info("[%s] Routes registered on plugin-owned aiohttp app", self.name)

    async def connect(self) -> bool:
        """Start the plugin-owned aiohttp runner.

        Tier 2A Task 2A.3 (2026-05-07) collapsed the previous hosted /
        standalone split: the adapter ALWAYS runs on its own aiohttp
        ``AppRunner`` + ``TCPSite``.  Port resolution order:

        1. ``config.extra.port`` from the platform config.
        2. ``MYAH_ADAPTER_PORT`` env var.
        3. ``MYAH_GATEWAY_PORT`` env var (default 8643).

        See ``docs/superpowers/specs/2026-05-06-myah-oss-completion-design.md``
        §3 Task 2A.3 for the explicit one-way-door rationale.
        """
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        self._standalone_mode = True
        self._runner_helper = MyahStandaloneRunner()

        try:
            bound = await self._runner_helper.start(
                self._register_routes_on_app,
                host="0.0.0.0",
                port=self._port,
            )
        except Exception:
            logger.exception(
                "[%s] Failed to start standalone aiohttp site on port %d",
                self.name,
                self._port,
            )
            # Best-effort teardown of any partially started state.
            try:
                await self._runner_helper.stop()
            except Exception:  # noqa: BLE001
                pass
            self._runner_helper = None
            return False

        # Mirror the bound port back onto the legacy attributes so any
        # downstream code that peeks at ``self._own_app`` etc. keeps
        # working (test fixtures + the disconnect path).
        self._own_app = self._runner_helper.app
        # NOTE: ``_own_runner`` / ``_own_site`` are intentionally left
        # unset — the runner-helper owns them now and ``disconnect()``
        # delegates teardown to ``self._runner_helper.stop()``.
        self._port = bound

        # Capture the event loop for thread-safe queue access (Fix 2)
        self._loop = asyncio.get_running_loop()

        # Start background sweep for orphaned streams
        sweep_task = asyncio.create_task(self._sweep_orphaned_streams())
        try:
            self._background_tasks.add(sweep_task)
        except TypeError:
            pass
        if hasattr(sweep_task, "add_done_callback"):
            sweep_task.add_done_callback(self._background_tasks.discard)

        self._mark_connected()
        logger.info(
            "[%s] Myah adapter connected (standalone mode, port=%d)",
            self.name,
            self._port,
        )
        return True

    async def disconnect(self) -> None:
        """Clean up all active streams and mappings."""
        self._mark_disconnected()

        # Close all active streams
        for stream_id, q in list(self._streams.items()):
            try:
                q.put_nowait(None)
            except Exception:
                pass

        # Unregister all approval callbacks
        from myah_hermes_plugin.dispatcher import unregister_gateway_notify
        for session_key in list(self._session_streams.keys()):
            try:
                unregister_gateway_notify(session_key)
            except Exception:
                pass

        self._streams.clear()
        self._streams_created.clear()
        self._session_streams.clear()
        self._chat_id_streams.clear()
        self._stream_sessions.clear()
        self._chat_id_message_ids.clear()

        # Tear down the plugin-owned aiohttp runner.  Helper handles
        # both ``TCPSite.stop()`` and ``AppRunner.cleanup()`` and is
        # idempotent.
        if self._runner_helper is not None:
            try:
                await self._runner_helper.stop()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[%s] standalone runner stop() raised", self.name, exc_info=True
                )
            self._runner_helper = None
        self._own_site = None
        self._own_runner = None
        self._own_app = None

        logger.info("[%s] Myah adapter disconnected", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Push a response to the user.

        Routing depends on the **caller**:

        * **Cron deliveries** (``metadata.job_id`` is set — Bug D v4):
          ALWAYS go through the webhook so the platform persists the
          run to chat history.  SSE is *not* a persistence path —
          ``message.delta`` events without a real consumer are silently
          dropped, and even with a consumer the cron's payload would
          be concatenated into whatever assistant message the chat
          happens to be streaming for an unrelated turn.  The webhook
          (``/api/v1/processes/webhook/run-complete``) is the only
          surface that calls ``_inject_cron_output_to_chat`` and
          writes the cron run to the chat's message history.

        * **Live chat replies** (no ``metadata.job_id``):
          1. Push as ``message.delta`` onto the active SSE stream when
             a subscriber is bound to ``chat_id`` — the user sees
             tokens stream into the message they just sent.
          2. Fall back to ``"No active stream"`` failure when no
             subscriber exists.  These are normal gateway-runner
             responses; the runner has its own retry semantics, and
             the cron-only webhook would 400 on missing ``job_id``.

        Threading: invoked from the cron ``ThreadPoolExecutor`` worker
        via ``asyncio.run_coroutine_threadsafe(adapter.send(...),
        loop)`` (see ``cron/scheduler.py:_deliver_result``).  The body
        is a plain ``async def`` — the threading bridge happens in the
        caller, not here.  Never raises — the cron path relies on
        ``SendResult.success`` to decide between adapter delivery and
        the standalone fallback.

        Bug D-v4 history (2026-04-25): an earlier version of this
        method preferred SSE-first for ALL callers and only fell back
        to the webhook when ``_chat_id_streams.get(chat_id)`` was
        empty.  That broke "test it here" cron triggers — the
        triggering chat turn had a live stream, the cron's content
        was pushed onto that turn's SSE queue, ``send()`` returned
        success, and the webhook never fired.  The output was either
        appended into the unrelated assistant message buffer or
        dropped on the floor when the run completed.  Output existed
        on disk but never reached chat history.  See
        ``docs/superpowers/specs/2026-04-24-cron-origin-and-approval-design.md``.
        """
        meta = dict(metadata) if metadata else {}
        is_cron_delivery = bool(meta.get("job_id"))

        # ── Cron Path A: recover job_id when scheduler didn't forward it ──
        # Vanilla upstream's cron/scheduler._deliver_result calls
        # adapter.send(chat_id, content, metadata={"thread_id": ...})
        # without forwarding the job dict — meta.job_id is absent and
        # the cron delivery would silently fall down the SSE-first path
        # (which doesn't persist to chat history).
        #
        # Recover via two signals, in order:
        #   1. The session contextvar (set by the cron worker via
        #      upstream tools.approval.set_current_session_key).
        #   2. jobs.json lookup by origin.chat_id; pick the most recent
        #      last_run_at when multiple jobs target the same chat.
        # Either signal populates meta with job_id + job_name + origin
        # and flips is_cron_delivery so the webhook path runs.
        # See spec §6.1 Path A.
        if not is_cron_delivery:
            recovered_job_id = _recover_cron_job_id_from_session_key()
            recovered_job: Optional[Dict[str, Any]] = None
            if recovered_job_id:
                for j in _load_cron_jobs_safely():
                    if j.get("id") == recovered_job_id:
                        recovered_job = j
                        break
                # Even without a jobs.json hit, the session key alone is
                # enough to mark this as cron delivery — the webhook
                # accepts the minimal payload.
                meta["job_id"] = recovered_job_id
                if recovered_job:
                    meta.setdefault("job_name", recovered_job.get("name") or recovered_job_id)
                    if recovered_job.get("origin"):
                        meta.setdefault("origin", recovered_job["origin"])
                else:
                    meta.setdefault("job_name", recovered_job_id)
                is_cron_delivery = True
            elif chat_id and (
                # Bug F fix (2026-05-21): the chat_id-based fallback was
                # firing for REGULAR gateway LLM replies whenever the chat
                # had any existing cron, causing the reply to be routed
                # through the cron-webhook path AND live-preview push,
                # duplicating the assistant message visible to the user.
                #
                # The cron scheduler ALWAYS passes some cron-context
                # metadata (``thread_id`` at minimum — see
                # ``cron/scheduler.py:_deliver_result``). Regular gateway
                # final-response sends pass no metadata. Gate the
                # jobs.json fallback on the presence of a cron-context
                # signal in metadata so regular replies bypass it.
                meta.get("thread_id")
                or meta.get("job_name")
                or meta.get("origin")
                or meta.get("ran_at")
            ):
                # No session signal — try jobs.json lookup by chat_id.
                matches = [
                    j for j in _load_cron_jobs_safely()
                    if isinstance(j.get("origin"), dict)
                    and j["origin"].get("platform") == "myah"
                    and j["origin"].get("chat_id") == chat_id
                ]
                if matches:
                    # Pick the most recent last_run_at; treat missing
                    # values as empty-string so freshly-created jobs
                    # (cron/jobs.py:655 — last_run_at=None) sort below
                    # any job that's actually run.
                    #
                    # Upstream writes last_run_at as ISO 8601 string
                    # (cron/jobs.py:872 — _hermes_now().isoformat()).
                    # ISO 8601 is specifically designed so lexicographic
                    # string compare matches chronological order, so
                    # str-string compare gives the correct most-recent.
                    #
                    # The (has_value, str) tuple form ensures None-valued
                    # jobs always lose to ANY job with a value, regardless
                    # of how str() coerces non-ISO inputs. This guards
                    # against the pre-fix TypeError when last_run_at was
                    # a mix of ISO strings and None (caught by PR #3
                    # reviewer, 2026-05-19).
                    matches.sort(key=_cron_last_run_sort_key, reverse=True)
                    chosen = matches[0]
                    meta["job_id"] = chosen.get("id")
                    meta.setdefault("job_name", chosen.get("name") or chosen.get("id"))
                    if chosen.get("origin"):
                        meta.setdefault("origin", chosen["origin"])
                    is_cron_delivery = True

        # ── Cron deliveries: always webhook (persistence path) ──
        if is_cron_delivery:
            # Optional live-preview: push the cron output to any active
            # SSE stream so the user gets a quick visual confirmation if
            # they happen to be watching this chat.  This is decoration
            # only — the platform's webhook handler is what writes the
            # cron run to chat history regardless of what we push here.
            stream_id = self._chat_id_streams.get(chat_id) if chat_id else None
            if stream_id and stream_id in self._streams:
                try:
                    self._push_event_sync(stream_id, {
                        "event": "message.delta",
                        "stream_id": stream_id,
                        "run_id": stream_id,
                        "timestamp": time.time(),
                        "delta": content,
                        "message_id": uuid.uuid4().hex[:12],
                    })
                except Exception as exc:  # noqa: BLE001 - best-effort preview
                    logger.debug(
                        f"Live-preview SSE push for cron delivery failed (non-fatal): {exc}"
                    )
            # Use the (possibly recovery-enriched) ``meta`` dict, NOT
            # the caller's original ``metadata`` — otherwise Path A's
            # recovered job_id/job_name/origin wouldn't reach the
            # webhook payload builder.
            return await self._send_via_webhook(chat_id, content, meta)
        # ────────────────────────────────────────────────────────

        # ── Phase F: native-streaming dedup ────────────────────────
        # When our pre_llm_call hook installed structured callbacks for
        # this session (Phase F), the SSE stream already delivered
        # tokens during the agent run. Vanilla's gateway calls
        # adapter.send(chat_id, full_response) after streaming
        # completes — that call would duplicate the assistant message.
        # The fork's _native_streaming_used flag in _run_agent would
        # have suppressed it; on vanilla we suppress it here.
        session_key = self._chat_id_session_keys.get(chat_id)
        if session_key and session_key in self._native_streaming_used:
            self._native_streaming_used.discard(session_key)
            logger.debug(
                "Phase F: suppressed gateway final send for native-streamed "
                "session=%s (chat_id=%s)",
                session_key, chat_id,
            )
            return SendResult(
                success=True,
                message_id="suppressed-native-streaming",
            )
        # ──────────────────────────────────────────────────────────

        # ── Live chat replies: SSE-first ────────────────────
        stream_id = self._chat_id_streams.get(chat_id) if chat_id else None
        if stream_id:
            q = self._streams.get(stream_id)
            if q is not None:
                msg_id = uuid.uuid4().hex[:12]
                self._push_event_sync(stream_id, {
                    "event": "message.delta",
                    "stream_id": stream_id,
                    "run_id": stream_id,
                    "timestamp": time.time(),
                    "delta": content,
                    "message_id": msg_id,
                })
                return SendResult(success=True, message_id=msg_id)

        if stream_id:
            logger.warning(
                "[myah] send fallback: stream mapping exists but queue missing "
                "chat_id=%s stream_id=%s active_streams=%s",
                chat_id,
                stream_id,
                list(self._streams.keys()),
            )
        else:
            logger.warning(
                "[myah] send fallback: no chat_id->stream mapping "
                "chat_id=%s active_chat_mappings=%s active_streams=%s",
                chat_id,
                dict(self._chat_id_streams),
                list(self._streams.keys()),
            )

        durable_result = await self._send_chat_final_via_webhook(chat_id, content, meta)
        if durable_result.success:
            return durable_result

        return SendResult(
            success=False,
            error=durable_result.error or f"No active stream for chat_id={chat_id}",
        )
        # ────────────────────────────────────────────────────

    # ── Cron delivery metadata enrichment override ─────────
    # Replaces the deleted ``cron/scheduler.py::_build_myah_send_metadata``
    # helper. Overrides ``BasePlatformAdapter.build_delivery_metadata``
    # (added to the fork in Tier 2B Task 2B.4 / Phase 4f Step 2; same
    # diff queued as upstream PR U-CRON). Called polymorphically by
    # ``cron.scheduler._deliver_result`` so the offline-webhook fallback
    # at ``MyahAdapter._send_via_webhook`` receives the ``job_id``,
    # ``job_name``, ``status``, ``ran_at``, and ``origin`` fields it
    # needs to reconstruct the platform's ``/webhook/run-complete``
    # payload.
    def build_delivery_metadata(
        self,
        job: Dict[str, Any],
        status_hint: str = "ok",
        base_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Enrich cron-delivery metadata for the Myah platform's offline-webhook fallback.

        Vendored from ``agent/hermes/cron/scheduler.py:_build_myah_send_metadata``
        (deleted in the same Phase 4f refactor). Called polymorphically
        by ``cron.scheduler._deliver_result`` for every cron delivery.

        Args:
            job: The cron job dict (must contain ``id``, ``name``, optionally ``origin``).
            status_hint: ``'ok'`` | ``'error'`` — forwarded into the ``adapter.send``
                metadata so the platform webhook receives the run status.
            base_metadata: Pre-existing metadata (e.g. ``{"thread_id": ...}``).
                Returned merged with the Myah-specific enrichment.

        Returns:
            A dict containing ``job_id``, ``job_name``, ``status``, ``ran_at``,
            ``origin`` — merged into a copy of ``base_metadata``. Caller mutations
            of the returned dict do not affect ``base_metadata`` (parity with
            ``BasePlatformAdapter``'s default, which returns ``dict(base_metadata)``).
        """
        from datetime import datetime, timezone

        merged: Dict[str, Any] = dict(base_metadata) if base_metadata else {}

        # Resolve origin the same way upstream's ``cron._resolve_origin`` does:
        # accept the dict only when it has both ``platform`` AND ``chat_id``;
        # otherwise collapse to ``None``. The plugin version is slightly more
        # defensive than upstream by checking ``isinstance(dict)`` first so a
        # malformed ``origin`` field never raises.
        origin = job.get("origin") or {}
        if not (isinstance(origin, dict) and origin.get("platform") and origin.get("chat_id")):
            origin = None

        merged.update({
            "job_id": job.get("id", ""),
            "job_name": job.get("name") or job.get("id", ""),
            "status": status_hint,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "origin": origin,
        })
        return merged
    # ────────────────────────────────────────────────────────

    async def _send_chat_final_via_webhook(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]],
    ) -> SendResult:
        """Persist a completed interactive chat reply when SSE is unavailable."""
        base_url = _myah_os.environ.get('MYAH_PLATFORM_BASE_URL')
        bearer = _myah_os.environ.get('MYAH_PLATFORM_BEARER')
        user_id = _myah_os.environ.get('MYAH_USER_ID', '')
        if not (base_url and bearer and user_id and chat_id):
            return SendResult(
                success=False,
                error=f"No active stream for chat_id={chat_id}",
            )

        meta = metadata or {}
        message_id = (
            meta.get('message_id')
            or self._chat_id_message_ids.get(chat_id)
            or ''
        )
        if not message_id:
            logger.warning(
                "[myah] final-message fallback unavailable: missing message_id "
                "chat_id=%s",
                chat_id,
            )
            return SendResult(
                success=False,
                error=(
                    f"No active stream for chat_id={chat_id}; "
                    "Missing message_id for durable final-message fallback"
                ),
            )
        session_key = self._chat_id_session_keys.get(chat_id) or ''
        model = ''
        provider = ''
        try:
            runner = self._resolve_runner()
            if runner is not None and session_key:
                attribution = get_cached_agent_attribution_direct(runner, session_key)
                if attribution is not None:
                    model = attribution.get('model', '') or ''
                    provider = attribution.get('provider', '') or ''
                if not model:
                    override = get_session_override_direct(runner, session_key) or {}
                    model = override.get('model', '') or ''
                    provider = override.get('provider', '') or ''
        except Exception:
            logger.debug("[myah] final-message attribution lookup failed", exc_info=True)

        payload = {
            'user_id': user_id,
            'chat_id': chat_id,
            'message_id': message_id,
            'response': content,
            'status': meta.get('status') or 'ok',
            'model': model,
            'provider': provider,
        }
        url = f"{base_url.rstrip('/')}/api/v1/myah/messages/final"
        headers = {'Authorization': f'Bearer {bearer}'}

        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body_text = ''
                    try:
                        body_text = (await resp.text())[:200]
                    except Exception:
                        pass
                    if 200 <= resp.status < 300:
                        msg_id = message_id or uuid.uuid4().hex[:12]
                        logger.info(
                            "[myah] durable final-message fallback delivered "
                            "chat_id=%s message_id=%s",
                            chat_id,
                            msg_id,
                        )
                        return SendResult(success=True, message_id=msg_id)
                    logger.warning(
                        "Myah final-message fallback failed: status=%s url=%s "
                        "chat_id=%s body=%r",
                        resp.status,
                        url,
                        chat_id,
                        body_text,
                    )
                    self._maybe_breadcrumb(
                        f"final-message fallback non-2xx: {resp.status}",
                        url=url, chat_id=chat_id, status=resp.status,
                    )
                    return SendResult(
                        success=False,
                        error=f"final-message fallback returned HTTP {resp.status}",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Myah final-message fallback raised %s: %s (url=%s chat_id=%s)",
                type(exc).__name__,
                exc,
                url,
                chat_id,
            )
            self._maybe_breadcrumb(
                f"final-message fallback exception: {type(exc).__name__}",
                url=url, chat_id=chat_id, error=str(exc)[:200],
            )
            return SendResult(
                success=False,
                error=f"final-message fallback error: {type(exc).__name__}: {exc}",
            )

    # ── Myah: Bug D v3 — webhook delivery helper ────────────
    async def _send_via_webhook(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]],
    ) -> SendResult:
        """POST cron output to the platform webhook receiver.

        Reads ``MYAH_PLATFORM_BASE_URL`` / ``MYAH_PLATFORM_BEARER`` /
        ``MYAH_USER_ID`` fresh from ``os.environ`` per call (the
        module-level cache at the top of this file is used by the
        attachment-fetch path which has different startup ordering).

        Payload shape pinned by ``platform-oss/backend/myah/routers/
        processes.py:824-831`` (the ``/webhook/run-complete`` handler).

        **Only fires for cron deliveries.**  The platform's webhook
        endpoint is ``/api/v1/processes/webhook/run-complete`` — it's
        cron-specific and rejects payloads without ``user_id`` and
        ``job_id``.  We detect cron callers by the presence of
        ``metadata.job_id`` (populated by
        :meth:`MyahAdapter.build_delivery_metadata`, called polymorphically
        by ``cron.scheduler._deliver_result`` — the legacy
        ``_build_myah_send_metadata`` helper was deleted in Phase 4f) and
        skip the webhook for non-cron chat replies that happen to hit a
        closed SSE stream — those should preserve the legacy
        ``"No active stream"`` failure so the gateway's standalone-send
        retry path can take over.
        """
        meta = metadata or {}

        # ── Skip for non-cron callers ──────────────────────
        # Live chat replies (gateway/run.py → adapter.send) carry
        # ``metadata={"thread_id": ...}`` or ``None`` — no cron context.
        # Attempting the webhook there always fails (platform 400s on
        # missing user_id/job_id) and adds noise to the logs.
        if not meta.get('job_id'):
            return SendResult(
                success=False,
                error=f"No active stream for chat_id={chat_id}",
            )

        base_url = _myah_os.environ.get('MYAH_PLATFORM_BASE_URL')
        bearer = _myah_os.environ.get('MYAH_PLATFORM_BEARER')
        if not (base_url and bearer):
            # No webhook env — preserve the legacy ``No active stream``
            # failure shape so existing callers (and existing tests)
            # don't see new behaviour when the platform isn't wired in.
            return SendResult(
                success=False,
                error=f"No active stream for chat_id={chat_id}",
            )

        # chat_id resolution: caller-supplied first, then origin metadata
        # (cron path passes deliver=origin → adapter receives the origin
        # chat_id via metadata enrichment in scheduler._deliver_result).
        origin = meta.get('origin') if isinstance(meta.get('origin'), dict) else {}
        resolved_chat_id = chat_id or (origin.get('chat_id') if origin else '') or ''
        if not resolved_chat_id:
            return SendResult(
                success=False,
                error="webhook fallback: no chat_id and no metadata.origin.chat_id",
            )

        user_id = _myah_os.environ.get('MYAH_USER_ID', '')
        payload = {
            'user_id': user_id,
            'job_id': meta.get('job_id') or '',
            'job_name': meta.get('job_name') or meta.get('job_id') or '',
            'chat_id': resolved_chat_id,
            'response': content,
            'status': meta.get('status') or 'ok',
            'ran_at': meta.get('ran_at') or '',
            'tool_calls_log': meta.get('tool_calls_log'),
        }
        url = f"{base_url.rstrip('/')}/api/v1/processes/webhook/run-complete"
        headers = {'Authorization': f'Bearer {bearer}'}

        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if 200 <= resp.status < 300:
                        msg_id = uuid.uuid4().hex[:12]
                        return SendResult(success=True, message_id=msg_id)
                    body_text = ''
                    try:
                        body_text = (await resp.text())[:200]
                    except Exception:
                        pass
                    logger.warning(
                        f"Myah webhook delivery failed: status={resp.status} "
                        f"url={url} chat_id={resolved_chat_id} body={body_text!r}"
                    )
                    self._maybe_breadcrumb(
                        f"webhook delivery non-2xx: {resp.status}",
                        url=url, chat_id=resolved_chat_id, status=resp.status,
                    )
                    return SendResult(
                        success=False,
                        error=f"webhook returned HTTP {resp.status}",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Myah webhook delivery raised {type(exc).__name__}: {exc} "
                f"(url={url} chat_id={resolved_chat_id})"
            )
            self._maybe_breadcrumb(
                f"webhook delivery exception: {type(exc).__name__}",
                url=url, chat_id=resolved_chat_id, error=str(exc)[:200],
            )
            return SendResult(
                success=False,
                error=f"webhook error: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _maybe_breadcrumb(message: str, **data: Any) -> None:
        """Best-effort Sentry breadcrumb at warning level — never raises."""
        try:
            import sentry_sdk  # type: ignore[import-not-found]
            sentry_sdk.add_breadcrumb(
                category="myah.adapter",
                level="warning",
                message=message,
                data=data,
            )
        except Exception:  # noqa: BLE001 - breadcrumb is best-effort
            pass
    # ────────────────────────────────────────────────────────

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Push a typing/status indicator to the SSE stream."""
        stream_id = self._chat_id_streams.get(chat_id)
        if not stream_id:
            return

        self._push_event_sync(stream_id, {
            "event": "status",
            "stream_id": stream_id,
            "run_id": stream_id,
            "timestamp": time.time(),
            "status": "typing",
        })

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about this chat."""
        return {
            "name": "Myah Web",
            "type": "dm",
            "platform": "myah",
            "chat_id": chat_id,
        }
