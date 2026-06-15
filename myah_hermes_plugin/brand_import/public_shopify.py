"""Public storefront scrape helpers for Brand Import.

The Creative Director onboarding path is intentionally scrape-first: a merchant
pastes their public storefront URL and Myah collects bounded, public brand/product
evidence without requiring Shopify/Composio auth.
"""

from __future__ import annotations

import html
import json
import re
import ipaddress
import socket
import urllib.request
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlparse, urlunparse
from typing import Any

from .source_adapters import BrandSourceEvidence

FetchFn = Callable[[str], bytes]

_MAX_HTML_BYTES = 750_000
_MAX_PRODUCTS_BYTES = 2_000_000
_PRODUCT_LIMIT = 100
_USER_AGENT = "Mozilla/5.0 MyahBrandImport/1.0 (+https://myah.ai)"
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
_META_RE = re.compile(
    r"<meta\s+[^>]*(?:property|name)=[\"'](?P<key>[^\"']+)[\"'][^>]*content=[\"'](?P<value>[^\"']*)[\"'][^>]*>",
    re.I,
)
_LINK_RE = re.compile(
    r"<link\s+[^>]*rel=[\"'][^\"']*(?:icon|shortcut icon)[^\"']*[\"'][^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>",
    re.I,
)
_FONT_URL_RE = re.compile(r"url\([\"']?(?P<url>[^\"')]+\.(?:woff2?|ttf|otf))(?:\?[^\"')]+)?[\"']?\)", re.I)
_FONT_LINK_RE = re.compile(
    r"<link\s+[^>]*(?:as=[\"']font[\"'][^>]*href=[\"'](?P<href1>[^\"']+)[\"']|href=[\"'](?P<href2>[^\"']+\.(?:woff2?|ttf|otf)(?:\?[^\"']*)?)[\"'][^>]*(?:as=[\"']font[\"'])?)[^>]*>",
    re.I,
)
_FONT_RE = re.compile(r"font-family\s*:\s*([^;}{]+)", re.I)
_SOCIAL_RE = re.compile(r"https?://(?:www\.)?(?:instagram\.com|tiktok\.com|facebook\.com|pinterest\.com|youtube\.com)/[^\"'\s<>]+", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _host_is_private(hostname: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("Could not resolve storefront host.") from exc
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_unspecified or any(ip in network for network in _PRIVATE_NETWORKS):
            return True
    return False


def _validate_public_fetch_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a valid public storefront URL.")
    if parsed.username or parsed.password:
        raise ValueError("Storefront URLs must not contain credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Storefront URLs must use port 80 or 443.") from exc
    if port and port not in {80, 443}:
        raise ValueError("Storefront URLs must use port 80 or 443.")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_unspecified or any(ip in network for network in _PRIVATE_NETWORKS):
            raise ValueError("Storefront URL must be public.")
    except ValueError as exc:
        if "Storefront URL must be public" in str(exc):
            raise
        if _host_is_private(parsed.hostname):
            raise ValueError("Storefront URL must resolve to a public address.")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _validate_public_fetch_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _strip_html(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean(text)


def freeze_public_product_urls(urls: Iterable[str], *, cap: int = 100) -> list[str]:
    """Return the first unique product URLs in discovered order, capped at ``cap``."""
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


def _normalize_store_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        raise ValueError("Enter a public storefront URL to import your brand.")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid public storefront URL.")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "", "", "", ""))


def _default_fetch(url: str, *, max_bytes: int = _MAX_HTML_BYTES) -> bytes:
    _validate_public_fetch_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    with opener.open(req, timeout=15) as response:
        _validate_public_fetch_url(response.geturl())
        return response.read(max_bytes + 1)[:max_bytes]


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = html.unescape(re.sub(r"\s+", " ", value)).strip()
    return cleaned or None


def _meta_map(html_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _META_RE.finditer(html_text):
        key = match.group("key").lower().strip()
        value = _clean(match.group("value"))
        if value and key not in out:
            out[key] = value
    return out


def _title(html_text: str) -> str | None:
    match = _TITLE_RE.search(html_text)
    title = _clean(match.group(1)) if match else None
    if title and "|" in title:
        return title.split("|", 1)[0].strip()
    return title


def _fonts(html_text: str) -> list[str]:
    fonts: list[str] = []
    seen: set[str] = set()
    for match in _FONT_RE.finditer(html_text[:_MAX_HTML_BYTES]):
        for raw in match.group(1).split(","):
            font = raw.strip().strip('"\'')
            if not font or font.lower() in {"sans-serif", "serif", "monospace", "inherit", "system-ui"}:
                continue
            if font not in seen:
                seen.add(font)
                fonts.append(font)
            if len(fonts) >= 8:
                return fonts
    return fonts


def _font_urls(html_text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    bounded = html_text[:_MAX_HTML_BYTES]
    for match in _FONT_URL_RE.finditer(bounded):
        raw = match.group("url")
        url = urljoin(base_url.rstrip("/") + "/", raw)
        if url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= 12:
            return urls
    for match in _FONT_LINK_RE.finditer(bounded):
        raw = match.group("href1") or match.group("href2")
        if not raw:
            continue
        url = urljoin(base_url.rstrip("/") + "/", raw)
        if url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= 12:
            return urls
    return urls


def _images(product: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    images = product.get("images") or []
    if isinstance(images, list):
        for image in images[:8]:
            if isinstance(image, dict):
                url = _clean(image.get("src") or image.get("url") or image.get("originalSrc"))
                if url:
                    urls.append(url)
            elif isinstance(image, str) and image.strip():
                urls.append(image.strip())
    return urls


def _scrape_products(base_url: str, fetch: FetchFn | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    warnings: list[str] = []
    root_url = urlunparse((urlparse(base_url).scheme, urlparse(base_url).netloc, "/", "", "", ""))
    products_url = urljoin(root_url, "products.json?limit=100")
    try:
        data = fetch(products_url) if fetch else _default_fetch(products_url, max_bytes=_MAX_PRODUCTS_BYTES)
        payload = json.loads(_text(data))
    except Exception as exc:  # public storefronts may hide products.json
        return [], [], [f"products_json_unavailable:{exc.__class__.__name__}"]
    raw_products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(raw_products, list):
        return [], [], ["products_json_invalid"]
    products: list[dict[str, Any]] = []
    urls: list[str] = []
    for product in raw_products[:_PRODUCT_LIMIT]:
        if not isinstance(product, dict):
            continue
        handle = _clean(str(product.get("handle") or ""))
        url = _clean(product.get("url")) or (urljoin(root_url, f"products/{handle}") if handle else None)
        title = _clean(product.get("title") or product.get("name")) or (handle.replace("-", " ").title() if handle else "Untitled product")
        item = {
            "id": str(product.get("id")) if product.get("id") is not None else None,
            "title": title,
            "handle": handle,
            "url": url,
            "description": _strip_html(product.get("body_html") or product.get("bodyHtml") or product.get("description")),
            "vendor": _clean(product.get("vendor")),
            "product_type": _clean(product.get("product_type") or product.get("productType")),
            "tags": product.get("tags") if isinstance(product.get("tags"), list) else [],
            "image_urls": _images(product),
            "source": "scraped_storefront",
        }
        products.append(item)
        if url:
            urls.append(url)
    if not products:
        warnings.append("no_products_json_products")
    return products, freeze_public_product_urls(urls, cap=_PRODUCT_LIMIT), warnings


def scrape_public_storefront(shop_url: str, *, fetch: FetchFn | None = None) -> BrandSourceEvidence:
    """Collect public brand evidence from a storefront URL without auth.

    The scraper is deliberately bounded and deterministic: homepage HTML plus
    Shopify-compatible `/products.json?limit=100`. It returns enough evidence to
    create an approvable Brand Brain draft, or explicit warnings when no public
    evidence was usable.
    """
    base_url = _normalize_store_url(shop_url)
    warnings: list[str] = []
    try:
        html_bytes = fetch(base_url) if fetch else _default_fetch(base_url)
    except Exception as exc:
        return BrandSourceEvidence(warnings=[f"storefront_fetch_failed:{exc.__class__.__name__}"])
    html_text = _text(html_bytes)
    meta = _meta_map(html_text)
    title = _title(html_text)
    logo = meta.get("og:image") or meta.get("twitter:image")
    favicon_match = _LINK_RE.search(html_text)
    favicon = favicon_match.group("href") if favicon_match else None
    products, product_urls, product_warnings = _scrape_products(base_url, fetch)
    warnings.extend(product_warnings)

    colors = []
    seen_colors: set[str] = set()
    for color in _HEX_RE.findall(html_text[:_MAX_HTML_BYTES]):
        normalized = color.upper()
        if normalized not in seen_colors:
            seen_colors.add(normalized)
            colors.append(normalized)
        if len(colors) >= 12:
            break

    scrape_evidence = {
        "shop": {
            "name": meta.get("og:site_name") or meta.get("application-name") or title,
            "domain": urlparse(base_url).netloc,
            "primary_domain": urlparse(base_url).netloc,
        },
        "visuals": {
            "colors": colors,
            "fonts": _fonts(html_text),
            "font_urls": _font_urls(html_text, base_url),
            "logo_url": urljoin(base_url.rstrip("/") + "/", logo) if logo else None,
            "favicon_url": urljoin(base_url.rstrip("/") + "/", favicon) if favicon else None,
        },
        "social_links": freeze_public_product_urls(_SOCIAL_RE.findall(html_text), cap=20),
        "products": products,
    }
    has_brand = bool(scrape_evidence["shop"].get("name") or scrape_evidence["visuals"].get("logo_url") or colors)
    if not has_brand and not products:
        warnings.append("no_public_storefront_evidence")
    return BrandSourceEvidence(scrape_evidence=scrape_evidence, public_product_urls=product_urls, warnings=warnings)
