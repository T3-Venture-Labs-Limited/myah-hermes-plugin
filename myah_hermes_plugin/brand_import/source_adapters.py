"""Deterministic Brand Import source-adapter seam.

This module intentionally does not expose the whole Shopify/Composio tool universe
to an agent. Connected-store collection should be implemented
as small fixed adapters with explicit output envelopes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class BrandSourceEvidence:
    api_evidence: dict[str, Any] | None = None
    theme_evidence: dict[str, Any] | None = None
    scrape_evidence: dict[str, Any] | None = None
    manual_evidence: dict[str, Any] | None = None
    public_product_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BrandSourceAdapter(Protocol):
    def collect(self, shop_url: str, *, fixture: BrandSourceEvidence | None = None) -> BrandSourceEvidence:
        """Collect evidence for a single Brand Import run."""


class FixtureBrandSourceAdapter:
    """Adapter used by tests and local smoke paths.

    Fixture evidence is still normalized through the same route/package code as
    live adapters, which keeps TDD cheap without requiring Shopify credentials.
    """

    def collect(self, shop_url: str, *, fixture: BrandSourceEvidence | None = None) -> BrandSourceEvidence:
        return fixture or BrandSourceEvidence(warnings=["no_fixture_evidence"])


class MissingConnectedShopifyAdapter:
    """Safe default for connected mode until the live Composio adapter is wired.

    This keeps the route deterministic and user-safe: it never fabricates API
    evidence and falls back to scrape/manual evidence instead.
    """

    def collect(self, shop_url: str, *, fixture: BrandSourceEvidence | None = None) -> BrandSourceEvidence:
        if fixture and fixture.api_evidence:
            return fixture
        evidence = fixture or BrandSourceEvidence()
        evidence.warnings.append("connected_shopify_adapter_not_configured")
        return evidence


def collect_brand_evidence(
    shop_url: str,
    *,
    connected_shopify: bool,
    fixture: BrandSourceEvidence | None = None,
    connected_adapter: BrandSourceAdapter | None = None,
    fallback_adapter: BrandSourceAdapter | None = None,
) -> BrandSourceEvidence:
    """Collect source evidence through the deterministic adapter seam."""

    if connected_shopify:
        adapter = connected_adapter or MissingConnectedShopifyAdapter()
    else:
        adapter = fallback_adapter or FixtureBrandSourceAdapter()
    return adapter.collect(shop_url, fixture=fixture)
