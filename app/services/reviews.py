"""Customer reviews and ratings (Part I §14).

§14 asks for "product reviews & ratings, with Admin moderation before
publishing". Moderation-before-publishing is the whole point: a review is not
content the shop chose to host until a moderator says so, and a bookshop
carrying devotional material has more than the usual reason to care what appears
under its own name.

So everything a customer submits lands as ``PENDING`` and is invisible —
including to the person who wrote it, who is instead told it is waiting. The
alternative, showing authors their own unpublished reviews, teaches people the
review "worked" and produces an angry second submission when it never appears.

The verified-purchase flag is computed here rather than trusted from the form:
it is a claim about the shop's own order history, so only the shop can make it.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import Product, ProductReview, ProductVariant
from app.models.enums import OrderStatus, ReviewStatus
from app.models.identity import User
from app.models.orders import Order, OrderLine

log = get_logger(__name__)

#: The scale shown on the product page. Five stars, whole numbers only — half
#: stars invite an average nobody can reproduce by counting.
MIN_RATING = 1
MAX_RATING = 5

#: Long enough to say something, short enough not to become an essay nobody
#: reads. A rating on its own is allowed: many customers will only ever click a
#: star, and refusing that loses the rating too.
MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 4000


def submit_review(
    db: Session,
    *,
    product: Product,
    user: User,
    rating: int,
    title: str | None = None,
    body: str | None = None,
) -> ProductReview:
    """Record a customer's review. It is not published by submitting it.

    Raises :class:`Conflict` if this customer has already reviewed the product:
    one voice per customer per product, otherwise a single motivated person can
    move the average on their own.
    """
    rating = _validated_rating(rating)

    if not product.scd_active_flag or not product.is_visible_flag:
        raise NotFound("That product does not exist.")

    if existing_review(db, product_id=product.pk_product_id, user_id=user.pk_user_id):
        raise Conflict("You have already reviewed this product.")

    now = utcnow()
    review = ProductReview(
        fk_product_id=product.pk_product_id,
        fk_user_id=user.pk_user_id,
        rating=rating,
        title=_trimmed(title, MAX_TITLE_LENGTH),
        body=_trimmed(body, MAX_BODY_LENGTH),
        submitted_dt=now,
        status=ReviewStatus.PENDING,
        verified_purchase_flag=has_purchased(
            db, product_id=product.pk_product_id, user_id=user.pk_user_id
        ),
        scd_active_from=now,
        scd_changed_by=user.pk_user_id,
    )
    db.add(review)
    db.flush()

    log.info(
        "review_submitted",
        extra={
            "product_id": product.pk_product_id,
            "user_id": user.pk_user_id,
            "rating": rating,
            "verified": review.verified_purchase_flag,
        },
    )
    return review


def _validated_rating(rating: int) -> int:
    try:
        value = int(rating)
    except (TypeError, ValueError):
        raise ValidationFailed("Choose a rating from 1 to 5.") from None
    if not MIN_RATING <= value <= MAX_RATING:
        raise ValidationFailed("Choose a rating from 1 to 5.")
    return value


def _trimmed(value: str | None, limit: int) -> str | None:
    """Empty is stored as NULL, not as an empty string.

    Two representations of "the customer wrote nothing" means every reader has
    to handle both.
    """
    if value is None:
        return None
    text = value.strip()
    return text[:limit] if text else None


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def has_purchased(db: Session, *, product_id: int, user_id: int) -> bool:
    """Did this customer actually buy this product?

    Any non-cancelled order counts, not only delivered ones: someone who has
    paid and is waiting has bought the thing. Checked across variants, because
    the customer bought a *product* as far as they are concerned.
    """
    found = db.scalar(
        select(OrderLine.pk_order_line_id)
        .join(Order, Order.pk_order_id == OrderLine.fk_order_id)
        .join(
            ProductVariant,
            ProductVariant.pk_product_variant_id == OrderLine.fk_product_variant_id,
        )
        .where(
            ProductVariant.fk_product_id == product_id,
            Order.fk_user_id == user_id,
            Order.status != OrderStatus.CANCELLED,
            Order.scd_active_flag.is_(True),
            OrderLine.scd_active_flag.is_(True),
        )
        .limit(1)
    )
    return found is not None


def existing_review(
    db: Session, *, product_id: int, user_id: int
) -> ProductReview | None:
    """This customer's review of this product, whatever its state.

    Includes rejected ones deliberately: a rejected review that could simply be
    resubmitted would make moderation decorative.
    """
    return db.scalars(
        select(ProductReview).where(
            ProductReview.fk_product_id == product_id,
            ProductReview.fk_user_id == user_id,
            ProductReview.scd_active_flag.is_(True),
        )
    ).first()


def can_review(db: Session, *, product_id: int, user: User | None) -> bool:
    """Whether to show the write-a-review form at all."""
    if user is None:
        return False
    return existing_review(db, product_id=product_id, user_id=user.pk_user_id) is None


# ---------------------------------------------------------------------------
# Ratings shown on the storefront
# ---------------------------------------------------------------------------


class RatingSummary:
    """The star line under a product name.

    A plain object rather than a dict so a template typo raises instead of
    silently rendering nothing.
    """

    __slots__ = ("average", "count", "distribution")

    def __init__(self, average: float, count: int, distribution: dict[int, int]):
        self.average = average
        self.count = count
        self.distribution = distribution

    @property
    def has_reviews(self) -> bool:
        return self.count > 0

    @property
    def full_stars(self) -> int:
        return int(self.average)

    @property
    def has_half_star(self) -> bool:
        return (self.average - self.full_stars) >= 0.5

    def share(self, stars: int) -> float:
        """Percentage of reviews at this star count, for the bar chart."""
        if not self.count:
            return 0.0
        return round(self.distribution.get(stars, 0) * 100 / self.count, 1)


def rating_summary(db: Session, product_id: int) -> RatingSummary:
    """Averaged over *approved* reviews only.

    Counting pending ones would leak moderation state: a customer could watch
    the average move and infer what was submitted.
    """
    rows = db.execute(
        select(ProductReview.rating, func.count())
        .where(
            ProductReview.fk_product_id == product_id,
            ProductReview.status == ReviewStatus.APPROVED,
            ProductReview.scd_active_flag.is_(True),
        )
        .group_by(ProductReview.rating)
    ).all()

    distribution = {int(rating): int(count) for rating, count in rows}
    total = sum(distribution.values())
    if not total:
        return RatingSummary(0.0, 0, {})

    weighted = sum(rating * count for rating, count in distribution.items())
    return RatingSummary(round(weighted / total, 1), total, distribution)


def rating_summaries(
    db: Session, product_ids: list[int]
) -> dict[int, RatingSummary]:
    """Summaries for a whole listing page in one query.

    Product cards show stars too, and one query per card is how a category page
    becomes slow.
    """
    if not product_ids:
        return {}

    rows = db.execute(
        select(ProductReview.fk_product_id, ProductReview.rating, func.count())
        .where(
            ProductReview.fk_product_id.in_(product_ids),
            ProductReview.status == ReviewStatus.APPROVED,
            ProductReview.scd_active_flag.is_(True),
        )
        .group_by(ProductReview.fk_product_id, ProductReview.rating)
    ).all()

    grouped: dict[int, dict[int, int]] = {}
    for product_id, rating, count in rows:
        grouped.setdefault(int(product_id), {})[int(rating)] = int(count)

    summaries: dict[int, RatingSummary] = {}
    for product_id in product_ids:
        distribution = grouped.get(product_id, {})
        total = sum(distribution.values())
        if total:
            weighted = sum(r * c for r, c in distribution.items())
            summaries[product_id] = RatingSummary(
                round(weighted / total, 1), total, distribution
            )
        else:
            summaries[product_id] = RatingSummary(0.0, 0, {})
    return summaries


def approved_reviews(
    db: Session, product_id: int, *, limit: int = 20
) -> list[ProductReview]:
    """Only moderated-and-published reviews reach the page (§14)."""
    return list(
        db.scalars(
            select(ProductReview)
            .where(
                ProductReview.fk_product_id == product_id,
                ProductReview.status == ReviewStatus.APPROVED,
                ProductReview.scd_active_flag.is_(True),
            )
            .order_by(ProductReview.submitted_dt.desc())
            .limit(limit)
        ).all()
    )


def reviewer_names(db: Session, reviews: list[ProductReview]) -> dict[int, str]:
    """Display names for a batch of reviews.

    Usernames, never email addresses: an address published under a review is a
    spam list the customer did not agree to join.
    """
    user_ids = {review.fk_user_id for review in reviews}
    if not user_ids:
        return {}
    rows = db.execute(
        select(User.pk_user_id, User.username).where(User.pk_user_id.in_(user_ids))
    ).all()
    return {int(user_id): username for user_id, username in rows}


def pending_count(db: Session) -> int:
    """How many reviews are waiting on a moderator — for the admin dashboard."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(ProductReview)
            .where(
                ProductReview.status == ReviewStatus.PENDING,
                ProductReview.scd_active_flag.is_(True),
            )
        )
        or 0
    )


def recent_submission_count(
    db: Session, *, user_id: int, within: dt.timedelta = dt.timedelta(hours=1)
) -> int:
    """Reviews this customer has filed recently.

    One review per product already caps the obvious abuse; this catches the
    other shape, where somebody works through the catalogue.
    """
    since = utcnow() - within
    return int(
        db.scalar(
            select(func.count())
            .select_from(ProductReview)
            .where(
                ProductReview.fk_user_id == user_id,
                ProductReview.submitted_dt >= since,
                ProductReview.scd_active_flag.is_(True),
            )
        )
        or 0
    )
