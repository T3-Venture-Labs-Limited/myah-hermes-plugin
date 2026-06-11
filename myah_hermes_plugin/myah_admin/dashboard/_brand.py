"""Brand Import dashboard routes for the Creative Director profile.

These routes run inside the profile-local Hermes dashboard process. They own
profile-local Brand Brain state and durable writes; the Myah platform should
proxy to these routes rather than writing `/data/.hermes` directly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...brand_import.package_builder import build_brand_package
from ...brand_import.source_adapters import BrandSourceEvidence, collect_brand_evidence
from ...brand_import.storage import BrandImportStore
from ._common import require_session_token

router = APIRouter(prefix="/brand", tags=["brand-import"])


class BrandImportStartRequest(BaseModel):
    shop_url: str = Field(..., min_length=1)
    connected_shopify: bool = False
    api_evidence: dict[str, Any] | None = None
    theme_evidence: dict[str, Any] | None = None
    scrape_evidence: dict[str, Any] | None = None
    manual_evidence: dict[str, Any] | None = None
    public_product_urls: list[str] | None = None


class BrandImportApproveRequest(BaseModel):
    job_id: str = Field(..., min_length=1)


@router.get("/status")
async def brand_status(_auth: None = Depends(require_session_token)) -> dict[str, Any]:
    return BrandImportStore().status()


@router.post("/import/start")
async def start_brand_import(
    request: BrandImportStartRequest,
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    has_fixture_evidence = any(
        [
            request.api_evidence,
            request.theme_evidence,
            request.scrape_evidence,
            request.manual_evidence,
            request.public_product_urls,
        ]
    )
    fixture = (
        BrandSourceEvidence(
            api_evidence=request.api_evidence,
            theme_evidence=request.theme_evidence,
            scrape_evidence=request.scrape_evidence,
            public_product_urls=list(request.public_product_urls or []),
            manual_evidence=request.manual_evidence,
        )
        if has_fixture_evidence
        else None
    )
    evidence = collect_brand_evidence(
        request.shop_url,
        connected_shopify=request.connected_shopify,
        fixture=fixture,
    )
    package = build_brand_package(
        shop_url=request.shop_url,
        api_evidence=evidence.api_evidence,
        theme_evidence=evidence.theme_evidence,
        scrape_evidence=evidence.scrape_evidence,
        public_product_urls=evidence.public_product_urls,
        manual_evidence=evidence.manual_evidence,
    )
    if evidence.warnings:
        package.setdefault("evidence_summary", {}).setdefault("warnings", []).extend(evidence.warnings)
    job = BrandImportStore().create_review_job(package)
    return {"job_id": job["job_id"], "status": job["status"], "package": package}


@router.post("/import/approve")
async def approve_brand_import(
    request: BrandImportApproveRequest,
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    try:
        active = BrandImportStore().approve(request.job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="brand import job not found")
    return active
