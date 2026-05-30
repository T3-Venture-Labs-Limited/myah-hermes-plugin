"""Cron job metadata handlers for the myah-admin dashboard plugin.

This surface is intentionally narrow: Myah may persist Myah-owned routing
metadata under ``job.myah`` so the platform can adopt legacy Hermes crons into
chat visibility. It does not expose arbitrary job mutation and never patches
native ``origin`` / ``deliver`` fields, preserving existing Hermes delivery.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, model_validator

from cron import jobs as cron_jobs

from ._common import require_session_token

router = APIRouter()

_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")


class MyahJobMetadata(BaseModel):
    """Myah-owned cron metadata accepted by the dashboard endpoint."""

    chat_id: str | None = None
    chat_name: str | None = None
    adopted_at: str | None = None
    legacy_origin: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class MyahMetadataBody(BaseModel):
    """Allow only the Myah-owned top-level namespace and known subfields."""

    myah: MyahJobMetadata

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_myah(self) -> "MyahMetadataBody":
        if not self.myah.model_dump(exclude_none=True):
            raise ValueError("myah metadata must be a non-empty object")
        return self


def _validate_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=422, detail="Invalid job ID format")
    return job_id


@router.post("/cron/jobs/{job_id}/myah-metadata", dependencies=[Depends(require_session_token)])
async def patch_myah_metadata(
    body: MyahMetadataBody,
    job_id: str = Path(..., min_length=12, max_length=12),
) -> dict[str, Any]:
    """Merge ``body.myah`` into a Hermes cron job's ``job.myah`` metadata.

    ``cron.jobs.update_job`` is the upstream primitive that persists job JSON in
    the Hermes runtime. Passing only ``{"myah": ...}`` guarantees this endpoint
    cannot overwrite native ``origin`` or ``deliver`` values.
    """
    job_id = _validate_job_id(job_id)
    try:
        current_jobs = cron_jobs.load_jobs() or []
        current = next((j for j in current_jobs if isinstance(j, dict) and j.get("id") == job_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Job not found")
        current_value = current.get("myah")
        existing_myah = current_value if isinstance(current_value, dict) else {}
        patch_myah = body.myah.model_dump(exclude_none=True)
        merged_myah = {**existing_myah, **patch_myah}
        updated = cron_jobs.update_job(job_id, {"myah": merged_myah})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    if not updated:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job": updated}
