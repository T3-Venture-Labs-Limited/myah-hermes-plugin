"""pre_gateway_dispatch hook for the Myah platform.

Tier 2A Task 2A.4 — replaces the ``skip_user_authorization`` semantics
that PR #20 removed from the fork's gateway/run.py.

Architectural note (verified 2026-05-07 against
``gateway/run.py::GatewayRunner._handle_message`` lines 3605-3644):
the upstream ``pre_gateway_dispatch`` hook supports three return
shapes — ``{"action": "skip", ...}`` to drop, ``{"action": "rewrite",
"text": ...}`` to mutate, and ``{"action": "allow"}`` (or ``None``)
to fall through to the normal auth + agent-loop chain. Returning
``"allow"`` does NOT bypass the gateway-level user-allowlist check at
line 3655; that path still runs.

For Myah's hosted single-tenant deployment topology, the gateway-level
auth bypass is provided by ``allow_all_env="MYAH_ALLOW_ALL_USERS"`` on
the platform registration (set in
``myah_platform/__init__.py::register``). The actual user authorization
happens at the platform's HTTP layer via the ``MYAH_ADAPTER_AUTH_KEY``
bearer token before requests ever reach the gateway. So when this hook
sees a ``platform == "myah"`` event, the user is already verified.

Today the hook is a thin "allow + observe" pass-through. It exists as
a single, documented choke point so future Myah-specific routing logic
(per-conversation rate limiting, silent-ingest of background events,
content-rewrite for tool-call playback, etc.) has a place to land
WITHOUT modifying upstream ``gateway/run.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def myah_pre_gateway_dispatch(
    event: Any,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Allow Myah-platform messages to proceed to normal dispatch.

    Parameters
    ----------
    event : MessageEvent
        Normalized inbound message. ``event.source.platform`` is the
        ``Platform`` enum we filter on.
    gateway : GatewayRunner
        Live gateway runner (unused today; kept for forward-compat).
    session_store : SessionStore
        Session-store handle (unused today; kept for forward-compat).
    **kwargs
        Reserved for future hook-signature additions.

    Returns
    -------
    dict | None
        ``{"action": "allow"}`` for Myah-platform messages so the
        first-recognized-action loop short-circuits cleanly with a
        documented decision. ``None`` for everything else so other
        plugins' hooks (and the default flow) can fire normally.
    """
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_name = getattr(platform, "value", None) if platform is not None else None

    if platform_name != "myah":
        return None

    logger.debug(
        "myah_pre_gateway_dispatch: allow platform=myah chat=%s user=%s",
        getattr(source, "chat_id", "unknown"),
        getattr(source, "user_id", "unknown"),
    )
    return {"action": "allow"}
