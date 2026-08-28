"""Customer reviews and ratings (Part I §14).

§14's requirement is "reviews & ratings, with Admin moderation before
publishing". Every test here is ultimately about the second half of that
sentence: submitting must never publish, and nothing a customer can do — retry,
resubmit, watch the average — may route around a moderator.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.base import utcnow
from app.models.catalog import ProductReview
from app.models.enums import ReviewStatus
from app.models.identity import User
from app.services import catalog_admin, reviews
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


@pytest.fixture
def other_user(db: Session, shop: dict) -> User:
    user = User(
        fk_role_id=shop["user"].fk_role_id,
        username="reader",
        email="reader@example.com",
        password_hash="x",
        email_verified_flag=True,
        scd_active_from=utcnow(),
    )
    db.add(user)
    db.commit()
    return user


def _review(db: Session, shop: dict, user: User, rating: int, **kwargs) -> ProductReview:
    review = reviews.submit_review(
        db, product=shop["product"], user=user, rating=rating, **kwargs
    )
    db.commit()
    return review


def _publish(db: Session, review: ProductReview) -> ProductReview:
    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.APPROVED
    )
    db.commit()
    return review


# ---------------------------------------------------------------------------
# Moderation before publishing (Part I §14)
# ---------------------------------------------------------------------------


def test_a_submitted_review_is_not_published(db: Session, shop: dict):
    review = _review(db, shop, shop["user"], 5, title="Wonderful")

    assert review.status == ReviewStatus.PENDING
    assert reviews.approved_reviews(db, shop["product"].pk_product_id) == []


def test_a_pending_review_does_not_move_the_average(db: Session, shop: dict):
    """Otherwise the average leaks what is sitting in the moderation queue."""
    _review(db, shop, shop["user"], 1)

    summary = reviews.rating_summary(db, shop["product"].pk_product_id)
    assert summary.count == 0
    assert summary.average == 0.0


def test_publishing_makes_it_visible_and_counted(db: Session, shop: dict):
    review = _publish(db, _review(db, shop, shop["user"], 4))

    assert reviews.approved_reviews(db, shop["product"].pk_product_id) == [review]
    assert reviews.rating_summary(db, shop["product"].pk_product_id).average == 4.0


def test_a_rejected_review_stays_invisible(db: Session, shop: dict):
    review = _review(db, shop, shop["user"], 1, body="nonsense")
    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.REJECTED
    )
    db.commit()

    assert reviews.approved_reviews(db, shop["product"].pk_product_id) == []
    assert reviews.rating_summary(db, shop["product"].pk_product_id).count == 0


def test_a_rejected_review_cannot_simply_be_resubmitted(db: Session, shop: dict):
    """If it could, moderation would be decorative."""
    review = _review(db, shop, shop["user"], 1)
    catalog_admin.moderate_review(
        db, review_id=review.pk_product_review_id, status=ReviewStatus.REJECTED
    )
    db.commit()

    with pytest.raises(Conflict):
        reviews.submit_review(
            db, product=shop["product"], user=shop["user"], rating=5
        )


# ---------------------------------------------------------------------------
# One voice per customer
# ---------------------------------------------------------------------------


def test_a_customer_may_review_a_product_once(db: Session, shop: dict):
    _review(db, shop, shop["user"], 5)

    with pytest.raises(Conflict):
        reviews.submit_review(db, product=shop["product"], user=shop["user"], rating=1)


def test_different_customers_both_count(db: Session, shop: dict, other_user: User):
    _publish(db, _review(db, shop, shop["user"], 5))
    _publish(db, _review(db, shop, other_user, 3))

    summary = reviews.rating_summary(db, shop["product"].pk_product_id)
    assert summary.count == 2
    assert summary.average == 4.0


def test_the_form_is_hidden_once_a_customer_has_reviewed(db: Session, shop: dict):
    product_id = shop["product"].pk_product_id
    assert reviews.can_review(db, product_id=product_id, user=shop["user"]) is True

    _review(db, shop, shop["user"], 5)
    assert reviews.can_review(db, product_id=product_id, user=shop["user"]) is False


def test_a_guest_is_never_offered_the_form(db: Session, shop: dict):
    assert reviews.can_review(
        db, product_id=shop["product"].pk_product_id, user=None
    ) is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rating", [0, 6, -1, 99])
def test_ratings_outside_one_to_five_are_refused(db: Session, shop: dict, rating: int):
    with pytest.raises(ValidationFailed):
        reviews.submit_review(
            db, product=shop["product"], user=shop["user"], rating=rating
        )


def test_a_non_numeric_rating_is_refused(db: Session, shop: dict):
    with pytest.raises(ValidationFailed):
        reviews.submit_review(
            db, product=shop["product"], user=shop["user"], rating="five"
        )


def test_a_rating_alone_is_enough(db: Session, shop: dict):
    """Most customers will only ever click a star; refusing that loses the
    rating as well as the comment."""
    review = _review(db, shop, shop["user"], 4)

    assert review.rating == 4
    assert review.title is None and review.body is None


def test_whitespace_only_text_is_stored_as_nothing(db: Session, shop: dict):
    """Two representations of "they wrote nothing" means every reader handles
    both."""
    review = _review(db, shop, shop["user"], 3, title="   ", body="\n\t ")
    assert review.title is None and review.body is None


def test_over_long_text_is_trimmed_not_rejected(db: Session, shop: dict):
    review = _review(db, shop, shop["user"], 3, body="x" * 9000)
    assert len(review.body) == reviews.MAX_BODY_LENGTH


def test_an_invisible_product_cannot_be_reviewed(db: Session, shop: dict):
    shop["product"].is_visible_flag = False
    db.commit()

    with pytest.raises(NotFound):
        reviews.submit_review(
            db, product=shop["product"], user=shop["user"], rating=5
        )


# ---------------------------------------------------------------------------
# Verified purchase
# ---------------------------------------------------------------------------


def test_a_buyer_is_marked_as_a_verified_purchase(db: Session, shop: dict):
    _cart(db, shop, user=shop["user"], quantity=1)
    place_order(
        db, _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    review = _review(db, shop, shop["user"], 5)
    assert review.verified_purchase_flag is True


def test_a_non_buyer_is_not(db: Session, shop: dict, other_user: User):
    review = _review(db, shop, other_user, 5)
    assert review.verified_purchase_flag is False


def test_the_flag_is_computed_not_taken_from_the_caller(db: Session, shop: dict):
    """It is a claim about the shop's own order history, so only the shop may
    make it. ``submit_review`` accepts no such argument at all."""
    import inspect

    parameters = inspect.signature(reviews.submit_review).parameters
    assert "verified_purchase_flag" not in parameters
    assert "verified" not in parameters


# ---------------------------------------------------------------------------
# The summary shown on the page
# ---------------------------------------------------------------------------


def test_the_average_is_rounded_to_one_decimal(db: Session, shop: dict, other_user: User):
    _publish(db, _review(db, shop, shop["user"], 5))
    _publish(db, _review(db, shop, other_user, 4))
    # 4.5 exactly.
    assert reviews.rating_summary(db, shop["product"].pk_product_id).average == 4.5


def test_half_stars_are_reported_for_the_star_row(db: Session, shop: dict, other_user: User):
    _publish(db, _review(db, shop, shop["user"], 5))
    _publish(db, _review(db, shop, other_user, 4))

    summary = reviews.rating_summary(db, shop["product"].pk_product_id)
    assert summary.full_stars == 4
    assert summary.has_half_star is True


def test_the_distribution_adds_up(db: Session, shop: dict, other_user: User):
    _publish(db, _review(db, shop, shop["user"], 5))
    _publish(db, _review(db, shop, other_user, 5))

    summary = reviews.rating_summary(db, shop["product"].pk_product_id)
    assert summary.distribution == {5: 2}
    assert summary.share(5) == 100.0
    assert summary.share(1) == 0.0


def test_a_product_with_no_reviews_reports_zero_not_an_error(db: Session, shop: dict):
    summary = reviews.rating_summary(db, shop["product"].pk_product_id)

    assert summary.has_reviews is False
    assert summary.share(3) == 0.0
    assert summary.full_stars == 0


def test_batch_summaries_cover_every_product_asked_for(db: Session, shop: dict):
    """A listing page renders a card per product; a missing key would be a
    template crash on the quietest product in the catalogue."""
    _publish(db, _review(db, shop, shop["user"], 5))

    summaries = reviews.rating_summaries(
        db, [shop["product"].pk_product_id, 9999]
    )
    assert summaries[shop["product"].pk_product_id].average == 5.0
    assert summaries[9999].has_reviews is False


def test_batch_summaries_match_the_single_lookup(db: Session, shop: dict, other_user: User):
    _publish(db, _review(db, shop, shop["user"], 5))
    _publish(db, _review(db, shop, other_user, 2))

    product_id = shop["product"].pk_product_id
    one = reviews.rating_summary(db, product_id)
    many = reviews.rating_summaries(db, [product_id])[product_id]

    assert (one.average, one.count) == (many.average, many.count)


def test_no_summaries_wanted_means_no_query(db: Session):
    assert reviews.rating_summaries(db, []) == {}


# ---------------------------------------------------------------------------
# Privacy and rate limiting
# ---------------------------------------------------------------------------


def test_reviewers_are_named_by_username_never_by_email(db: Session, shop: dict):
    """An address published under a review is a spam list the customer never
    agreed to join."""
    review = _publish(db, _review(db, shop, shop["user"], 5))

    names = reviews.reviewer_names(db, [review])
    assert names[shop["user"].pk_user_id] == shop["user"].username
    assert "@" not in names[shop["user"].pk_user_id]


def test_naming_nobody_needs_no_query(db: Session):
    assert reviews.reviewer_names(db, []) == {}


def test_recent_submissions_are_counted_for_rate_limiting(db: Session, shop: dict):
    _review(db, shop, shop["user"], 5)
    assert reviews.recent_submission_count(db, user_id=shop["user"].pk_user_id) == 1


def test_old_submissions_fall_outside_the_window(db: Session, shop: dict):
    review = _review(db, shop, shop["user"], 5)
    review.submitted_dt = utcnow() - dt.timedelta(days=2)
    db.commit()

    assert reviews.recent_submission_count(db, user_id=shop["user"].pk_user_id) == 0


def test_the_pending_queue_is_countable_for_the_admin(db: Session, shop: dict, other_user: User):
    assert reviews.pending_count(db) == 0

    _review(db, shop, shop["user"], 5)
    assert reviews.pending_count(db) == 1

    _publish(db, _review(db, shop, other_user, 4))
    assert reviews.pending_count(db) == 1, "published reviews leave the queue"


# ---------------------------------------------------------------------------
# The submitted date (Part I §14)
# ---------------------------------------------------------------------------


def test_moderating_does_not_move_the_displayed_date(db: Session, shop: dict):
    """The date shown is when the customer wrote it, not when a moderator got
    round to it."""
    review = _review(db, shop, shop["user"], 5)
    written = review.submitted_dt

    _publish(db, review)
    assert review.submitted_dt == written
