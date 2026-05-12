"""F7 — disconnect a single MCP server without restarting the gateway.

Vanilla upstream's ``tools/mcp_tool`` exposes a ``shutdown_mcp_servers()``
helper that tears down ALL configured MCP servers at once. The Myah
admin UI needs per-server teardown so a user can remove one MCP
connection (e.g. a misbehaving server) while leaving the others alive.

Vanilla does NOT expose a public per-server disconnect API. The
implementation must use upstream's private state directly:

- ``_servers: Dict[str, MCPServerTask]``  — verified at
  ``upstream/main:tools/mcp_tool.py:1607``
- ``_lock: threading.Lock``                — line 1969 (sync, NOT asyncio)
- ``MCPServerTask.shutdown(self)``         — line 1568 (async coro)
- ``_run_on_mcp_loop(coro, timeout=...)``  — line 2042 (cross-loop helper)

Per ``runtime_extensions/__init__.py`` docstring: this is direct
attribute access (normal Python), not file modification, so the
``plugins MUST NOT modify core files`` rule (Teknium May 2026) is
satisfied.

Robustness: every step uses ``getattr(..., default)`` so an upstream
rename of any attribute degrades to "noop, return False" instead of
raising. The CI guard at
``tests/test_mcp_disconnect.py::test_upstream_state_present`` catches
the rename at plugin-CI time.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def disconnect_mcp_server(name: str, *, timeout: float = 15.0) -> bool:
    """Disconnect a single MCP server by name without restarting the gateway.

    Returns:
        True if the named server was found and shut down (cleanly or
        with a logged exception during ``shutdown()``).
        False if upstream's private state is missing (API drift) or
        no such server is registered.

    Args:
        name: The MCP server name as configured in ``mcp_servers`` of
            ``~/.hermes/config.yaml``.
        timeout: Max seconds to wait for the cross-loop ``shutdown()``
            coroutine to complete. Defaults to 15s.

    Thread safety: vanilla's ``_lock`` is a ``threading.Lock`` (sync,
    NOT ``asyncio.Lock``). We acquire it the way every other reader
    does — synchronously — and only call the async ``shutdown()`` via
    the upstream-supplied ``_run_on_mcp_loop`` cross-loop bridge.
    """
    try:
        from tools import mcp_tool
    except ImportError:
        logger.warning(
            "tools.mcp_tool unavailable; cannot disconnect %r — upstream "
            "package may be missing the MCP integration",
            name,
        )
        return False

    servers = getattr(mcp_tool, "_servers", None)
    lock = getattr(mcp_tool, "_lock", None)
    run_on_loop = getattr(mcp_tool, "_run_on_mcp_loop", None)

    if servers is None or lock is None:
        logger.warning(
            "tools.mcp_tool private state missing (_servers=%s, _lock=%s) — "
            "upstream may have refactored the MCP module",
            servers is not None,
            lock is not None,
        )
        return False

    with lock:
        target = servers.get(name)
        if target is None:
            logger.info("disconnect_mcp_server: no server named %r is registered", name)
            return False

        shutdown = getattr(target, "shutdown", None)
        if shutdown is not None and run_on_loop is not None:
            try:
                run_on_loop(shutdown(), timeout=timeout)
            except Exception as exc:
                # Deliberately swallow — the user wants the server gone;
                # surfacing a shutdown failure shouldn't block the pop.
                logger.warning(
                    "MCP server %r shutdown raised %s: %s — proceeding with removal",
                    name,
                    type(exc).__name__,
                    exc,
                )
        elif run_on_loop is None:
            logger.warning(
                "tools.mcp_tool._run_on_mcp_loop missing — cannot await "
                "shutdown(); removing server %r without graceful close",
                name,
            )

        servers.pop(name, None)
        logger.info("disconnect_mcp_server: removed %r from registry", name)
        return True
