"""Sitemap and robots.txt endpoints (Part I §16).

Generated on request rather than written to disk on a schedule. A bookshop's
catalogue is small enough that the query cost is trivial, and a file on disk is
one more thing that can go stale without anybody noticing — a sitemap listing
products that were withdrawn last month is worse than none at all.

Registered before the storefront's legacy catch-all, which would otherwise
swallow these paths and 404 them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.db.session import get_db
from app.services import sitemap

router = APIRouter(include_in_schema=False)

#: Crawlers re-fetch these; a short cache keeps a burst from hitting the
#: database once per request without letting a withdrawn product linger.
CACHE_CONTROL = "public, max-age=3600"


def _xml(body: str) -> Response:
    return Response(
        content=body,
        media_type="application/xml",
        headers={"Cache-Control": CACHE_CONTROL},
    )


@router.get("/sitemap.xml")
def sitemap_root(db: Session = Depends(get_db)) -> Response:
    """The sitemap itself, or an index when the catalogue outgrows one file.

    Serving a single ``<urlset>`` while it fits keeps the common case simple;
    the index appears only when it is actually needed, and the entry point does
    not change either way.
    """
    urls = sitemap.collect_urls(db)
    pages = sitemap.page_count(len(urls))

    if pages == 1:
        return _xml(sitemap.render_urlset(urls))

    newest = max((url.lastmod for url in urls if url.lastmod), default=None)
    return _xml(sitemap.render_index(pages, lastmod=newest))


@router.get("/sitemap-{page}.xml")
def sitemap_page(page: int, db: Session = Depends(get_db)) -> Response:
    """One chunk of a split sitemap."""
    urls = sitemap.collect_urls(db)
    if page < 1 or page > sitemap.page_count(len(urls)):
        raise NotFound("That sitemap page does not exist.")
    return _xml(sitemap.render_urlset(sitemap.page_slice(urls, page)))


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> Response:
    return PlainTextResponse(
        sitemap.render_robots(), headers={"Cache-Control": CACHE_CONTROL}
    )
