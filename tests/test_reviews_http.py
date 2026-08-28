"""The review flow over real HTTP (Part I §14).

:mod:`tests.test_reviews` pins the rules; this pins that a shopper can reach
them from the product page, that the page renders the stars and the form, and —
the one that matters most — that a submitted review does not appear on the page
that submitted it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import utcnow
from app.models.catalog import ProductReview
from app.models.enums import ReviewStatus
from app.models.identity import User
from app.services import catalog_admin, reviews, sessions
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


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
    monkeypatch.setattr(activity, "record_event", lambda *a, **k: None)

    return TestClient(app)


@pytest.fixture
def signed_in(client: TestClient, db: Session, shop: dict) -> TestClient:
    session = sessions.create_session(db, shop["user"], _FakeRequest())
    db.commit()
    client.cookies.set(settings.session_cookie_name, session.session_key)
    return client


@pytest.fixture
def product_path(shop: dict) -> str:
    # The id leads and the slug is decoration (§16), so the bare id resolves.
    # English is pinned because these assertions compare against real strings,
    # and the site's default language is Arabic.
    return f"/p/{shop['product'].pk_product_id}?lang=en"


def _with(path: str, **params) -> str:
    extra = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{path}&{extra}" if extra else path


def _submit(client: TestClient, shop: dict, **data):
    payload = {"rating": "5"}
    payload.update(data)
    return client.post(
        f"/products/{shop['product'].pk_product_id}/review",
        data=payload,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_product_page_renders_the_reviews_tab(client: TestClient, product_path: str):
    response = client.get(product_path)
    assert response.status_code == 200
    assert 'id="reviews"' in response.text


def test_a_guest_is_invited_to_sign_in_rather_than_shown_a_form(
    client: TestClient, product_path: str
):
    response = client.get(product_path)
    assert "/auth/login?next=" in response.text
    assert 'name="rating"' not in response.text


def test_a_signed_in_customer_gets_the_form(signed_in: TestClient, product_path: str):
    response = signed_in.get(product_path)
    assert 'name="rating"' in response.text
    assert 'value="5"' in response.text


def test_the_page_says_reviews_are_moderated(signed_in: TestClient, product_path: str):
    """A customer who is not told will read the delay as a bug."""
    response = signed_in.get(product_path)
    assert "product.review_moderation_note" not in response.text  # key resolved
    from app.core.i18n import catalog

    assert catalog("en")["product.review_moderation_note"] in response.text


def test_both_languages_render(signed_in: TestClient, shop: dict):
    """One template serves RTL and LTR (§17.3)."""
    for language in ("ar", "en"):
        response = signed_in.get(
            f"/p/{shop['product'].pk_product_id}?lang={language}"
        )
        assert response.status_code == 200
        assert "product.review" not in response.text, f"key leaked in {language}"
        assert f'dir="{"rtl" if language == "ar" else "ltr"}"' in response.text


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


def test_submitting_stores_a_pending_review(
    signed_in: TestClient, db: Session, shop: dict
):
    response = _submit(signed_in, shop, rating="4", title="Good", body="Enjoyed it.")

    assert response.status_code == 303
    assert response.headers["location"].endswith("?review=submitted#reviews")

    db.expire_all()
    review = db.query(ProductReview).one()
    assert review.status == ReviewStatus.PENDING
    assert review.rating == 4
    assert review.fk_user_id == shop["user"].pk_user_id


def test_a_submitted_review_does_not_appear_on_the_page(
    signed_in: TestClient, db: Session, shop: dict, product_path: str
):
    """The single most important behaviour in §14."""
    _submit(signed_in, shop, rating="5", body="Please publish me immediately")

    response = signed_in.get(product_path)
    assert "Please publish me immediately" not in response.text


def test_the_author_is_told_it_is_waiting(
    signed_in: TestClient, db: Session, shop: dict, product_path: str
):
    """Otherwise they submit again, and the moderation queue fills with
    duplicates."""
    from app.core.i18n import catalog

    _submit(signed_in, shop)
    response = signed_in.get(_with(product_path, review="submitted"))

    assert catalog("en")["product.review_submitted"] in response.text
    # And the form is gone.
    assert 'name="rating"' not in response.text


def test_publishing_makes_it_appear(
    signed_in: TestClient, db: Session, shop: dict, product_path: str
):
    _submit(signed_in, shop, rating="5", body="A genuinely lovely edition")
    db.expire_all()
    review = db.query(ProductReview).one()

    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.APPROVED
    )
    db.commit()

    response = signed_in.get(product_path)
    assert "A genuinely lovely edition" in response.text
    assert shop["user"].username in response.text


def test_a_guest_is_redirected_to_login_not_refused(
    client: TestClient, db: Session, shop: dict
):
    response = _submit(client, shop)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")
    assert db.query(ProductReview).count() == 0


def test_a_bad_rating_comes_back_as_a_message(
    signed_in: TestClient, db: Session, shop: dict
):
    response = _submit(signed_in, shop, rating="9")

    assert response.status_code == 303
    assert response.headers["location"].endswith("?review=bad_rating#reviews")
    assert db.query(ProductReview).count() == 0


def test_a_second_review_comes_back_as_a_message(
    signed_in: TestClient, db: Session, shop: dict
):
    _submit(signed_in, shop)
    response = _submit(signed_in, shop, rating="1")

    assert response.headers["location"].endswith("?review=already_reviewed#reviews")
    db.expire_all()
    assert db.query(ProductReview).count() == 1
    assert db.query(ProductReview).one().rating == 5, "the first one stands"


def test_reviewing_a_hidden_product_is_not_found(
    signed_in: TestClient, db: Session, shop: dict
):
    shop["product"].is_visible_flag = False
    db.commit()

    response = _submit(signed_in, shop)
    assert response.status_code == 404
    assert db.query(ProductReview).count() == 0


def test_a_flood_of_reviews_is_throttled(
    signed_in: TestClient, db: Session, shop: dict, store: dict
):
    """One-per-product caps the obvious abuse; this catches somebody working
    through the catalogue."""
    from app.web.commerce import REVIEW_RATE_LIMIT

    now = utcnow()
    for index in range(REVIEW_RATE_LIMIT):
        db.add(
            ProductReview(
                fk_product_id=shop["product"].pk_product_id,
                fk_user_id=shop["user"].pk_user_id,
                rating=5,
                submitted_dt=now,
                status=ReviewStatus.REJECTED,
                scd_active_from=now,
            )
        )
    db.commit()

    response = _submit(signed_in, shop)
    assert response.headers["location"].endswith("?review=too_many#reviews")


# ---------------------------------------------------------------------------
# What gets shown about the reviewer
# ---------------------------------------------------------------------------


@pytest.fixture
def published_review(db: Session, shop: dict) -> ProductReview:
    review = reviews.submit_review(
        db, product=shop["product"], user=shop["user"], rating=5, body="Recommended"
    )
    db.commit()
    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.APPROVED
    )
    db.commit()
    return review


def test_the_reviewers_email_is_never_on_the_page(
    client: TestClient, shop: dict, product_path: str, published_review
):
    response = client.get(product_path)
    assert shop["user"].username in response.text
    assert shop["user"].email not in response.text


def test_the_average_shows_once_a_review_is_published(
    client: TestClient, product_path: str, published_review
):
    response = client.get(product_path)
    # The star row is drawn as a clipped overlay; 5/5 fills it completely.
    assert "width: 100.0%" in response.text


def test_a_review_body_is_escaped(
    client: TestClient, db: Session, shop: dict, product_path: str
):
    """Review text is attacker-supplied and shown to every other shopper."""
    review = reviews.submit_review(
        db, product=shop["product"], user=shop["user"], rating=5,
        body="<script>alert(document.cookie)</script>",
    )
    db.commit()
    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.APPROVED
    )
    db.commit()

    response = client.get(product_path)
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text


def test_a_junk_flash_code_is_not_reflected(client: TestClient, product_path: str):
    response = client.get(_with(product_path, review="<script>x</script>"))
    assert response.status_code == 200
    assert "script>x" not in response.text


# ---------------------------------------------------------------------------
# Verified purchase
# ---------------------------------------------------------------------------


def test_a_buyers_review_is_badged_as_a_verified_purchase(
    signed_in: TestClient, db: Session, shop: dict, product_path: str
):
    from app.services.checkout import CheckoutRequest, place_order
    from app.services.commerce import ShopperRef

    _cart(db, shop, user=shop["user"], quantity=1)
    place_order(
        db, _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    _submit(signed_in, shop, rating="5", body="Bought and read")
    db.expire_all()
    review = db.query(ProductReview).one()
    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.APPROVED
    )
    db.commit()

    from app.core.i18n import catalog

    response = signed_in.get(product_path)
    assert catalog("en")["product.verified_purchase"] in response.text


def test_a_non_buyers_review_is_not_badged(
    client: TestClient, db: Session, shop: dict, product_path: str
):
    from app.core.i18n import catalog

    other = User(
        fk_role_id=shop["user"].fk_role_id,
        username="passerby",
        email="passerby@example.com",
        password_hash="x",
        scd_active_from=utcnow(),
    )
    db.add(other)
    db.commit()

    review = reviews.submit_review(
        db, product=shop["product"], user=other, rating=4, body="Looks nice"
    )
    db.commit()
    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.APPROVED
    )
    db.commit()

    response = client.get(product_path)
    assert "Looks nice" in response.text
    assert catalog("en")["product.verified_purchase"] not in response.text
