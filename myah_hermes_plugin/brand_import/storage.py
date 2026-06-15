"""File-backed Brand Import state for profile-local Hermes containers."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_JOB_ID_RE = re.compile(r"^brand-[0-9a-f]{12}$")
_MAX_SAFE_TEXT = 120
logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any, *, max_len: int = _MAX_SAFE_TEXT) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("---", "-")
    # Drop direct prompt-injection phrases that can arrive from scraped pages.
    text = re.sub(r"ignore previous instructions", "", text, flags=re.IGNORECASE).strip()
    return text[:max_len].strip()


def _valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.fullmatch(job_id or ""))


def approval_blockers(package: dict[str, Any]) -> list[str]:
    """Return server-authoritative blockers that prevent Brand Brain approval."""
    summary = package.get("evidence_summary") or {}
    warnings = set(summary.get("warnings") or [])
    blockers = sorted(warnings.intersection({"no_fixture_evidence", "no_public_storefront_evidence", "connected_shopify_adapter_not_configured"}))
    if blockers and (summary.get("stored_product_count") or 0) == 0 and not (summary.get("visual_sources") or []):
        return blockers
    return []


def _annotate_approval_state(job: dict[str, Any]) -> dict[str, Any]:
    """Attach approvable/approval_blockers without mutating persisted job files."""
    package = job.get("package") if isinstance(job, dict) else None
    blockers = approval_blockers(package or {}) if isinstance(package, dict) else []
    annotated = {**job}
    annotated["approval_blockers"] = blockers
    annotated["approvable"] = not blockers and job.get("status") == "needs_review"
    return annotated


def _hermes_home() -> Path:
    from myah_hermes_plugin.myah_admin.dashboard._common import hermes_home_path

    return hermes_home_path()


def _wiki_root() -> Path:
    from myah_hermes_plugin.myah_admin.dashboard._wiki_paths import wiki_root

    return wiki_root()


def _atomic_json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping unreadable Brand Import JSON %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("Skipping non-object Brand Import JSON %s", path)
        return None
    return payload


def _apply_package_overrides(package: dict[str, Any], overrides: dict[str, Any]) -> None:
    brand = package.setdefault("brand", {})
    if not isinstance(brand, dict):
        raise ValueError("brand import package has invalid brand data")
    manual = package.setdefault("manual_overrides", {})
    if not isinstance(manual, dict):
        manual = {}
        package["manual_overrides"] = manual
    visual_identity = package.setdefault("visual_identity", {})
    if not isinstance(visual_identity, dict):
        visual_identity = {}
        package["visual_identity"] = visual_identity

    if overrides.get("logo_data_url"):
        logo_data_url = str(overrides["logo_data_url"])
        if not logo_data_url.startswith("data:image/") or len(logo_data_url) > 2_000_000:
            raise ValueError("uploaded logo must be an image under 2MB")
        brand["logo_url"] = logo_data_url
        visual_identity["logo_url"] = logo_data_url
        manual["logo_filename"] = _safe_text(overrides.get("logo_filename"), max_len=240)
        manual["logo_source"] = "uploaded"
    elif overrides.get("logo_url"):
        logo_url = _safe_text(overrides.get("logo_url"), max_len=1000)
        brand["logo_url"] = logo_url
        visual_identity["logo_url"] = logo_url
        manual["logo_source"] = "manual_url"
    if isinstance(overrides.get("typography"), dict):
        brand["typography"] = {str(k): _safe_text(v, max_len=120) for k, v in overrides["typography"].items() if v}
        manual["typography"] = True
    if isinstance(overrides.get("colors"), dict):
        brand["colors"] = {str(k): _safe_text(v, max_len=40) for k, v in overrides["colors"].items() if v}
        manual["colors"] = True


def _corrupt_job_marker(path: Path) -> dict[str, Any]:
    return {
        "job_id": path.stem,
        "status": "failed",
        "error": "corrupt_brand_import_job",
        "updated_at": _utc_now(),
    }


class BrandImportStore:
    """Small JSON/file store for Brand Import jobs and active Brand Brain."""

    def __init__(self, *, hermes_home: Path | None = None, wiki_root: Path | None = None, profile_id: str = "creative-director") -> None:
        self.hermes_home = hermes_home or _hermes_home()
        self.profile_id = profile_id
        self.profile_home = self._profile_home()
        self.wiki_root = wiki_root or _wiki_root()
        self.root = self.profile_home / "brand_import"
        self.jobs_dir = self.root / "jobs"
        self.active_path = self.root / "active.json"

    def _profile_home(self) -> Path:
        # The dashboard process currently runs once from root HERMES_HOME, while
        # per-profile gateways run separately. Brand Import is Creative
        # Director-specific, so durable job state and generated profile-local
        # skills must live under root/profiles/creative-director. If this route
        # ever moves into a profile-local dashboard, HERMES_HOME may already be
        # the profile directory; in that case, use it directly.
        if self.hermes_home.name == self.profile_id:
            return self.hermes_home
        return self.hermes_home / "profiles" / self.profile_id

    def create_review_job(self, package: dict[str, Any]) -> dict[str, Any]:
        job_id = f"brand-{uuid.uuid4().hex[:12]}"
        job = {
            "job_id": job_id,
            "status": "needs_review",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "package": package,
        }
        _atomic_json_dump(self.jobs_dir / f"{job_id}.json", job)
        return _annotate_approval_state(job)

    def override_review_job(self, job_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
        """Apply user-supplied review overrides before approval."""
        job = self.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job.get("status") != "needs_review":
            raise ValueError("brand import overrides can only be applied before approval")
        package = job.get("package")
        if not isinstance(package, dict):
            raise ValueError("brand import job is missing a package")
        _apply_package_overrides(package, overrides)
        job["updated_at"] = _utc_now()
        _atomic_json_dump(self.jobs_dir / f"{job_id}.json", job)
        return _annotate_approval_state(job)

    def override_active(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """Apply user-supplied overrides to the approved active Brand Brain."""
        active = self.active()
        if not active:
            raise KeyError("active")
        package = active.get("package")
        if not isinstance(package, dict):
            raise ValueError("active brand import manifest is missing a package")
        _apply_package_overrides(package, overrides)
        active["updated_at"] = _utc_now()
        _atomic_json_dump(self.active_path, active)

        approved_job_id = active.get("approved_job_id")
        if isinstance(approved_job_id, str) and _valid_job_id(approved_job_id):
            job = self.get_job(approved_job_id)
            if job and isinstance(job.get("package"), dict):
                job["package"] = package
                job["updated_at"] = active["updated_at"]
                _atomic_json_dump(self.jobs_dir / f"{approved_job_id}.json", job)

        self.write_brand_brain(package)
        self.write_brand_style_skill(package)
        return active

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not _valid_job_id(job_id):
            raise ValueError("invalid brand import job id")
        path = self.jobs_dir / f"{job_id}.json"
        if not path.exists():
            return None
        return _load_json_object(path)

    def latest_job(self) -> dict[str, Any] | None:
        if not self.jobs_dir.exists():
            return None
        candidates: list[tuple[int, str, Path]] = []
        for path in self.jobs_dir.glob("brand-*.json"):
            if not _valid_job_id(path.stem):
                continue
            try:
                candidates.append((path.stat().st_mtime_ns, path.name, path))
            except OSError as exc:
                logger.warning("Skipping Brand Import job that disappeared during status lookup %s: %s", path, exc)
        for _mtime, _name, path in sorted(candidates, reverse=True):
            job = _load_json_object(path)
            if job is None:
                return _corrupt_job_marker(path)
            return job
        return None

    def active(self) -> dict[str, Any] | None:
        if not self.active_path.exists():
            return None
        return _load_json_object(self.active_path)

    def status(self) -> dict[str, Any]:
        active = self.active()
        job = self.latest_job()
        if active and job and job.get("status") == "failed":
            return {"status": "active", "active": active, "current_job": _annotate_approval_state(job)}
        if job and job.get("status") != "active":
            annotated_job = _annotate_approval_state(job)
            return {"status": job.get("status", "unknown"), "active": active, "current_job": annotated_job}
        if active:
            return {"status": "active", "active": active, "current_job": _annotate_approval_state(job) if job else None}
        return {"status": "empty", "active": None, "current_job": None}

    def approve(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        package = job["package"]
        if approval_blockers(package):
            raise ValueError("brand import needs evidence before approval")
        self.write_brand_brain(package)
        self.write_brand_style_skill(package)
        job["status"] = "active"
        job["updated_at"] = _utc_now()
        _atomic_json_dump(self.jobs_dir / f"{job_id}.json", job)
        active = {"status": "active", "approved_job_id": job_id, "approved_at": _utc_now(), "package": package}
        _atomic_json_dump(self.active_path, active)
        return active

    def write_brand_brain(self, package: dict[str, Any]) -> None:
        brand = package.get("brand") or {}
        brand_dir = self.wiki_root / "brand"
        name = _safe_text(brand.get("name")) or "Brand"
        colors = brand.get("colors") or {}
        typography = brand.get("typography") or {}
        voice = _safe_text(brand.get("voice"), max_len=500)
        readme = [
            f"# {name}",
            "",
            "## Source summary",
            f"- Source mode: `{package.get('source_mode')}`",
            f"- Shop URL: {_safe_text(package.get('shop_url'), max_len=500)}",
            f"- Product source: `{(package.get('evidence_summary') or {}).get('product_source')}`",
            "",
            "## Brand identity",
            f"- Domain: {_safe_text(brand.get('domain'), max_len=240)}",
            f"- Logo URL: {_safe_text(brand.get('logo_url'), max_len=500)}",
            f"- Favicon URL: {_safe_text(brand.get('favicon_url'), max_len=500)}",
            f"- Colors: {json.dumps(colors, sort_keys=True)}",
            f"- Typography: {json.dumps(typography, sort_keys=True)}",
            f"- Voice: {voice}",
            "",
        ]

        products_lines = ["# Products", ""]
        for index, product in enumerate(package.get("products") or []):
            if not isinstance(product, dict):
                raise ValueError(f"invalid products[{index}]: expected object")
            title = _safe_text(product.get("title"), max_len=160) or "Untitled product"
            products_lines.append(f"## {title}")
            if product.get("url"):
                products_lines.append(f"- URL: {_safe_text(product['url'], max_len=500)}")
            if product.get("handle"):
                products_lines.append(f"- Handle: {_safe_text(product['handle'])}")
            if product.get("product_type"):
                products_lines.append(f"- Product type: {_safe_text(product['product_type'], max_len=160)}")
            if product.get("vendor"):
                products_lines.append(f"- Vendor: {_safe_text(product['vendor'], max_len=160)}")
            if product.get("description"):
                products_lines.append(f"- Description: {_safe_text(product['description'], max_len=1000)}")
            tags = product.get("tags") or []
            if tags:
                safe_tags = [_safe_text(tag, max_len=80) for tag in tags if _safe_text(tag, max_len=80)]
                if safe_tags:
                    products_lines.append(f"- Tags: {', '.join(safe_tags)}")
            for image in product.get("image_urls") or []:
                products_lines.append(f"- Image: {_safe_text(image, max_len=500)}")
            products_lines.append("")

        content_sources = package.get("content_sources") or {}
        content_lines = ["# Source Content", ""]
        for section in ("pages", "policies", "blogs"):
            content_lines.append(f"## {section.title()}")
            for index, item in enumerate(content_sources.get(section) or []):
                if not isinstance(item, dict):
                    raise ValueError(f"invalid content_sources.{section}[{index}]: expected object")
                title = _safe_text(item.get("title") or item.get("name"), max_len=160) or "Untitled"
                body = _safe_text(item.get("body") or item.get("body_html") or item.get("bodyHtml"), max_len=500)
                content_lines.append(f"### {title}")
                if body:
                    content_lines.append(body)
                content_lines.append("")

        visual = [
            "# Visual System",
            "",
            f"Colors: `{json.dumps(colors, sort_keys=True)}`",
            "",
            f"Typography: `{json.dumps(typography, sort_keys=True)}`",
            "",
        ]

        brand_dir.mkdir(parents=True, exist_ok=True)
        _atomic_text_write(brand_dir / "README.md", "\n".join(readme))
        _atomic_text_write(brand_dir / "products.md", "\n".join(products_lines))
        _atomic_text_write(brand_dir / "source-content.md", "\n".join(content_lines))
        _atomic_text_write(brand_dir / "visual-system.md", "\n".join(visual))

    def write_brand_style_skill(self, package: dict[str, Any]) -> None:
        brand = package.get("brand") or {}
        name = _safe_text(brand.get("name")) or "the brand"
        voice = _safe_text(brand.get("voice"), max_len=500)
        skill_dir = self.profile_home / "skills" / "brand-style-guide"
        skill_dir.mkdir(parents=True, exist_ok=True)
        slug_name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "brand"
        description = _safe_text(f"Brand style guide for {name}; use when drafting Creative Director ecommerce content for this profile.", max_len=220)
        escaped_description = description.replace("\\", "\\\\").replace('"', '\\"')
        content = f"""---
name: brand-style-guide
description: "{escaped_description}"
---

# Brand Style Guide

Brand: {name}
Canonical brand slug: `{slug_name}`

## Brand voice

{voice or 'No explicit brand voice was imported yet.'}

## Source

Generated by Myah Creative Director Brand Import from approved Brand Brain package.

## Use

- Preserve the brand's tone and ecommerce positioning.
- Prefer product facts from `brand/products.md`.
- Treat imported fields as editable user-approved context, not immutable truth.
"""
        _atomic_text_write(skill_dir / "SKILL.md", content)
