"""Promocode validation and discount calculation (Part I §13).

Three limit settings are deliberately independent, because §13 treats them as
distinct questions:

* ``single_use_globally_flag`` — the code works exactly once, for anybody.
* ``max_total_uses`` — reusable until a total cap across all customers.
* ``max_uses_per_customer`` — how often one customer may reuse it.

Usage counts are never stored on the promocode row. They are ``COUNT()`` over
``TRX_PROMOCODE_REDEMPTION``, aggregated in SQL (Part II §1) — a stored counter
and a redemption ledger can disagree, and then neither can be trusted.
Cancellations write a *negative* reversal row rather than deleting, so a
cancelled order correctly returns the use to the customer's allowance.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import PromocodeInvalid
from app.db.base import utcnow
from app.models.catalog import Category, Product, ProductCategory
from app.models.consignment import ConsignmentItem
from app.models.enums import PromocodeKind
from app.models.marketing import Promocode, PromocodeRedemption, PromocodeRestriction
from app.services.pricing import q


@dataclass(slots=True)
class PromocodeResult:
    """A validated code and what it is worth on this specific basket."""

    promocode: Promocode
    discount_amt: Decimal
    #: Which line subtotals the discount was computed against — useful for the
    #: order summary, and for explaining "why is my discount smaller than 20%".
    eligible_amt: Decimal

    @property
    def code(self) -> str:
        return self.promocode.code


def normalize_code(raw: str) -> str:
    return (raw or "").strip().upper()


def find_active(db: Session, raw_code: str) -> Promocode | None:
    code = normalize_code(raw_code)
    if not code:
        return None
    return db.scalars(
        select(Promocode).where(
            Promocode.code == code,
            Promocode.scd_active_flag.is_(True),
        )
    ).first()


def redemption_count(db: Session, promocode_id: int, *, user_id: int | None = None) -> int:
    """Net redemptions, counting reversals.

    A reversal row carries ``reverses_redemption_id``; counting rows that are
    neither reversals nor reversed gives the true number of live uses.
    """
    reversed_ids = select(PromocodeRedemption.reverses_redemption_id).where(
        PromocodeRedemption.fk_promocode_id == promocode_id,
        PromocodeRedemption.reverses_redemption_id.is_not(None),
    )
    stmt = (
        select(func.count())
        .select_from(PromocodeRedemption)
        .where(
            PromocodeRedemption.fk_promocode_id == promocode_id,
            PromocodeRedemption.reverses_redemption_id.is_(None),
            PromocodeRedemption.pk_promocode_redemption_id.not_in(reversed_ids),
        )
    )
    if user_id is not None:
        stmt = stmt.where(PromocodeRedemption.fk_user_id == user_id)
    return db.scalar(stmt) or 0


def _eligible_product_ids(
    db: Session, promocode: Promocode, product_ids: list[int]
) -> set[int]:
    """Apply category/product restrictions, inclusions and exclusions (§13).

    With no restriction rows the code applies to everything. Inclusion rows
    narrow it; exclusion rows carve out of whatever remains, so "everything
    except Bibles" does not require listing every other category.
    """
    if not product_ids:
        return set()

    restrictions = db.scalars(
        select(PromocodeRestriction).where(
            PromocodeRestriction.fk_promocode_id == promocode.pk_promocode_id,
            PromocodeRestriction.scd_active_flag.is_(True),
        )
    ).all()
    if not restrictions:
        return set(product_ids)

    # Map each product to its categories, ancestors included, so a restriction
    # on a parent category reaches its children.
    membership = db.execute(
        select(ProductCategory.fk_product_id, Category.pk_category_id, Category.ancestor_path)
        .join(Category, Category.pk_category_id == ProductCategory.fk_category_id)
        .where(
            ProductCategory.fk_product_id.in_(product_ids),
            ProductCategory.scd_active_flag.is_(True),
            Category.scd_active_flag.is_(True),
        )
    ).all()

    categories_by_product: dict[int, set[int]] = {pid: set() for pid in product_ids}
    for product_id, category_id, ancestor_path in membership:
        categories_by_product[product_id].add(category_id)
        for part in ancestor_path.strip("/").split("/"):
            if part:
                categories_by_product[product_id].add(int(part))

    includes = [r for r in restrictions if not r.is_exclusion_flag]
    excludes = [r for r in restrictions if r.is_exclusion_flag]

    def matches(rule: PromocodeRestriction, product_id: int) -> bool:
        if rule.fk_product_id is not None:
            return rule.fk_product_id == product_id
        if rule.fk_category_id is not None:
            return rule.fk_category_id in categories_by_product.get(product_id, set())
        return False

    eligible = {
        pid
        for pid in product_ids
        if not includes or any(matches(rule, pid) for rule in includes)
    }
    return {pid for pid in eligible if not any(matches(rule, pid) for rule in excludes)}


def _consigned_product_ids(db: Session, product_ids: list[int]) -> set[int]:
    """Products currently held under a consignment arrangement.

    Eligibility needs *both* switches on: the promocode's
    ``applies_to_consigned_flag`` and the arrangement's own
    ``promocodes_eligible_flag`` (Part I §7). This returns the products where
    the arrangement side says no.
    """
    from app.models.catalog import ProductVariant
    from app.models.consignment import Consignment

    rows = db.execute(
        select(ProductVariant.fk_product_id)
        .join(
            ConsignmentItem,
            ConsignmentItem.fk_product_variant_id == ProductVariant.pk_product_variant_id,
        )
        .join(Consignment, Consignment.pk_consignment_id == ConsignmentItem.fk_consignment_id)
        .where(
            ProductVariant.fk_product_id.in_(product_ids),
            ConsignmentItem.scd_active_flag.is_(True),
            Consignment.scd_active_flag.is_(True),
            Consignment.promocodes_eligible_flag.is_(False),
        )
    ).all()
    return {row[0] for row in rows}


def validate(
    db: Session,
    raw_code: str,
    *,
    user_id: int | None,
    line_totals: dict[int, Decimal],
    has_item_discount: dict[int, bool] | None = None,
    now: dt.datetime | None = None,
) -> PromocodeResult:
    """Validate a code against this basket and compute what it is worth.

    ``line_totals`` maps product id → that product's discounted line subtotal.
    Raises :class:`PromocodeInvalid` with a specific message for every failure —
    a shopper who typed a valid-but-expired code should be told which it is.
    """
    now = now or utcnow()
    promocode = find_active(db, raw_code)
    if promocode is None:
        raise PromocodeInvalid("That promocode was not recognised.")

    if promocode.starts_dt and promocode.starts_dt > now:
        raise PromocodeInvalid("That promocode is not active yet.")
    if promocode.expires_dt and promocode.expires_dt <= now:
        raise PromocodeInvalid("That promocode has expired.")

    total_uses = redemption_count(db, promocode.pk_promocode_id)
    if promocode.single_use_globally_flag and total_uses >= 1:
        raise PromocodeInvalid("That promocode has already been used.")
    if promocode.max_total_uses is not None and total_uses >= promocode.max_total_uses:
        raise PromocodeInvalid("That promocode has reached its usage limit.")

    if user_id is not None and promocode.max_uses_per_customer is not None:
        mine = redemption_count(db, promocode.pk_promocode_id, user_id=user_id)
        if mine >= promocode.max_uses_per_customer:
            raise PromocodeInvalid("You have already used that promocode.")

    product_ids = list(line_totals)
    eligible_ids = _eligible_product_ids(db, promocode, product_ids)

    # Consigned items qualify only if both the code and the arrangement allow it.
    if not promocode.applies_to_consigned_flag:
        eligible_ids -= _consigned_product_ids(db, product_ids)

    # Codes that do not stack skip anything already carrying an item discount.
    if not promocode.stacks_with_item_discount_flag and has_item_discount:
        eligible_ids = {pid for pid in eligible_ids if not has_item_discount.get(pid)}

    if not eligible_ids:
        raise PromocodeInvalid("That promocode does not apply to anything in your cart.")

    eligible_amt = q(sum((line_totals[pid] for pid in eligible_ids), Decimal("0")))
    basket_amt = q(sum(line_totals.values(), Decimal("0")))

    # The minimum is judged on the whole basket, not just the eligible part —
    # "spend 30 JOD to qualify" means what the customer actually spends.
    if promocode.minimum_order_amt and basket_amt < promocode.minimum_order_amt:
        raise PromocodeInvalid(
            "Your order does not meet the minimum for that promocode.",
            details={"minimum_amt": str(promocode.minimum_order_amt)},
        )

    discount = _calculate(promocode, eligible_amt)
    if discount <= 0:
        raise PromocodeInvalid("That promocode does not reduce this order.")

    return PromocodeResult(
        promocode=promocode,
        discount_amt=discount,
        eligible_amt=eligible_amt,
    )


def _calculate(promocode: Promocode, eligible_amt: Decimal) -> Decimal:
    """Value of the code against the eligible subtotal.

    Never exceeds the eligible amount: a 10 JOD flat code on a 6 JOD basket
    takes 6, not 10 — a promocode is a discount, never a payout.
    """
    match promocode.promocode_kind:
        case PromocodeKind.PERCENTAGE:
            discount = eligible_amt * Decimal(promocode.percentage or 0) / Decimal(100)

        case PromocodeKind.PERCENTAGE_CAPPED:
            raw = eligible_amt * Decimal(promocode.percentage or 0) / Decimal(100)
            cap = Decimal(promocode.max_discount_amt or 0)
            discount = min(raw, cap) if cap > 0 else raw

        case PromocodeKind.FIXED_AMOUNT:
            discount = Decimal(promocode.fixed_amount_amt or 0)

        case _:  # pragma: no cover - kind is constrained by the enum
            discount = Decimal("0")

    return q(min(discount, eligible_amt))


def record_redemption(
    db: Session,
    promocode: Promocode,
    *,
    order_id: int,
    user_id: int,
    discount_amt: Decimal,
) -> PromocodeRedemption:
    redemption = PromocodeRedemption(
        fk_promocode_id=promocode.pk_promocode_id,
        fk_order_id=order_id,
        fk_user_id=user_id,
        discount_amt=discount_amt,
        created_dt=utcnow(),
        created_by=user_id,
    )
    db.add(redemption)
    return redemption


def reverse_redemption(
    db: Session, redemption: PromocodeRedemption, *, actor_user_id: int | None = None
) -> PromocodeRedemption:
    """Return a use to the customer's allowance when an order is cancelled.

    A new negative row, never an edit — ``TRX_`` tables are insert-only, and the
    original redemption is a fact that happened (Part II §1).
    """
    reversal = PromocodeRedemption(
        fk_promocode_id=redemption.fk_promocode_id,
        fk_order_id=redemption.fk_order_id,
        fk_user_id=redemption.fk_user_id,
        discount_amt=-Decimal(redemption.discount_amt),
        reverses_redemption_id=redemption.pk_promocode_redemption_id,
        created_dt=utcnow(),
        created_by=actor_user_id,
    )
    db.add(reversal)
    return reversal
