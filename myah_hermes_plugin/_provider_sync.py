"""Vanilla-aligned provider state synchronization.

Vanilla hermes treats providers as two distinct categories:
  1. PROVIDER_REGISTRY providers (openai-codex, nous, anthropic, zai, kimi, ...)
     - auth.json:active_provider = <provider_id>
     - resolve_provider("auto") returns it via the active_provider branch
     - Some have OAuth tokens in credential_pool; some have api_key env vars.
  2. Non-registry "special" providers (currently only "openrouter")
     - auth.json:active_provider = None  (vanilla calls deactivate_provider)
     - resolve_provider("auto") returns "openrouter" via the env-var fallback branch
     - .env MUST have OPENROUTER_API_KEY (or OPENAI_API_KEY) for the env-var branch
       to find it.

PR #96 violated category-2's invariant by setting active_provider="openrouter".
This module restores the vanilla invariant.

References:
  - vanilla _model_flow_openrouter (main.py:2229-2285): writes .env, sets
    active_provider=None via deactivate_provider() at main.py:2282
  - vanilla _save_provider_state (auth.py:863-869): used for category-1
  - resolve_provider priority chain (auth.py:1112-1226): branches on PROVIDER_REGISTRY
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Hardcoded mapping for non-PROVIDER_REGISTRY special providers.
# Add new entries here if upstream adds more special-cased providers.
_SPECIAL_PROVIDER_ENV_VARS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
}


def _get_env_var_for_provider(provider_id: str) -> Optional[str]:
    """Return the env-var name for an api_key provider, or None.

    For PROVIDER_REGISTRY providers with auth_type='api_key', returns
    pconfig.api_key_env_vars[0]. For OAuth providers, returns None
    (no env-var needed). For special-cased providers (openrouter), returns
    the hardcoded mapping.
    """
    # Check the special-cased map first.
    if provider_id in _SPECIAL_PROVIDER_ENV_VARS:
        return _SPECIAL_PROVIDER_ENV_VARS[provider_id]
    # Fall through to PROVIDER_REGISTRY for registered api_key providers.
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except ImportError:
        return None
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if pconfig is None:
        return None
    if getattr(pconfig, "auth_type", None) != "api_key":
        return None
    env_vars = getattr(pconfig, "api_key_env_vars", None) or []
    return env_vars[0] if env_vars else None


def _resolve_api_key_from_pool(provider_id: str) -> Optional[str]:
    """Read the first credential_pool entry's access_token for a provider, or None.

    Accepts both vanilla list-of-dicts shape and the legacy dict shape that
    older tests / migrations sometimes produce.
    """
    try:
        from hermes_cli.auth import _load_auth_store
        store = _load_auth_store()
    except Exception:
        return None
    pool = store.get("credential_pool") or {}
    entries = pool.get(provider_id)
    if not entries:
        return None
    # Vanilla shape: list of dicts.
    if isinstance(entries, list):
        first = entries[0] if entries else None
        if not isinstance(first, dict):
            return None
        token = (first.get("access_token") or first.get("api_key") or "")
        return token.strip() or None
    # Legacy shape: bare dict with api_key/access_token.
    if isinstance(entries, dict):
        token = (entries.get("access_token") or entries.get("api_key") or "")
        return token.strip() or None
    return None


def sync_provider_state(provider_id: str, *, api_key: Optional[str] = None) -> dict:
    """Sync auth.json + .env to reflect provider_id, vanilla-style.

    Args:
        provider_id: The provider id (e.g., "openrouter", "openai-codex").
        api_key: Optional explicit api_key. If None, will be read from
            credential_pool[provider_id][0].access_token.

    Returns:
        dict with keys:
            "active_provider": new value (None or provider_id)
            "env_var_written": env var name that was written, or None
            "source": "explicit" if api_key arg used, "pool" if read from pool,
                "none" if no key was needed/available
    """
    from hermes_cli.auth import (
        PROVIDER_REGISTRY,
        _load_auth_store,
        _save_auth_store,
    )
    from hermes_cli.config import get_env_value, save_env_value

    result: dict = {
        "active_provider": None,
        "env_var_written": None,
        "source": "none",
    }

    # Determine env_var (if any) and resolve api_key.
    env_var = _get_env_var_for_provider(provider_id)
    resolved_key: Optional[str] = None
    key_source = "none"
    if env_var:
        if api_key:
            resolved_key = api_key
            key_source = "explicit"
        else:
            resolved_key = _resolve_api_key_from_pool(provider_id)
            if resolved_key:
                key_source = "pool"

    # Write env var if applicable AND it's not already set
    # (vanilla's invariant: .env wins; never overwrite an existing key).
    if env_var and resolved_key:
        existing = get_env_value(env_var) or ""
        if not existing.strip():
            try:
                save_env_value(env_var, resolved_key)
                result["env_var_written"] = env_var
                result["source"] = key_source
                logger.info(
                    "[myah] sync_provider_state: wrote %s to .env (source=%s)",
                    env_var,
                    key_source,
                )
            except Exception:
                logger.exception(
                    "[myah] sync_provider_state: failed to write %s to .env",
                    env_var,
                )

    # Update auth.json:active_provider per vanilla rules.
    try:
        store = _load_auth_store()
        if provider_id in PROVIDER_REGISTRY:
            # Category 1: registered provider — set active_provider=<id>.
            current = store.get("active_provider")
            if current != provider_id:
                store["active_provider"] = provider_id
                providers = store.get("providers")
                if not isinstance(providers, dict):
                    providers = {}
                    store["providers"] = providers
                providers.setdefault(provider_id, {})
                _save_auth_store(store)
                result["active_provider"] = provider_id
                logger.info(
                    "[myah] sync_provider_state: active_provider %r -> %r",
                    current,
                    provider_id,
                )
            else:
                result["active_provider"] = provider_id
        else:
            # Category 2: special-cased non-registry provider (openrouter).
            # Vanilla invariant: active_provider=None so resolve_provider("auto")
            # falls through to the env-var branch.
            current = store.get("active_provider")
            if current is not None:
                store["active_provider"] = None
                _save_auth_store(store)
                logger.info(
                    "[myah] sync_provider_state: active_provider %r -> None "
                    "(vanilla rule for non-registry provider %r)",
                    current,
                    provider_id,
                )
            result["active_provider"] = None
    except Exception:
        logger.exception(
            "[myah] sync_provider_state: failed to update auth.json for %r",
            provider_id,
        )

    return result
