"""Public Shopify fallback helpers.

Connected-mode product data should come from Shopify API evidence. This module is
only for no-connection public fallback and therefore freezes the first N sitemap
product URLs before any enrichment can drop/fill items.
"""

from __future__ import annotations

from collections.abc import Iterable


def freeze_public_product_urls(urls: Iterable[str], *, cap: int = 100) -> list[str]:
    """Return the first unique product URLs in discovered order, capped at ``cap``.

    This is intentionally order-preserving and does not backfill later products
    when an early URL later fails enrichment. The returned list is the frozen
    public-fallback product set.
    """
    frozen: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        frozen.append(url)
        if len(frozen) >= cap:
            break
    return frozen
