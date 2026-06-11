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


def _ensure_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"invalid {field}: expected object")
    return value


def _ensure_list(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"invalid {field}: expected list")
    return value


def _string_list(value: Any, *, field: str) -> list[str]:
    items = _ensure_list(value, field=field)
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _dict_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    items = _ensure_list(value, field=field)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"invalid {field}[{index}]: expected object")
        out.append(item)
    return out


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
    shop_url: str | None,
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
    api = _ensure_mapping(api_evidence, field="api_evidence")
    theme = _ensure_mapping(theme_evidence, field="theme_evidence")
    scrape = _ensure_mapping(scrape_evidence, field="scrape_evidence")
    manual = _ensure_mapping(manual_evidence, field="manual_evidence")

    shop = _ensure_mapping(api.get("shop") or scrape.get("shop") or manual.get("shop"), field="shop")
    brand = _ensure_mapping(api.get("brand"), field="api_evidence.brand")
    visuals = _ensure_mapping(
        scrape.get("visuals") or scrape.get("visual_identity") or manual.get("visual_identity"),
        field="visual_identity",
    )
    theme_settings = _ensure_mapping(theme.get("settings"), field="theme_evidence.settings")

    products = _api_products(_dict_list(api.get("products"), field="api_evidence.products"))
    manual_has_evidence = any(
        manual.get(key) for key in ("visual_identity", "brand_name", "name", "voice", "shop")
    )
    product_source = "shopify_api" if products else ("public_storefront" if public_product_urls else ("manual" if manual_has_evidence else "none"))
    if not products:
        products = _public_products(public_product_urls or [])

    visual_colors = _string_list(visuals.get("colors"), field="visual_identity.colors")
    visual_fonts = _string_list(visuals.get("fonts"), field="visual_identity.fonts")

    colors = dict(brand.get("colors") or {})
    if not colors and isinstance(theme_settings.get("colors"), dict):
        colors.update(theme_settings["colors"])
    if visual_colors:
        colors.setdefault("primary", visual_colors[0])
        if len(visual_colors) > 1:
            colors.setdefault("accent", visual_colors[1])

    typography = dict(theme_settings.get("typography") or {})
    if visual_fonts:
        typography.setdefault("body", visual_fonts[0])
        if len(visual_fonts) > 1:
            typography.setdefault("fallback", visual_fonts[1])

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
            "colors": visual_colors,
            "fonts": visual_fonts,
            "logo_url": logo_url,
            "favicon_url": _first_str(visuals.get("favicon_url")),
        },
        "products": products,
        "content_sources": {
            "pages": _dict_list(api.get("pages"), field="api_evidence.pages"),
            "policies": _dict_list(api.get("policies"), field="api_evidence.policies"),
            "blogs": _dict_list(api.get("blogs"), field="api_evidence.blogs"),
        },
        "evidence_summary": {
            "product_source": product_source,
            "visual_sources": visual_sources,
            "api_product_count": len(api.get("products") or []),
            "stored_product_count": len(products),
        },
    }
