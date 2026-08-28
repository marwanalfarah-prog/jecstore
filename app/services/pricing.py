"""Price and discount resolution (Part I §5.5, §1.1).

Prices are *always* computed, never cached onto a cart or a product row. That is
what makes the Part I §1.1 rule true in practice: an item sitting in an open
cart reflects the current rate and current discount at the moment of checkout,
not whatever applied when it was added.

The interesting rule here is overlap. When a product belongs to two categories
that each have a live promotion — "Bibles −20%" and "Christmas −15%" — the spec
is explicit that precedence is configurable *per product*, not one global
setting. :func:`resolve_price` implements all three modes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.catalog import Category, Discount, Product, ProductCategory
from app.models.enums import DiscountKind, DiscountScope, OverlapRule
from app.models.money import ExchangeRate

#: JOD is a three-decimal currency; every stored amount quantizes to fils.
FILS = Decimal("0.001")


def q(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(FILS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PriceView:
    """A resolved price, ready to render.

    Both figures are JOD — conversion to USD happens at the display layer only
    (Part I §1.1), so nothing downstream can accidentally persist a USD amount.
    """

    list_amt: Decimal
    final_amt: Decimal
    applied_discount_ids: tuple[int, ...] = ()

    @property
    def has_discount(self) -> bool:
        return self.final_amt < self.list_amt

    @property
    def saved_amt(self) -> Decimal:
        return q(self.list_amt - self.final_amt)

    @property
    def discount_percentage(self) -> int:
        """Rounded for the badge. The *amount* is authoritative — the badge is a
        label, and showing "21%" next to a 20.6% cut is noise."""
        if not self.has_discount or self.list_amt <= 0:
            return 0
        return int(
            (self.saved_amt / self.list_amt * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )


def current_usd_rate(db: Session, *, as_of: dt.datetime | None = None) -> Decimal:
    """The live JOD→USD display rate, or the one in force at ``as_of``.

    A past order reports at the rate at time of sale, which is why the rate is
    an SCD table rather than a settings value (Part I §1.1).
    """
    stmt = select(ExchangeRate)
    if as_of is None:
        stmt = stmt.where(ExchangeRate.scd_active_flag.is_(True))
    else:
        stmt = stmt.where(
            ExchangeRate.scd_active_from <= as_of,
            (ExchangeRate.scd_active_to > as_of) | (ExchangeRate.scd_active_to.is_(None)),
        )
    rate = db.scalars(stmt.order_by(ExchangeRate.scd_active_from.desc()).limit(1)).first()
    if rate is None:
        from app.core.config import settings

        return Decimal(str(settings.default_usd_rate))
    return Decimal(rate.jod_to_usd_rate)


def live_discounts_for_products(
    db: Session,
    product_ids: Sequence[int],
    *,
    now: dt.datetime | None = None,
) -> dict[int, list[Discount]]:
    """Every live discount touching each product, in one pass.

    Batched deliberately: a listing page resolves 50 prices, and doing this per
    product would be the textbook N+1 that Part II §2 rules out. Category
    discounts are matched through the ancestor path, so "20% off all Bibles"
    reaches every sub-category without being re-entered.
    """
    if not product_ids:
        return {}

    now = now or utcnow()

    # Which categories does each product sit in, and what are their ancestors?
    membership_rows = db.execute(
        select(ProductCategory.fk_product_id, Category.pk_category_id, Category.ancestor_path)
        .join(Category, Category.pk_category_id == ProductCategory.fk_category_id)
        .where(
            ProductCategory.fk_product_id.in_(product_ids),
            ProductCategory.scd_active_flag.is_(True),
            Category.scd_active_flag.is_(True),
        )
    ).all()

    categories_by_product: dict[int, set[int]] = {pid: set() for pid in product_ids}
    for product_id, category_id, ancestor_path in membership_rows:
        categories_by_product[product_id].add(category_id)
        # "/1/7/" -> {1, 7}: a discount on any ancestor applies here too.
        for part in ancestor_path.strip("/").split("/"):
            if part:
                categories_by_product[product_id].add(int(part))

    all_category_ids = {cid for ids in categories_by_product.values() for cid in ids}

    discounts = db.scalars(
        select(Discount).where(
            Discount.scd_active_flag.is_(True),
            (Discount.starts_dt.is_(None)) | (Discount.starts_dt <= now),
            (Discount.ends_dt.is_(None)) | (Discount.ends_dt > now),
            (
                Discount.fk_product_id.in_(product_ids)
                | (
                    Discount.fk_category_id.in_(all_category_ids)
                    if all_category_ids
                    else Discount.fk_category_id.is_(None) & False
                )
            ),
        )
    ).all()

    result: dict[int, list[Discount]] = {pid: [] for pid in product_ids}
    for discount in discounts:
        if discount.discount_scope == DiscountScope.PRODUCT:
            if discount.fk_product_id in result:
                result[discount.fk_product_id].append(discount)
        else:
            for product_id, category_ids in categories_by_product.items():
                if discount.fk_category_id in category_ids:
                    result[product_id].append(discount)
    return result


def _apply_one(base_amt: Decimal, discount: Discount) -> Decimal:
    if discount.discount_kind == DiscountKind.PERCENTAGE and discount.percentage is not None:
        return q(base_amt * (Decimal(100) - Decimal(discount.percentage)) / Decimal(100))
    if discount.discount_kind == DiscountKind.FIXED_PRICE and discount.fixed_price_amt is not None:
        return q(Decimal(discount.fixed_price_amt))
    return base_amt


def resolve_price(
    list_amt: Decimal,
    discounts: Iterable[Discount],
    overlap_rule: str = OverlapRule.BEST_FOR_CUSTOMER,
) -> PriceView:
    """Apply live discounts to a list price under the product's overlap rule.

    * ``BEST_FOR_CUSTOMER`` — take whichever single discount gives the lowest
      price. The safe default: never accidentally cheaper than intended.
    * ``ADDITIVE`` — apply them in sequence, each on the running price. Two 20%
      cuts give 36% off, not 40% — compounding, which is what "additive"
      means once discounts are percentages of a shrinking base.
    * ``FIRST_MATCH`` — highest ``priority`` wins, and nothing else applies.

    A fixed *price* discount is a floor rather than a factor, so under ADDITIVE
    it replaces the running price instead of compounding onto it.
    """
    list_amt = q(Decimal(list_amt))
    candidates = list(discounts)
    if not candidates:
        return PriceView(list_amt=list_amt, final_amt=list_amt)

    if overlap_rule == OverlapRule.FIRST_MATCH:
        winner = max(candidates, key=lambda d: (d.priority, d.pk_discount_id))
        final = _apply_one(list_amt, winner)
        return PriceView(list_amt, min(final, list_amt), (winner.pk_discount_id,))

    if overlap_rule == OverlapRule.ADDITIVE:
        running = list_amt
        applied: list[int] = []
        # Deterministic order: priority, then id — so the same inputs always
        # produce the same price, which matters for a reprinted invoice.
        for discount in sorted(candidates, key=lambda d: (-d.priority, d.pk_discount_id)):
            candidate = _apply_one(running, discount)
            if candidate < running:
                running = candidate
                applied.append(discount.pk_discount_id)
        return PriceView(list_amt, q(running), tuple(applied))

    # BEST_FOR_CUSTOMER (default)
    best_amt = list_amt
    best_id: int | None = None
    for discount in candidates:
        candidate = _apply_one(list_amt, discount)
        if candidate < best_amt:
            best_amt, best_id = candidate, discount.pk_discount_id
    return PriceView(list_amt, q(best_amt), (best_id,) if best_id else ())


def price_products(
    db: Session,
    products: Sequence[Product],
    *,
    now: dt.datetime | None = None,
) -> dict[int, PriceView]:
    """Resolve prices for a page of products in two queries, not 2N."""
    if not products:
        return {}
    product_ids = [p.pk_product_id for p in products]
    discounts = live_discounts_for_products(db, product_ids, now=now)
    return {
        p.pk_product_id: resolve_price(
            p.base_price_amt,
            discounts.get(p.pk_product_id, []),
            p.discount_overlap_rule,
        )
        for p in products
    }
