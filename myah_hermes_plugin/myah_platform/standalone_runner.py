"""Plugin-owned aiohttp ``AppRunner`` + ``TCPSite``.

Replaces the prior dependency on ``gateway/platforms/api_server.py``'s
``register_pre_setup_hook`` and ``get_shared_app``.  After Tier 2A
Task 2A.3 the plugin **always** runs on its own port (default 8643 via
``MYAH_GATEWAY_PORT``); upstream's API-server adapter is no longer in
the path.

This is a one-way door for hosted Myah: once shipped, hosted Myah uses
the standalone runner forever.  See
``docs/superpowers/specs/2026-05-06-myah-oss-completion-design.md`` §3
Task 2A.3 for the explicit decision rationale.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - aiohttp is a hard dep of the plugin
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# Default port (RFC-1700 unassigned range, hosted prod uses this).
_DEFAULT_PORT = 8643


def resolve_default_port() -> int:
    """Resolve the default standalone port from MYAH_GATEWAY_PORT env."""
    raw = os.environ.get("MYAH_GATEWAY_PORT", "")
    if not raw:
        return _DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "MYAH_GATEWAY_PORT=%r is not an integer — falling back to %d",
            raw,
            _DEFAULT_PORT,
        )
        return _DEFAULT_PORT


class MyahStandaloneRunner:
    """Wraps ``aiohttp.web.Application`` + ``AppRunner`` + ``TCPSite``.

    Designed for the plugin's adapter to own a single, fully isolated
    aiohttp surface that survives independent of any upstream hooks.

    Lifecycle:

        runner = MyahStandaloneRunner()
        await runner.start(register_routes_fn, host="0.0.0.0", port=...)
        ...
        await runner.stop()

    ``register_routes_fn`` is invoked with the freshly-created
    ``web.Application`` so the caller can attach its routes (and any
    per-app state, e.g. ``app["myah_adapter"] = self``) before
    ``AppRunner.setup`` freezes the router.

    ``port=0`` requests an OS-assigned ephemeral port — useful for
    tests.  After ``start()`` the actually bound port is available via
    :attr:`bound_port`.
    """

    def __init__(self) -> None:
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError(
                "aiohttp is required for MyahStandaloneRunner but is not installed"
            )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._bound_port: Optional[int] = None
        self._host: Optional[str] = None

    @property
    def bound_port(self) -> Optional[int]:
        """The port the site is bound to, or ``None`` before :meth:`start`."""
        return self._bound_port

    @property
    def app(self) -> Optional["web.Application"]:
        """The underlying ``web.Application`` (``None`` before start)."""
        return self._app

    async def start(
        self,
        register_routes: Callable[["web.Application"], None],
        *,
        host: str = "0.0.0.0",
        port: Optional[int] = None,
    ) -> int:
        """Build the app, attach routes, and start the TCP site.

        Returns the actually-bound port (== ``port`` unless
        ``port=0``).  Raises whatever ``aiohttp`` raises on bind
        failure.
        """
        assert web is not None  # for type-checkers; AIOHTTP_AVAILABLE check above
        if port is None:
            port = resolve_default_port()

        self._host = host
        self._app = web.Application()
        register_routes(self._app)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, host=host, port=port)
        await self._site.start()

        # Resolve the actually-bound port (port=0 → OS-assigned).
        self._bound_port = port
        if port == 0:
            # Reach into the underlying server socket for the bound port.
            try:
                server = self._site._server  # type: ignore[attr-defined]
                if server and server.sockets:
                    self._bound_port = server.sockets[0].getsockname()[1]
            except Exception:  # noqa: BLE001
                log.warning("Failed to resolve OS-assigned ephemeral port", exc_info=True)

        log.info(
            "MyahStandaloneRunner listening on http://%s:%d",
            host,
            self._bound_port,
        )
        return self._bound_port or 0

    async def stop(self) -> None:
        """Graceful shutdown.  Idempotent — safe to call multiple times."""
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:  # noqa: BLE001
                log.debug("MyahStandaloneRunner: TCPSite.stop() raised", exc_info=True)
            self._site = None

        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                log.debug(
                    "MyahStandaloneRunner: AppRunner.cleanup() raised", exc_info=True
                )
            self._runner = None

        self._app = None
        self._bound_port = None


# Re-export so ``from .standalone_runner import resolve_default_port,
# MyahStandaloneRunner`` works.
__all__ = ["MyahStandaloneRunner", "resolve_default_port"]
