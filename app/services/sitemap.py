"""Sitemap and robots.txt (Part I §16).

§16 asks for "SEO basics: sitemap, meta tags, clean URLs". Meta tags and clean
URLs are in the templates and the URL builders; this is the sitemap.

Two decisions shape the file:

**Bilingual pages are one URL with alternates, not two.** §1 makes Arabic and
English equal, and §16 gives each name its own slug — so the same product is
reachable at two paths. Listing both as separate ``<url>`` entries tells a
search engine the shop has duplicate content. Instead each entry carries
``xhtml:link rel="alternate" hreflang="…"`` pointing at its counterpart, which
is what the protocol is for.

**The URL count is capped and chunked.** The sitemap protocol allows 50,000 URLs
per file. A bookshop is unlikely to reach that, but "unlikely" is how a shop
ends up serving a silently truncated sitemap after a good year, so the index is
built properly rather than assumed away.

Nothing here queries anything a customer could not already see: the sitemap is
generated from the same ``visible_products_stmt`` the storefront uses, so an
unpublished product cannot leak through it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from urllib.parse import quote
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.i18n import LANGUAGES
from app.models.catalog import Category, Product, ProductTag, Publisher, Tag
from app.services.catalog import (
    category_url,
    product_url,
    publisher_url,
    tag_url,
    visible_products_stmt,
)

#: The protocol's hard limit per file.
MAX_URLS_PER_FILE = 50_000

#: Pages that exist without a database row behind them.
STATIC_PATHS: tuple[tuple[str, str], ...] = (
    ("/", "daily"),
    ("/categories", "weekly"),
    ("/branches", "monthly"),
)


@dataclass(slots=True)
class SitemapUrl:
    """One ``<url>`` entry.

    ``alternates`` maps language code to path — the same page in the other
    language, so a search engine serves Arabic readers the Arabic slug.
    """

    path: str
    lastmod: dt.datetime | None = None
    changefreq: str = "weekly"
    priority: str | None = None
    alternates: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Collecting URLs
# ---------------------------------------------------------------------------


def collect_urls(db: Session) -> list[SitemapUrl]:
    """Every page worth indexing, in a stable order.

    Deliberately excludes anything behind a login, anything with a cart or
    session in it, and search result pages — none of them are a destination a
    search engine should send somebody to.
    """
    urls: list[SitemapUrl] = [
        SitemapUrl(path=path, changefreq=freq, priority="1.0" if path == "/" else None)
        for path, freq in STATIC_PATHS
    ]

    urls.extend(_category_urls(db))
    urls.extend(_product_urls(db))
    urls.extend(_publisher_urls(db))
    urls.extend(_tag_urls(db))
    return urls


def _bilingual(builder, row) -> tuple[str, dict[str, str]]:
    """Canonical path plus its per-language alternates.

    The canonical is the Arabic one: Arabic is the shop's primary language
    (§1), and picking a stable side stops the canonical flipping as content is
    edited.
    """
    alternates = {language: builder(row, language) for language in LANGUAGES}
    return alternates.get("ar") or next(iter(alternates.values())), alternates


def _category_urls(db: Session) -> list[SitemapUrl]:
    rows = db.scalars(
        select(Category)
        .where(
            Category.scd_active_flag.is_(True),
            Category.is_visible_flag.is_(True),
        )
        .order_by(Category.pk_category_id)
    ).all()

    result = []
    for row in rows:
        path, alternates = _bilingual(category_url, row)
        result.append(
            SitemapUrl(
                path=path,
                lastmod=row.scd_active_from,
                changefreq="weekly",
                priority="0.8",
                alternates=alternates,
            )
        )
    return result


def _product_urls(db: Session) -> list[SitemapUrl]:
    rows = db.scalars(
        visible_products_stmt().order_by(Product.pk_product_id)
    ).unique().all()

    result = []
    for row in rows:
        path, alternates = _bilingual(product_url, row)
        result.append(
            SitemapUrl(
                path=path,
                # The published date, not the SCD timestamp: a price change
                # should not tell a crawler the page was rewritten.
                lastmod=row.published_dt or row.scd_active_from,
                changefreq="weekly",
                priority="0.7",
                alternates=alternates,
            )
        )
    return result


def _publisher_urls(db: Session) -> list[SitemapUrl]:
    rows = db.scalars(
        select(Publisher)
        .where(Publisher.scd_active_flag.is_(True))
        .order_by(Publisher.pk_publisher_id)
    ).all()
    # One slug serves both languages here, so there is nothing to alternate.
    return [
        SitemapUrl(
            path=publisher_url(row),
            lastmod=row.scd_active_from,
            changefreq="monthly",
            priority="0.5",
        )
        for row in rows
    ]


def _tag_urls(db: Session) -> list[SitemapUrl]:
    """Tag landing pages, but only tags that lead somewhere (§15).

    A tag with no visible products is an empty page, and §17.4 is explicit
    about not shipping dead space — offering it to a crawler is worse than not
    having it.
    """
    used = select(ProductTag.fk_tag_id).where(ProductTag.scd_active_flag.is_(True))
    rows = db.scalars(
        select(Tag)
        .where(Tag.scd_active_flag.is_(True), Tag.pk_tag_id.in_(used))
        .order_by(Tag.pk_tag_id)
    ).all()
    return [
        SitemapUrl(
            path=tag_url(row),
            lastmod=row.scd_active_from,
            changefreq="monthly",
            priority="0.4",
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _absolute(path: str) -> str:
    """Fully qualified and percent-encoded.

    The Arabic slugs (§16) are non-ASCII, and the sitemap protocol requires
    URL-escaped values — a raw UTF-8 ``<loc>`` is invalid even though most
    crawlers accept it. ``safe`` keeps the path structure readable.
    """
    return f"{settings.app_base_url.rstrip('/')}{quote(path, safe='/-_.~')}"


def _attr(value: str) -> str:
    """Escape for an XML attribute — quotes included, unlike ``escape``."""
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _w3c(moment: dt.datetime | None) -> str | None:
    """W3C datetime, which is what the protocol requires.

    Naive values are treated as UTC rather than dropped: a missing lastmod is
    a lost signal, and everything stored here is UTC by convention (Part II
    §1).
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def render_urlset(urls: list[SitemapUrl]) -> str:
    """One ``<urlset>`` document."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]

    for url in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{escape(_absolute(url.path))}</loc>")

        lastmod = _w3c(url.lastmod)
        if lastmod:
            lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append(f"    <changefreq>{url.changefreq}</changefreq>")
        if url.priority:
            lines.append(f"    <priority>{url.priority}</priority>")

        for language, alternate in sorted(url.alternates.items()):
            lines.append(
                f'    <xhtml:link rel="alternate" hreflang="{language}"'
                f' href="{_attr(_absolute(alternate))}"/>'
            )
        lines.append("  </url>")

    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def render_index(count: int, lastmod: dt.datetime | None = None) -> str:
    """The ``<sitemapindex>`` pointing at each chunk."""
    stamp = _w3c(lastmod)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for page in range(count):
        lines.append("  <sitemap>")
        lines.append(
            f"    <loc>{escape(_absolute(f'/sitemap-{page + 1}.xml'))}</loc>"
        )
        if stamp:
            lines.append(f"    <lastmod>{stamp}</lastmod>")
        lines.append("  </sitemap>")
    lines.append("</sitemapindex>")
    return "\n".join(lines) + "\n"


def page_count(total_urls: int) -> int:
    """How many chunk files the current catalogue needs. Never zero."""
    return max((total_urls + MAX_URLS_PER_FILE - 1) // MAX_URLS_PER_FILE, 1)


def page_slice(urls: list[SitemapUrl], page: int) -> list[SitemapUrl]:
    """URLs belonging to a one-based chunk number."""
    start = (page - 1) * MAX_URLS_PER_FILE
    return urls[start : start + MAX_URLS_PER_FILE]


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

#: Paths no crawler should follow. Every one is either private, session-bound,
#: or an infinite space — a crawler working through filter permutations of
#: ``/search`` costs the shop bandwidth and indexes nothing useful.
DISALLOWED: tuple[str, ...] = (
    "/admin",
    "/account",
    "/auth",
    "/cart",
    "/checkout",
    "/compare",
    "/search",
    "/orders/track",
    "/media/tmp",
)


def render_robots() -> str:
    lines = ["User-agent: *"]
    lines.extend(f"Disallow: {path}" for path in DISALLOWED)
    lines.append("")
    lines.append(f"Sitemap: {_absolute('/sitemap.xml')}")
    return "\n".join(lines) + "\n"
