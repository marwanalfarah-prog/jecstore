"""Sitemap and robots.txt (Part I §16).

Two things make this worth testing rather than eyeballing. First, a sitemap is
machine-read: an invalid document fails silently, months before anyone notices
the shop is not being indexed — so the output is parsed as XML here, not
string-matched. Second, a sitemap is a public listing of everything the shop
has, which makes it the easiest place in the codebase to leak a product that is
not meant to be visible yet.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import utcnow
from app.models.catalog import Category, Product, ProductTag, Publisher, Tag
from app.services import sitemap
from tests.test_checkout import db, store  # noqa: F401

NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "xhtml": "http://www.w3.org/1999/xhtml",
}


def _parse(xml: str) -> ElementTree.Element:
    """Parsing is the assertion: malformed XML raises here."""
    return ElementTree.fromstring(xml)


def _locations(xml: str) -> list[str]:
    return [
        unquote(element.text or "")
        for element in _parse(xml).findall("sm:url/sm:loc", NS)
    ]


@pytest.fixture
def catalogue(db: Session, store: dict) -> dict:
    """A publisher, a tag applied to the product, and a second hidden product."""
    now = utcnow()

    publisher = Publisher(
        name_ar="دار النشر", name_en="The Press", slug="the-press",
        scd_active_from=now,
    )
    tag = Tag(name_ar="صلاة", name_en="Prayer", slug="prayer", scd_active_from=now)
    db.add_all([publisher, tag])
    db.flush()

    db.add(
        ProductTag(
            fk_product_id=store["product"].pk_product_id,
            fk_tag_id=tag.pk_tag_id,
            scd_active_from=now,
        )
    )

    hidden = Product(
        name_ar="مسودة", name_en="Draft",
        slug_ar="مسودة", slug_en="draft",
        base_price_amt=1,
        is_visible_flag=False,
        scd_active_from=now,
    )
    db.add(hidden)
    db.commit()

    return {"publisher": publisher, "tag": tag, "hidden": hidden}


# ---------------------------------------------------------------------------
# What is listed
# ---------------------------------------------------------------------------


def test_the_document_is_valid_xml(db: Session, store: dict):
    _parse(sitemap.render_urlset(sitemap.collect_urls(db)))


def test_the_homepage_is_listed_first(db: Session):
    locations = _locations(sitemap.render_urlset(sitemap.collect_urls(db)))
    assert locations[0].endswith("/")


def test_visible_products_are_listed(db: Session, store: dict, catalogue):
    locations = _locations(sitemap.render_urlset(sitemap.collect_urls(db)))
    assert any(f"/p/{store['product'].pk_product_id}" in loc for loc in locations)


def test_a_hidden_product_is_never_listed(db: Session, catalogue):
    """The sitemap is a public inventory; an unpublished product must not
    appear in it before it appears on the site."""
    locations = _locations(sitemap.render_urlset(sitemap.collect_urls(db)))
    assert not any(f"/p/{catalogue['hidden'].pk_product_id}" in loc for loc in locations)
    assert not any("draft" in loc for loc in locations)


def test_a_hidden_category_is_never_listed(db: Session, store: dict):
    category = db.query(Category).first()
    category.is_visible_flag = False
    db.commit()

    locations = _locations(sitemap.render_urlset(sitemap.collect_urls(db)))
    assert not any(f"/c/{category.pk_category_id}" in loc for loc in locations)


def test_publishers_and_used_tags_are_listed(db: Session, catalogue):
    locations = _locations(sitemap.render_urlset(sitemap.collect_urls(db)))
    assert any("/publisher/" in loc for loc in locations)
    assert any("/tag/" in loc for loc in locations)


def test_a_tag_nobody_uses_is_left_out(db: Session, store: dict):
    """It would be an empty landing page — worse than not having one."""
    db.add(Tag(name_ar="مهجور", name_en="Orphan", slug="orphan", scd_active_from=utcnow()))
    db.commit()

    locations = _locations(sitemap.render_urlset(sitemap.collect_urls(db)))
    assert not any("orphan" in loc for loc in locations)


def test_private_pages_are_not_listed(db: Session, store: dict):
    """Nothing behind a login, and nothing session-bound."""
    locations = _locations(sitemap.render_urlset(sitemap.collect_urls(db)))
    for private in ("/admin", "/account", "/cart", "/checkout", "/auth", "/search"):
        assert not any(urlparse(loc).path.startswith(private) for loc in locations), private


# ---------------------------------------------------------------------------
# Bilingual URLs (Part I §1, §16)
# ---------------------------------------------------------------------------


def test_a_product_appears_once_with_alternates_not_twice(db: Session, store: dict):
    """Two entries for the same product reads as duplicate content."""
    root = _parse(sitemap.render_urlset(sitemap.collect_urls(db)))
    product_id = store["product"].pk_product_id

    entries = [
        element
        for element in root.findall("sm:url", NS)
        if f"/p/{product_id}" in unquote(element.find("sm:loc", NS).text)
    ]
    assert len(entries) == 1

    alternates = entries[0].findall("xhtml:link", NS)
    assert {link.get("hreflang") for link in alternates} == {"ar", "en"}


def test_the_alternates_carry_each_languages_own_slug(db: Session, store: dict):
    root = _parse(sitemap.render_urlset(sitemap.collect_urls(db)))
    product_id = store["product"].pk_product_id

    entry = next(
        element
        for element in root.findall("sm:url", NS)
        if f"/p/{product_id}" in unquote(element.find("sm:loc", NS).text)
    )
    by_language = {
        link.get("hreflang"): unquote(link.get("href"))
        for link in entry.findall("xhtml:link", NS)
    }

    assert by_language["en"].endswith(store["product"].slug_en)
    assert by_language["ar"].endswith(store["product"].slug_ar)


def test_arabic_slugs_are_percent_encoded(db: Session, store: dict):
    """A raw UTF-8 ``<loc>`` is invalid per the protocol, however tolerant
    crawlers are in practice."""
    xml = sitemap.render_urlset(sitemap.collect_urls(db))
    locations = [element.text for element in _parse(xml).findall("sm:url/sm:loc", NS)]

    assert any("%D" in loc for loc in locations), "expected an encoded Arabic slug"
    for loc in locations:
        assert loc.isascii(), loc


# ---------------------------------------------------------------------------
# The document itself
# ---------------------------------------------------------------------------


def test_urls_are_absolute(db: Session, store: dict):
    """A relative <loc> is invalid, and a crawler cannot resolve it."""
    for loc in _locations(sitemap.render_urlset(sitemap.collect_urls(db))):
        parsed = urlparse(loc)
        assert parsed.scheme and parsed.netloc, loc


def test_the_base_url_is_configurable(db: Session, monkeypatch, store: dict):
    """The sitemap is the one place a wrong base URL is actively harmful — it
    publishes the mistake to search engines."""
    monkeypatch.setattr(settings, "app_base_url", "https://jecstore.jo/")

    for loc in _locations(sitemap.render_urlset(sitemap.collect_urls(db))):
        assert loc.startswith("https://jecstore.jo/")
        assert "//" not in loc[len("https://") :], "no doubled slash from the base"


def test_lastmod_is_a_w3c_timestamp(db: Session, store: dict):
    root = _parse(sitemap.render_urlset(sitemap.collect_urls(db)))

    stamps = [element.text for element in root.findall("sm:url/sm:lastmod", NS)]
    assert stamps, "expected at least one lastmod"
    for stamp in stamps:
        # Raises if it is not a parseable ISO-8601 instant.
        parsed = dt.datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None, "a lastmod without an offset is ambiguous"


def test_a_naive_timestamp_is_treated_as_utc_not_dropped(db: Session):
    """SQLite hands back naive datetimes; a dropped lastmod is a lost signal."""
    naive = dt.datetime(2026, 1, 2, 3, 4, 5)
    url = sitemap.SitemapUrl(path="/x", lastmod=naive)

    rendered = sitemap.render_urlset([url])
    assert "2026-01-02T03:04:05+00:00" in rendered


def test_a_url_without_a_lastmod_omits_the_element(db: Session):
    rendered = sitemap.render_urlset([sitemap.SitemapUrl(path="/x")])
    assert "<lastmod>" not in rendered


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_a_small_catalogue_needs_one_file():
    assert sitemap.page_count(0) == 1
    assert sitemap.page_count(10) == 1
    assert sitemap.page_count(sitemap.MAX_URLS_PER_FILE) == 1


def test_the_protocol_limit_starts_a_second_file():
    assert sitemap.page_count(sitemap.MAX_URLS_PER_FILE + 1) == 2
    assert sitemap.page_count(sitemap.MAX_URLS_PER_FILE * 3) == 3


def test_every_url_lands_in_exactly_one_chunk():
    urls = [sitemap.SitemapUrl(path=f"/p/{i}") for i in range(120_000)]
    pages = sitemap.page_count(len(urls))

    seen = [u.path for page in range(1, pages + 1) for u in sitemap.page_slice(urls, page)]
    assert len(seen) == len(urls)
    assert len(set(seen)) == len(urls)


def test_the_index_lists_every_chunk():
    root = _parse(sitemap.render_index(3))
    locations = [element.text for element in root.findall("sm:sitemap/sm:loc", NS)]

    assert len(locations) == 3
    assert locations[0].endswith("/sitemap-1.xml")
    assert locations[-1].endswith("/sitemap-3.xml")


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def test_robots_points_at_the_sitemap():
    body = sitemap.render_robots()
    assert "Sitemap: " in body
    assert "/sitemap.xml" in body


def test_robots_disallows_every_private_area():
    body = sitemap.render_robots()
    for path in ("/admin", "/account", "/auth", "/cart", "/checkout"):
        assert f"Disallow: {path}" in body


def test_robots_disallows_search_so_crawlers_do_not_walk_the_filters():
    """Filter permutations are an infinite space that indexes nothing useful."""
    assert "Disallow: /search" in sitemap.render_robots()


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db: Session, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    import app.db.session as db_session
    import app.services.activity as activity
    import app.web.middleware as middleware
    from app.main import app

    maker = sessionmaker(
        bind=db.get_bind(), autoflush=False, expire_on_commit=False, class_=Session
    )
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    monkeypatch.setattr(middleware, "SessionLocal", maker)
    monkeypatch.setattr(activity, "record_page_view", lambda *a, **k: None)

    return TestClient(app)


def test_the_sitemap_is_served_as_xml(client, store: dict):
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    _parse(response.text)


def test_robots_is_served_as_plain_text(client):
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Sitemap:" in response.text


def test_both_are_cacheable(client, store: dict):
    """Crawlers re-fetch these; without a cache header every hit is a full
    catalogue scan."""
    for path in ("/sitemap.xml", "/robots.txt"):
        assert "max-age" in client.get(path).headers.get("cache-control", ""), path


def test_a_single_page_catalogue_serves_the_urlset_directly(client, store: dict):
    """No index indirection while everything fits in one file."""
    body = client.get("/sitemap.xml").text
    assert "<urlset" in body and "<sitemapindex" not in body


def test_the_chunk_endpoint_serves_page_one(client, store: dict):
    response = client.get("/sitemap-1.xml")

    assert response.status_code == 200
    assert _locations(response.text) == _locations(client.get("/sitemap.xml").text)


def test_a_chunk_beyond_the_catalogue_is_not_found(client, store: dict):
    assert client.get("/sitemap-2.xml").status_code == 404
    assert client.get("/sitemap-0.xml").status_code == 404


def test_the_seo_routes_are_not_swallowed_by_the_legacy_catch_all(client):
    """``/{path:path}`` is registered last precisely so this holds — but the
    ordering is easy to break and impossible to notice by hand."""
    for path in ("/sitemap.xml", "/robots.txt"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "text/html" not in response.headers["content-type"], path


# ---------------------------------------------------------------------------
# Clean URLs (Part I §16)
# ---------------------------------------------------------------------------


def test_a_stale_slug_redirects_to_the_canonical_url(client, store: dict, catalogue):
    """§16's decision: the id resolves the page and the old slug 301s, so a
    link shared over WhatsApp years ago still lands somewhere."""
    cases = {
        f"/p/{store['product'].pk_product_id}/a-name-from-last-year": "/p/",
        f"/c/{db_first_category_id(client)}/an-old-category-name": "/c/",
        f"/publisher/{catalogue['publisher'].pk_publisher_id}/renamed": "/publisher/",
        f"/tag/{catalogue['tag'].pk_tag_id}/renamed": "/tag/",
    }

    for stale, prefix in cases.items():
        response = client.get(stale, follow_redirects=False)
        assert response.status_code == 301, stale
        assert response.headers["location"].startswith(prefix), stale
        assert response.headers["location"] != stale


def db_first_category_id(client) -> int:
    """The seeded category, read back through the app's own session."""
    import app.db.session as db_session

    with db_session.SessionLocal() as session:
        return session.query(Category).first().pk_category_id


def test_the_canonical_target_serves_the_page(client, store: dict):
    product_id = store["product"].pk_product_id
    canonical = client.get(f"/p/{product_id}/stale", follow_redirects=False).headers[
        "location"
    ]

    response = client.get(canonical, follow_redirects=False)
    assert response.status_code == 200, "the canonical must not redirect again"


def test_every_indexed_page_declares_a_canonical_link(client, store: dict, catalogue):
    """Two URLs for one page is what a canonical tag exists to resolve."""
    paths = [
        "/",
        f"/p/{store['product'].pk_product_id}",
        f"/c/{db_first_category_id(client)}",
        f"/publisher/{catalogue['publisher'].pk_publisher_id}",
        f"/tag/{catalogue['tag'].pk_tag_id}",
    ]

    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'rel="canonical"' in response.text, path
