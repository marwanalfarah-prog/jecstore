"""Homepage assembly (Part I §4).

Admin arranges the homepage without a developer, so the page is data: a list of
``SCD_HOMEPAGE_SECTION`` rows, ordered by ``sort_order`` and filtered by their
schedule. The auto-populating carousel types (New Arrivals, Best Sellers,
Discounted, Most Viewed) resolve their own contents here — that is what makes
them drop-in rather than hand-curated, and what lets any of them be scoped to a
single category the way the old site's per-category tabs were.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.catalog import Category, Discount, Product, ProductCategory, Publisher
from app.models.enums import HomepageSectionKind
from app.models.marketing import HomepageSection, HomepageSectionItem
from app.services.catalog import AvailabilityView, availability_for_products, visible_products_stmt
from app.services.pricing import PriceView, price_products
from app.services.reviews import RatingSummary, rating_summaries


@dataclass(slots=True)
class ResolvedSection:
    """A section plus whatever it needs to render — resolved once, server side."""

    section: HomepageSection
    products: list[Product]
    prices: dict[int, PriceView]
    availability: dict[int, AvailabilityView]
    ratings: dict[int, RatingSummary]
    publishers: list[Publisher]

    @property
    def kind(self) -> str:
        return self.section.section_kind

    @property
    def has_content(self) -> bool:
        """A scheduled carousel with nothing to show is skipped rather than
        rendered as an empty band — §17.4 on dead space."""
        if self.kind in _PRODUCT_KINDS:
            return bool(self.products)
        if self.kind == HomepageSectionKind.PUBLISHER_CAROUSEL:
            return bool(self.publishers)
        return True


_PRODUCT_KINDS = {
    HomepageSectionKind.CURATED_PRODUCTS,
    HomepageSectionKind.NEW_ARRIVALS,
    HomepageSectionKind.DISCOUNTED,
    HomepageSectionKind.BEST_SELLERS,
    HomepageSectionKind.MOST_VIEWED,
}


def _scoped(stmt, category_id: int | None):
    """Restrict a product query to one category and its descendants."""
    if category_id is None:
        return stmt
    descendants = select(Category.pk_category_id).where(
        Category.scd_active_flag.is_(True),
        (Category.pk_category_id == category_id)
        | (Category.ancestor_path.like(f"%/{category_id}/%")),
    )
    membership = select(ProductCategory.fk_product_id).where(
        ProductCategory.scd_active_flag.is_(True),
        ProductCategory.fk_category_id.in_(descendants),
    )
    return stmt.where(Product.pk_product_id.in_(membership))


def products_for_section(
    db: Session, section: HomepageSection, *, now: dt.datetime
) -> list[Product]:
    limit = max(1, min(section.item_limit, 48))
    stmt = _scoped(visible_products_stmt(), section.fk_category_id)

    match section.section_kind:
        case HomepageSectionKind.NEW_ARRIVALS:
            stmt = stmt.order_by(Product.published_dt.desc().nulls_last())

        case HomepageSectionKind.BEST_SELLERS:
            stmt = stmt.order_by(Product.purchase_count.desc())

        case HomepageSectionKind.MOST_VIEWED:
            stmt = stmt.order_by(Product.view_count.desc())

        case HomepageSectionKind.DISCOUNTED:
            # Products with a discount live right now — either their own, or one
            # on a category they belong to.
            live_discount = select(Discount.fk_product_id).where(
                Discount.scd_active_flag.is_(True),
                Discount.fk_product_id.is_not(None),
                (Discount.starts_dt.is_(None)) | (Discount.starts_dt <= now),
                (Discount.ends_dt.is_(None)) | (Discount.ends_dt > now),
            )
            discounted_categories = select(Discount.fk_category_id).where(
                Discount.scd_active_flag.is_(True),
                Discount.fk_category_id.is_not(None),
                (Discount.starts_dt.is_(None)) | (Discount.starts_dt <= now),
                (Discount.ends_dt.is_(None)) | (Discount.ends_dt > now),
            )
            by_category = select(ProductCategory.fk_product_id).where(
                ProductCategory.scd_active_flag.is_(True),
                ProductCategory.fk_category_id.in_(discounted_categories),
            )
            stmt = stmt.where(
                Product.pk_product_id.in_(live_discount)
                | Product.pk_product_id.in_(by_category)
            ).order_by(Product.purchase_count.desc())

        case HomepageSectionKind.CURATED_PRODUCTS:
            curated = (
                select(HomepageSectionItem.fk_product_id, HomepageSectionItem.sort_order)
                .where(
                    HomepageSectionItem.fk_homepage_section_id
                    == section.pk_homepage_section_id,
                    HomepageSectionItem.scd_active_flag.is_(True),
                )
                .order_by(HomepageSectionItem.sort_order)
                .subquery()
            )
            stmt = (
                stmt.join(curated, curated.c.fk_product_id == Product.pk_product_id)
                .order_by(curated.c.sort_order)
            )

        case _:
            return []

    return list(db.scalars(stmt.limit(limit)).unique().all())


def build_homepage(db: Session, *, now: dt.datetime | None = None) -> list[ResolvedSection]:
    """Resolve every live section, batching prices and stock across all of them.

    Prices and availability are looked up once for the union of every section's
    products rather than per section — the same product frequently appears in
    both Best Sellers and Discounted, and Part II §2 rules out the N+1.
    """
    now = now or utcnow()

    sections = db.scalars(
        select(HomepageSection)
        .where(HomepageSection.scd_active_flag.is_(True))
        .order_by(HomepageSection.sort_order, HomepageSection.pk_homepage_section_id)
    ).all()
    live = [s for s in sections if s.is_live(now)]

    products_by_section = {
        s.pk_homepage_section_id: products_for_section(db, s, now=now) for s in live
    }

    everything: dict[int, Product] = {}
    for products in products_by_section.values():
        for product in products:
            everything[product.pk_product_id] = product

    prices = price_products(db, list(everything.values()), now=now)
    availability = availability_for_products(db, list(everything.keys()))
    ratings = rating_summaries(db, list(everything.keys()))

    publishers: list[Publisher] = []
    if any(s.section_kind == HomepageSectionKind.PUBLISHER_CAROUSEL for s in live):
        publishers = list(
            db.scalars(
                select(Publisher)
                .where(
                    Publisher.scd_active_flag.is_(True),
                    Publisher.show_on_homepage_flag.is_(True),
                )
                .order_by(Publisher.sort_order, Publisher.pk_publisher_id)
            ).all()
        )

    resolved = [
        ResolvedSection(
            section=s,
            products=products_by_section[s.pk_homepage_section_id],
            prices=prices,
            availability=availability,
            ratings=ratings,
            publishers=publishers
            if s.section_kind == HomepageSectionKind.PUBLISHER_CAROUSEL
            else [],
        )
        for s in live
    ]
    return [section for section in resolved if section.has_content]
