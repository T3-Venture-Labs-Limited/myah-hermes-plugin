"""Env-CRUD handlers for the myah-admin dashboard plugin.

Wraps upstream GET / PUT / DELETE /api/env (web_server.py:1224, 1243, 1253)
via the loopback proxy. Plugin-namespace endpoints, exempted from the
dashboard's auth middleware. Phase 7.7 plugin migration (2026-05-12) —
see docs/superpowers/specs/2026-05-12-plugin-dashboard-migration-design.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ._common import require_session_token
from ._proxy import proxy_to_native

router = APIRouter(dependencies=[Depends(require_session_token)])


class EnvVarUpdate(BaseModel):
    key: str
    value: str


class EnvVarDelete(BaseModel):
    key: str


@router.get('/env')
async def get_env() -> dict:
    """Plugin-namespace mirror of GET /api/env."""
    return await proxy_to_native('GET', '/api/env')


@router.put('/env')
async def put_env(body: EnvVarUpdate) -> dict:
    """Plugin-namespace mirror of PUT /api/env."""
    return await proxy_to_native('PUT', '/api/env', json_body=body.model_dump())


@router.delete('/env')
async def delete_env(body: EnvVarDelete) -> dict:
    """Plugin-namespace mirror of DELETE /api/env."""
    return await proxy_to_native('DELETE', '/api/env', json_body=body.model_dump())
