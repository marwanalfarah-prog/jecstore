"""The wishlist, as a shopper actually experiences it (Part I §14).

Two failures this module exists for, both of which the storefront shipped with:

* The heart is a *toggle*, and it rendered identically whether the product was
  already saved or not — same outline icon, same "Add to wishlist" label. A
  shopper could not tell what state they were in, and a second click silently
  removed the item they thought they were adding.
* The header badge counted wishlist rows, while `/account/wishlist` listed
  wishlisted products joined to Product and filtered on active + visible. Hide a
  product and the badge read 1 over a permanently empty page.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import utcnow
from app.models.identity import User
from app.models.orders import Wishlist
from app.services import sessions
from tests.test_checkout import _FakeRequest, db, store  # noqa: F401 - fixtures


@pytest.fixture
def client(db: Session, monkeypatch) -> TestClient:
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


@pytest.fixture
def shopper(client: TestClient, db: Session, store: dict) -> User:
    user = store["user"]
    session = sessions.create_session(db, user, _FakeRequest())
    db.commit()
    client.cookies.set(settings.session_cookie_name, session.session_key)
    return user


def _button(body: str, product_id: int) -> str:
    match = re.search(
        rf'<button[^>]*id="wishlist-toggle-{product_id}".*?</button>', body, re.S
    )
    assert match, f"no wishlist button for product {product_id}"
    return re.sub(r"\s+", " ", match.group(0))


def _badge(body: str) -> int:
    match = re.search(r'id="wishlist-indicator".*?</a>', body, re.S)
    assert match, "no wishlist indicator in the header"
    count = re.search(r">\s*(\d+)\s*</span>", match.group(0))
    return int(count.group(1)) if count else 0


# ---------------------------------------------------------------------------
# The button reports its own state
# ---------------------------------------------------------------------------


def test_the_button_shows_whether_the_product_is_already_saved(
    client: TestClient, db: Session, store: dict, shopper: User
):
    product_id = store["product"].pk_product_id

    before = _button(client.get(f"/p/{product_id}", params={"lang": "en"}).text, product_id)
    assert 'aria-pressed="false"' in before
    assert "Add to wishlist" in before

    client.post("/account/wishlist/toggle", data={"product_id": product_id})

    after = _button(client.get(f"/p/{product_id}", params={"lang": "en"}).text, product_id)
    assert 'aria-pressed="true"' in after, "a saved product still reads as not saved"
    assert "In your wishlist" in after
    # Three signals, because one is never enough: colour for scanning, a label
    # for reading, aria-pressed for a screen reader (§17.7).
    assert 'fill="currentColor"' in after, "the heart is not filled in"


def test_pressing_the_button_swaps_the_button_as_well_as_the_badge(
    client: TestClient, store: dict, shopper: User
):
    """Returning only the header badge left the control the shopper actually
    pressed still showing its previous state."""
    product_id = store["product"].pk_product_id
    response = client.post(
        "/account/wishlist/toggle",
        data={"product_id": product_id},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'id="wishlist-indicator"' in response.text
    assert f'id="wishlist-toggle-{product_id}"' in response.text
    assert response.text.count("hx-swap-oob") == 2


def test_the_swap_comes_back_in_the_shape_that_was_pressed(
    client: TestClient, store: dict, shopper: User
):
    """A card's heart is icon-only; the product page's is a labelled button.
    Swapping the wrong one in drops a full-width button into a product grid."""
    product_id = store["product"].pk_product_id

    compact = client.post(
        "/account/wishlist/toggle",
        data={"product_id": product_id, "compact": "true"},
        headers={"HX-Request": "true"},
    ).text
    assert "product-card__quick-action" in compact
    assert "In your wishlist" not in compact, "the card variant should carry no label"

    full = client.post(
        "/account/wishlist/toggle",
        data={"product_id": product_id, "compact": "false"},
        headers={"HX-Request": "true"},
    ).text
    assert "btn-outline" in full


def test_toggling_twice_returns_to_the_starting_state(
    client: TestClient, store: dict, shopper: User
):
    product_id = store["product"].pk_product_id
    page = f"/p/{product_id}"

    client.post("/account/wishlist/toggle", data={"product_id": product_id})
    assert 'aria-pressed="true"' in _button(client.get(page).text, product_id)

    client.post("/account/wishlist/toggle", data={"product_id": product_id})
    assert 'aria-pressed="false"' in _button(client.get(page).text, product_id)


# ---------------------------------------------------------------------------
# The badge agrees with the page
# ---------------------------------------------------------------------------


def test_the_badge_matches_what_the_wishlist_page_lists(
    client: TestClient, db: Session, store: dict, shopper: User
):
    product = store["product"]
    client.post("/account/wishlist/toggle", data={"product_id": product.pk_product_id})

    page = client.get("/account/wishlist", params={"lang": "en"}).text
    assert _badge(page) == 1
    assert page.count("product-card__title") == 1


def test_hiding_a_saved_product_drops_it_from_the_badge_too(
    client: TestClient, db: Session, store: dict, shopper: User
):
    """The badge counted wishlist rows and the page joined Product, so hiding a
    product left the badge reading 1 over an empty page — permanently, because
    nothing the shopper could do would reconcile the two."""
    product = store["product"]
    client.post("/account/wishlist/toggle", data={"product_id": product.pk_product_id})

    product.is_visible_flag = False
    db.commit()

    page = client.get("/account/wishlist", params={"lang": "en"}).text
    assert page.count("product-card__title") == 0
    assert _badge(page) == 0, "the badge still counts a product the page cannot show"


def test_a_retired_product_leaves_the_badge_too(
    client: TestClient, db: Session, store: dict, shopper: User
):
    product = store["product"]
    client.post("/account/wishlist/toggle", data={"product_id": product.pk_product_id})

    product.scd_active_flag = False
    db.commit()

    assert _badge(client.get("/account/wishlist", params={"lang": "en"}).text) == 0


# ---------------------------------------------------------------------------
# Signed out
# ---------------------------------------------------------------------------


def test_a_guest_is_sent_to_sign_in_rather_than_ignored(
    client: TestClient, store: dict
):
    """§14 allows guest browsing, but the wishlist belongs to an account. An
    HTMX press has to redirect the browser, not answer with silence."""
    response = client.post(
        "/account/wishlist/toggle",
        data={"product_id": store["product"].pk_product_id},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 204
    assert response.headers["hx-redirect"].startswith("/auth/login")


def test_a_guest_sees_no_saved_state_and_no_badge(client: TestClient, store: dict):
    body = client.get(f"/p/{store['product'].pk_product_id}", params={"lang": "en"}).text
    assert 'aria-pressed="false"' in _button(body, store["product"].pk_product_id)
