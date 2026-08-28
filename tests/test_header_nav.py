"""The site-wide header's category nav must actually navigate (Part I §3.1).

The bug this pins: the mega-menu and the mobile drawer both linked to
``{{ category.url }}``, but ``Category`` has no ``url`` attribute. Jinja renders
an undefined attribute as the empty string, so every category link shipped as
``href=""`` — which a browser treats as "reload this page". Clicking a category
did nothing, on every page of the site, on both desktop and mobile.

Nothing caught it because nothing failed: no exception, no 500, no missing
template. The page rendered perfectly and the links were simply inert. So the
assertions here are about the *rendered href*, not about the view returning 200
— a page can be entirely healthy and still be unusable.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models.catalog import Category
from app.db.base import utcnow
from tests.test_checkout import db, store  # noqa: F401 - fixtures

# Every page whose layout renders the shared header.
HEADER_PAGES = [
    "/",
    "/categories",
    "/cart",
    "/compare",
    "/branches",
    "/orders/track",
    "/auth/login",
    "/search?q=book",
]

MEGA_LINK = re.compile(r'<a href="([^"]*)" class="mega-nav__link">')
DRAWER_LINK = re.compile(
    r'<a href="([^"]*)"\s+class="flex items-center justify-between px-4 py-3'
)


@pytest.fixture
def client(db: Session, store: dict, monkeypatch) -> TestClient:
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
    monkeypatch.setattr(activity, "record_event", lambda *a, **k: None)

    return TestClient(app)


def test_the_fixture_really_puts_a_category_in_the_menu(client: TestClient) -> None:
    """Guard the guard: assertions over an empty menu would all vacuously pass."""
    assert MEGA_LINK.findall(client.get("/").text)


@pytest.mark.parametrize("path", HEADER_PAGES)
def test_mega_menu_category_links_are_not_empty(client: TestClient, path: str) -> None:
    hrefs = MEGA_LINK.findall(client.get(path).text)
    assert hrefs, f"no category links rendered on {path}"
    assert all(hrefs), (
        f"{path} rendered a category link with an empty href: {hrefs}. "
        f"An undefined attribute in the template silently becomes ''."
    )


@pytest.mark.parametrize("path", HEADER_PAGES)
def test_mobile_drawer_category_links_are_not_empty(
    client: TestClient, path: str
) -> None:
    hrefs = DRAWER_LINK.findall(client.get(path).text)
    assert hrefs, f"no drawer category links rendered on {path}"
    assert all(hrefs), f"{path} rendered an empty drawer href: {hrefs}"


@pytest.mark.parametrize("path", HEADER_PAGES)
def test_every_category_link_actually_resolves(client: TestClient, path: str) -> None:
    """The link is only real if following it reaches the category page."""
    for href in set(MEGA_LINK.findall(client.get(path).text)):
        response = client.get(href, follow_redirects=False)
        assert response.status_code in (200, 301), (
            f"{path} links to {href}, which returns {response.status_code}"
        )


@pytest.mark.parametrize(
    "language,expected_slug", [("ar", "كتب"), ("en", "books")]
)
def test_category_link_follows_the_request_language(
    client: TestClient, language: str, expected_slug: str
) -> None:
    """§16 gives AR and EN their own slugs; the header must pick the right one."""
    hrefs = MEGA_LINK.findall(client.get(f"/?lang={language}").text)
    assert hrefs == [f"/c/1/{expected_slug}"], hrefs


def test_the_id_leads_so_a_blank_slug_still_links(
    client: TestClient, db: Session
) -> None:
    """A blank slug must not collapse the href back to "".

    The column is NOT NULL, so the reachable degenerate case is the empty
    string — which is what an all-punctuation name slugifies down to. §16 puts
    the id first precisely so the slug is decoration: a blank slug should give
    a thinner URL (`/c/2`), never a broken one.
    """
    db.add(
        Category(
            name_ar="؟؟؟",
            name_en="???",
            slug_ar="",
            slug_en="",
            ancestor_path="/",
            scd_active_from=utcnow(),
        )
    )
    db.commit()

    hrefs = MEGA_LINK.findall(client.get("/?lang=en").text)
    assert len(hrefs) == 2
    assert all(href.startswith("/c/") for href in hrefs), hrefs
    assert all(client.get(h).status_code == 200 for h in hrefs)


def test_no_rendered_page_ships_an_empty_anchor(client: TestClient) -> None:
    """The general form of the bug, across the whole shared layout.

    An `<a href="">` is always a defect: it looks like a link, and it silently
    reloads the page instead of going anywhere.
    """
    offenders = []
    for path in HEADER_PAGES:
        html = client.get(path).text
        if re.search(r'<a\b[^>]*\shref=""', html):
            offenders.append(path)
    assert not offenders, f"pages rendering <a href=\"\">: {offenders}"
