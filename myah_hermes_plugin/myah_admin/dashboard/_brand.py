"""Brand Import dashboard routes for the Creative Director profile.

These routes run inside the profile-local Hermes dashboard process. They own
profile-local Brand Brain state and durable writes; the Myah platform should
proxy to these routes rather than writing `/data/.hermes` directly.
"""

from __future__ import annotations

import asyncio
from typing import Any
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ...brand_import.package_builder import build_brand_package
from ...brand_import.source_adapters import BrandSourceEvidence, collect_brand_evidence
from ...brand_import.storage import BrandImportStore, approval_blockers
from ._common import require_session_token

router = APIRouter(prefix="/brand", tags=["brand-import"])

MAX_IMPORT_TOTAL_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ITEMS = 100
MAX_OVERRIDE_PRODUCTS = 120
MAX_OVERRIDE_SOCIAL_LINKS = 20


def _request_payload(request: BaseModel) -> dict[str, Any]:
    if hasattr(request, "model_dump"):
        return request.model_dump(exclude_none=True)  # pydantic v2
    return request.dict(exclude_none=True)  # pydantic v1 fallback


def _reject_oversized_start_payload(request: "BrandImportStartRequest") -> None:
    payload = _request_payload(request)
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_IMPORT_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="brand import payload too large")

    list_checks = [
        (payload, "public_product_urls"),
        (payload.get("api_evidence") or {}, "products"),
        (payload.get("api_evidence") or {}, "pages"),
        (payload.get("api_evidence") or {}, "policies"),
        (payload.get("api_evidence") or {}, "blogs"),
    ]
    for container, key in list_checks:
        value = container.get(key) if isinstance(container, dict) else None
        if isinstance(value, list) and len(value) > MAX_IMPORT_ITEMS:
            raise HTTPException(status_code=413, detail=f"brand import {key} exceeds {MAX_IMPORT_ITEMS} item limit")


async def _read_limited_json_request(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        normalized_length = content_length.strip()
        if not re.fullmatch(r"[0-9]+", normalized_length):
            raise HTTPException(status_code=400, detail="invalid content length")
        if int(normalized_length) > MAX_IMPORT_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="brand import payload too large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_IMPORT_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="brand import payload too large")
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="brand import payload must not be empty")

    try:
        payload = json.loads(b"".join(chunks))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="brand import payload must be an object")
    return payload


def _validate_start_request(payload: dict[str, Any]) -> "BrandImportStartRequest":
    try:
        if hasattr(BrandImportStartRequest, "model_validate"):
            return BrandImportStartRequest.model_validate(payload)  # type: ignore[attr-defined]
        return BrandImportStartRequest.parse_obj(payload)
    except ValidationError as exc:
        try:
            detail = exc.errors(include_url=False)
        except TypeError:  # pydantic v1 fallback
            detail = exc.errors()
        raise HTTPException(status_code=422, detail=detail)


class BrandImportStartRequest(BaseModel):
    shop_url: str | None = Field(default=None)
    connected_shopify: bool = False  # accepted for backwards compatibility; Brand Import is scrape-first.
    api_evidence: dict[str, Any] | None = None
    theme_evidence: dict[str, Any] | None = None
    scrape_evidence: dict[str, Any] | None = None
    manual_evidence: dict[str, Any] | None = None
    public_product_urls: list[str] | None = None


class BrandImportApproveRequest(BaseModel):
    job_id: str = Field(..., min_length=1)
    file_assets: dict[str, Any] | None = None

class BrandImportOverrideRequest(BaseModel):
    job_id: str | None = Field(default=None)
    logo_data_url: str | None = None
    logo_filename: str | None = None
    logo_url: str | None = None
    typography: dict[str, Any] | None = None
    colors: dict[str, Any] | None = None
    social_links: list[str] | None = None
    products: list[dict[str, Any]] | None = None


@router.get("/status")
async def brand_status(_auth: None = Depends(require_session_token)) -> dict[str, Any]:
    return BrandImportStore().status()


@router.post("/import/start")
async def start_brand_import(
    raw_request: Request,
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    request = _validate_start_request(await _read_limited_json_request(raw_request))
    _reject_oversized_start_payload(request)
    has_fixture_evidence = any(
        [
            request.api_evidence,
            request.theme_evidence,
            request.scrape_evidence,
            request.manual_evidence,
            request.public_product_urls,
        ]
    )
    if not has_fixture_evidence and not (request.shop_url or "").strip():
        raise HTTPException(status_code=400, detail="Enter a storefront URL to import your brand.")
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
    try:
        evidence = await asyncio.to_thread(
            collect_brand_evidence,
            request.shop_url or "",
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if evidence.warnings:
        package.setdefault("evidence_summary", {}).setdefault("warnings", []).extend(evidence.warnings)
    blockers = approval_blockers(package)
    if not has_fixture_evidence and ("no_public_storefront_evidence" in evidence.warnings or "storefront_fetch_failed" in ";".join(evidence.warnings)):
        raise HTTPException(
            status_code=400,
            detail="We could not collect enough public brand evidence from that URL. Check the storefront URL and try again.",
        )
    if blockers and not has_fixture_evidence:
        raise HTTPException(
            status_code=400,
            detail="We could not collect enough public brand evidence from that URL. Check the storefront URL and try again.",
        )
    job = BrandImportStore().create_review_job(package)
    return {"job_id": job["job_id"], "status": job["status"], "package": package}


@router.post("/import/override")
async def override_brand_import(
    request: BrandImportOverrideRequest,
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    overrides = _request_payload(request)
    if request.products is not None and len(request.products) > MAX_OVERRIDE_PRODUCTS:
        raise HTTPException(status_code=413, detail=f"brand import products exceeds {MAX_OVERRIDE_PRODUCTS} item limit")
    if request.social_links is not None and len(request.social_links) > MAX_OVERRIDE_SOCIAL_LINKS:
        raise HTTPException(status_code=413, detail=f"brand import social_links exceeds {MAX_OVERRIDE_SOCIAL_LINKS} item limit")
    overrides.pop("job_id", None)
    try:
        store = BrandImportStore()
        if request.job_id:
            return store.override_review_job(request.job_id, overrides)
        return store.override_active(overrides)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        if str(exc).strip("'") == "active":
            raise HTTPException(status_code=404, detail="active brand import not found")
        raise HTTPException(status_code=404, detail="brand import job not found")


@router.post("/import/approve")
async def approve_brand_import(
    request: BrandImportApproveRequest,
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    try:
        active = BrandImportStore().approve(request.job_id, file_assets=request.file_assets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="brand import job not found")
    return active
