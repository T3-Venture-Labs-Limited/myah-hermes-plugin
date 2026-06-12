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


def _image_urls(product: dict[str, Any], *, field: str) -> list[str]:
    urls: list[str] = []
    for index, image in enumerate(_ensure_list(product.get("images"), field=field)):
        if isinstance(image, dict):
            url = _first_str(image.get("url"), image.get("src"), image.get("originalSrc"))
        elif isinstance(image, str):
            url = _first_str(image)
        else:
            raise ValueError(f"invalid {field}[{index}]: expected object or string")
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


def _tag_list(value: Any, *, field: str) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return _string_list(value, field=field)


def _dict_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    items = _ensure_list(value, field=field)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"invalid {field}[{index}]: expected object")
        out.append(item)
    return out


def _api_products(products: list[dict[str, Any]], *, source: str = "shopify_api") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, product in enumerate(products[:100]):
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
                "tags": _tag_list(product.get("tags"), field=f"api_evidence.products[{index}].tags"),
                "image_urls": _image_urls(product, field=f"api_evidence.products[{index}].images"),
                "source": source,
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

    api_products = _dict_list(api.get("products"), field="api_evidence.products")
    scrape_products = _dict_list(scrape.get("products"), field="scrape_evidence.products")
    products = _api_products(api_products, source="shopify_api")
    if not products and scrape_products:
        products = _api_products(scrape_products, source="scraped_storefront")
    manual_has_evidence = any(
        manual.get(key) for key in ("visual_identity", "brand_name", "name", "voice", "shop")
    )
    if api_products:
        product_source = "shopify_api"
    elif scrape_products:
        product_source = "scraped_storefront"
    elif public_product_urls:
        product_source = "public_storefront"
    elif manual_has_evidence:
        product_source = "manual"
    else:
        product_source = "none"
    if not products:
        products = _public_products(public_product_urls or [])

    visual_colors = _string_list(visuals.get("colors"), field="visual_identity.colors")
    visual_fonts = _string_list(visuals.get("fonts"), field="visual_identity.fonts")

    colors = dict(_ensure_mapping(brand.get("colors"), field="api_evidence.brand.colors"))
    theme_colors = _ensure_mapping(theme_settings.get("colors"), field="theme_evidence.settings.colors")
    if not colors and theme_colors:
        colors.update(theme_colors)
    if visual_colors:
        colors.setdefault("primary", visual_colors[0])
        if len(visual_colors) > 1:
            colors.setdefault("accent", visual_colors[1])

    typography = dict(_ensure_mapping(theme_settings.get("typography"), field="theme_evidence.settings.typography"))
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
        "source_mode": "api_first" if api else ("scraped_storefront" if scrape else "manual" if manual_has_evidence else "public_fallback"),
        "shop_url": shop_url,
        "brand": {
            "name": _first_str(shop.get("name"), manual.get("brand_name"), manual.get("name")) or "Untitled brand",
            "domain": _first_str(shop.get("primary_domain"), shop.get("domain"), shop_url),
            "colors": colors,
            "typography": typography,
            "logo_url": logo_url,
            "favicon_url": _first_str(visuals.get("favicon_url")),
            "social_links": _string_list(scrape.get("social_links"), field="scrape_evidence.social_links"),
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
            "api_product_count": len(api_products),
            "stored_product_count": len(products),
        },
    }
