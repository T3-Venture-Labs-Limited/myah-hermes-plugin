"""Build bounded Brand Brain draft packages from collected evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .public_shopify import freeze_public_product_urls


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _image_urls(product: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for image in product.get("images") or []:
        if isinstance(image, dict):
            url = _first_str(image.get("url"), image.get("src"), image.get("originalSrc"))
        else:
            url = _first_str(image)
        if url:
            urls.append(url)
    return urls


def _api_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for product in products[:100]:
        handle = _first_str(product.get("handle"))
        url = _first_str(product.get("onlineStoreUrl"), product.get("url"))
        out.append(
            {
                "id": _first_str(product.get("id")),
                "title": _first_str(product.get("title"), product.get("name")) or "Untitled product",
                "handle": handle,
                "url": url,
                "description": _first_str(product.get("description"), product.get("body_html"), product.get("bodyHtml")),
                "vendor": _first_str(product.get("vendor")),
                "product_type": _first_str(product.get("product_type"), product.get("productType")),
                "tags": product.get("tags") or [],
                "image_urls": _image_urls(product),
                "source": "shopify_api",
            }
        )
    return out


def _public_products(urls: list[str]) -> list[dict[str, Any]]:
    return [
        {"title": url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title(), "url": url, "source": "public_storefront"}
        for url in freeze_public_product_urls(urls, cap=100)
    ]


def build_brand_package(
    *,
    shop_url: str,
    api_evidence: dict[str, Any] | None = None,
    theme_evidence: dict[str, Any] | None = None,
    scrape_evidence: dict[str, Any] | None = None,
    manual_evidence: dict[str, Any] | None = None,
    public_product_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Merge source evidence into a bounded draft Brand Brain package.

    Source precedence follows the spec:
    merchant/API evidence > theme settings/assets > scrape-light > manual fill.
    Public products are used only when Shopify API products are absent.
    """
    api = api_evidence or {}
    theme = theme_evidence or {}
    scrape = scrape_evidence or {}
    manual = manual_evidence or {}

    shop = api.get("shop") or scrape.get("shop") or manual.get("shop") or {}
    brand = api.get("brand") or {}
    visuals = scrape.get("visuals") or scrape.get("visual_identity") or manual.get("visual_identity") or {}
    theme_settings = theme.get("settings") or {}

    products = _api_products(list(api.get("products") or []))
    product_source = "shopify_api" if products else "public_storefront"
    if not products:
        products = _public_products(public_product_urls or [])

    colors = dict(brand.get("colors") or {})
    if not colors and isinstance(theme_settings.get("colors"), dict):
        colors.update(theme_settings["colors"])
    if visuals.get("colors"):
        colors.setdefault("primary", visuals["colors"][0])
        if len(visuals["colors"]) > 1:
            colors.setdefault("accent", visuals["colors"][1])

    typography = dict(theme_settings.get("typography") or {})
    if visuals.get("fonts"):
        typography.setdefault("body", visuals["fonts"][0])
        if len(visuals["fonts"]) > 1:
            typography.setdefault("fallback", visuals["fonts"][1])

    logo = brand.get("logo") or {}
    logo_url = _first_str(logo.get("url") if isinstance(logo, dict) else logo, visuals.get("logo_url"))

    visual_sources: list[str] = []
    if brand:
        visual_sources.append("shopify_api_brand")
    if theme:
        visual_sources.append("theme_assets")
    if scrape:
        visual_sources.append("scrape_light")
    if manual.get("visual_identity") or manual.get("brand_name") or manual.get("name") or manual.get("voice") or manual.get("shop"):
        visual_sources.append("manual")

    return {
        "version": 1,
        "created_at": _now(),
        "source_mode": "api_first" if api else "public_fallback",
        "shop_url": shop_url,
        "brand": {
            "name": _first_str(shop.get("name"), manual.get("brand_name"), manual.get("name")) or "Untitled brand",
            "domain": _first_str(shop.get("primary_domain"), shop.get("domain"), shop_url),
            "colors": colors,
            "typography": typography,
            "logo_url": logo_url,
            "favicon_url": _first_str(visuals.get("favicon_url")),
            "social_links": list(scrape.get("social_links") or []),
            "voice": _first_str(manual.get("voice")),
        },
        "visual_identity": {
            "colors": visuals.get("colors") or [],
            "fonts": visuals.get("fonts") or [],
            "logo_url": logo_url,
            "favicon_url": _first_str(visuals.get("favicon_url")),
        },
        "products": products,
        "content_sources": {
            "pages": list(api.get("pages") or []),
            "policies": list(api.get("policies") or []),
            "blogs": list(api.get("blogs") or []),
        },
        "evidence_summary": {
            "product_source": product_source,
            "visual_sources": visual_sources,
            "api_product_count": len(api.get("products") or []),
            "stored_product_count": len(products),
        },
    }
