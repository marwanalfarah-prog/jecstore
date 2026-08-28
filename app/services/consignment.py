"""Consignment in both directions (Part I §7).

One arrangement type covers both directions because the mechanics are identical
and only the *sign of who owes whom* changes:

* **Inbound** — their items, held and sold by us. The stock sits in our branch
  and is sellable, but it is not ours: it stays out of the cost-average pool
  (§7, §11), and when a unit sells we owe the consignor their share.
* **Outbound** — our items, held by someone else to sell (a bazaar). The stock
  leaves our sellable pool but remains ours, and when a unit sells they owe us
  our share.

That sign convention is the one thing to keep straight, and it lives in exactly
one place: :func:`settlement_position`, where a **positive net means we owe
them**.

Every Admin-configurable choice §7 calls out is on the arrangement or the item,
not hardcoded:

* the revenue split — per item, or an arrangement-wide default;
* whether the split is taken on the **discounted** or the **original** price;
* whether storewide promocodes may touch these items at all.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import ProductVariant
from app.models.consignment import (
    Consignment,
    ConsignmentItem,
    ConsignmentSale,
    ConsignmentSettlement,
    Consignor,
)
from app.models.enums import (
    ConsignmentDirection,
    ConsignmentItemState,
    ConsignmentSplitBasis,
    MoneyDirection,
    MoneyReason,
    MovementKind,
    StockPoolKind,
    WriteOffReason,
)
from app.models.inventory import StockLevel, StockMovement, StockPool
from app.models.orders import Order, OrderLine
from app.services import locking, money
from app.services.pricing import q

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Arrangements
# ---------------------------------------------------------------------------


def create_consignor(
    db: Session,
    *,
    name: str,
    contact_person: str | None = None,
    phone_country_code: str | None = None,
    phone_number: str | None = None,
    email: str | None = None,
    user_id: int | None = None,
    note: str | None = None,
) -> Consignor:
    consignor = Consignor(
        name=name.strip(),
        contact_person=contact_person,
        phone_country_code=phone_country_code,
        phone_number=phone_number,
        email=email,
        fk_user_id=user_id,
        note=note,
        scd_active_from=utcnow(),
    )
    db.add(consignor)
    db.flush()
    return consignor


def create_arrangement(
    db: Session,
    *,
    reference: str,
    consignor_id: int,
    direction: str,
    default_our_share_percentage: Decimal,
    split_basis: str = ConsignmentSplitBasis.DISCOUNTED_PRICE,
    promocodes_eligible: bool = False,
    stock_pool_id: int | None = None,
    starts_date: dt.date | None = None,
    ends_date: dt.date | None = None,
    note: str | None = None,
) -> Consignment:
    """Open an arrangement, creating its stock pool if one is not supplied.

    The pool's flags are what enforce §7's inventory rules, so they are derived
    from the direction rather than left to the caller:

    * inbound  — sellable (it sits in our branch) but **not owned**, so it never
      enters the cost-average pool;
    * outbound — **not sellable** (we no longer hold it) but still ours.
    """
    if not (Decimal(0) <= Decimal(default_our_share_percentage) <= Decimal(100)):
        raise ValidationFailed("The revenue share must be between 0 and 100 percent.")
    if direction not in set(ConsignmentDirection):
        raise ValidationFailed("Choose a consignment direction.")

    consignor = db.get(Consignor, consignor_id)
    if consignor is None:
        raise NotFound("That consignor does not exist.")

    now = utcnow()
    if stock_pool_id is None:
        inbound = direction == ConsignmentDirection.INBOUND
        pool = StockPool(
            pool_kind=(
                StockPoolKind.BRANCH if inbound else StockPoolKind.CONSIGNMENT_OUT
            ),
            name_ar=f"أمانة — {consignor.name}",
            name_en=f"Consignment — {consignor.name}",
            is_sellable_flag=inbound,
            is_owned_flag=not inbound,
            scd_active_from=now,
        )
        db.add(pool)
        db.flush()
        stock_pool_id = pool.pk_stock_pool_id

    arrangement = Consignment(
        reference=reference,
        fk_consignor_id=consignor_id,
        direction=direction,
        default_our_share_percentage=q(Decimal(default_our_share_percentage)),
        split_basis=split_basis,
        promocodes_eligible_flag=promocodes_eligible,
        fk_stock_pool_id=stock_pool_id,
        starts_date=starts_date,
        ends_date=ends_date,
        note=note,
        scd_active_from=now,
    )
    db.add(arrangement)
    db.flush()

    log.info(
        "consignment_created",
        extra={"reference": reference, "direction": direction},
    )
    return arrangement


def place_items(
    db: Session,
    arrangement: Consignment,
    *,
    variant_id: int,
    quantity: int,
    agreed_price_amt: Decimal | None = None,
    our_share_percentage: Decimal | None = None,
    actor_user_id: int | None = None,
) -> ConsignmentItem:
    """Put units under the arrangement and move the stock to match.

    Inbound units *arrive* into the consignment pool; outbound units *leave* our
    own stock for it. Either way the pool's ownership flag keeps them out of the
    wrong totals (§7).
    """
    if quantity <= 0:
        raise ValidationFailed("Quantity must be positive.")
    if arrangement.closed_dt is not None:
        raise Conflict("This arrangement is closed.")

    now = utcnow()
    item = ConsignmentItem(
        fk_consignment_id=arrangement.pk_consignment_id,
        fk_product_variant_id=variant_id,
        quantity_placed=quantity,
        agreed_price_amt=q(agreed_price_amt) if agreed_price_amt is not None else None,
        our_share_percentage=(
            q(Decimal(our_share_percentage)) if our_share_percentage is not None else None
        ),
        state=ConsignmentItemState.HELD,
        scd_active_from=now,
    )
    db.add(item)
    db.flush()

    pool_id = arrangement.fk_stock_pool_id
    inbound = arrangement.direction == ConsignmentDirection.INBOUND

    if inbound:
        # Their goods arrive in our branch. Cost stays zero: we never paid for
        # them, and §7 keeps them out of the cost-average pool.
        level = _ensure_level(db, variant_id, pool_id, now)
        level.quantity_on_hand += quantity
        level.last_movement_dt = now
        db.add(
            StockMovement(
                fk_product_variant_id=variant_id,
                fk_stock_pool_id=pool_id,
                movement_kind=MovementKind.CONSIGNMENT_OUT,
                quantity_delta=quantity,
                fk_consignment_id=arrangement.pk_consignment_id,
                note=f"Received on consignment {arrangement.reference}",
                created_dt=now,
                created_by=actor_user_id,
            )
        )
    else:
        # Our goods go out to them. They leave our sellable stock but stay ours.
        levels = locking.lock_stock_levels(db, [variant_id])
        source = next(
            (l for l in levels if l.fk_stock_pool_id != pool_id and l.quantity_sellable > 0),
            None,
        )
        if source is None or source.quantity_sellable < quantity:
            raise Conflict("Not enough sellable stock to send on consignment.")

        unit_cost = Decimal(source.average_cost_amt or 0)
        source.quantity_on_hand -= quantity
        source.last_movement_dt = now

        destination = _ensure_level(db, variant_id, pool_id, now)
        destination.quantity_on_hand += quantity
        destination.average_cost_amt = unit_cost
        destination.last_movement_dt = now

        db.add(
            StockMovement(
                fk_product_variant_id=variant_id,
                fk_stock_pool_id=source.fk_stock_pool_id,
                movement_kind=MovementKind.CONSIGNMENT_OUT,
                quantity_delta=-quantity,
                unit_cost_amt=unit_cost,
                fk_consignment_id=arrangement.pk_consignment_id,
                note=f"Sent on consignment {arrangement.reference}",
                created_dt=now,
                created_by=actor_user_id,
            )
        )

    log.info(
        "consignment_items_placed",
        extra={"reference": arrangement.reference, "quantity": quantity},
    )
    db.flush()
    return item


def _ensure_level(
    db: Session, variant_id: int, pool_id: int, now: dt.datetime
) -> StockLevel:
    existing = db.scalars(
        select(StockLevel).where(
            StockLevel.fk_product_variant_id == variant_id,
            StockLevel.fk_stock_pool_id == pool_id,
            StockLevel.scd_active_flag.is_(True),
        )
    ).first()
    if existing is not None:
        return existing

    level = StockLevel(
        fk_product_variant_id=variant_id,
        fk_stock_pool_id=pool_id,
        quantity_on_hand=0,
        quantity_reserved=0,
        average_cost_amt=Decimal("0"),
        last_movement_dt=now,
        scd_active_from=now,
    )
    db.add(level)
    db.flush()
    return level


# ---------------------------------------------------------------------------
# Selling a consigned unit
# ---------------------------------------------------------------------------


def item_for_variant(
    db: Session, variant_id: int, *, stock_pool_id: int | None = None
) -> ConsignmentItem | None:
    """The open consignment holding for a variant, if there is one.

    Used at checkout to tag an order line, so the split can be computed when the
    sale is finalised.
    """
    rows = db.scalars(
        select(ConsignmentItem)
        .join(
            Consignment,
            Consignment.pk_consignment_id == ConsignmentItem.fk_consignment_id,
        )
        .where(
            ConsignmentItem.fk_product_variant_id == variant_id,
            ConsignmentItem.scd_active_flag.is_(True),
            ConsignmentItem.state == ConsignmentItemState.HELD,
            Consignment.scd_active_flag.is_(True),
            Consignment.closed_dt.is_(None),
        )
        .order_by(ConsignmentItem.pk_consignment_item_id)
    ).all()
    if stock_pool_id is not None:
        rows = [
            item
            for item in rows
            if item.consignment
            and item.consignment.fk_stock_pool_id == stock_pool_id
        ]
    return next((item for item in rows if item.quantity_outstanding > 0), None)


def split_for(item: ConsignmentItem, arrangement: Consignment) -> Decimal:
    """Our percentage: the item override, else the arrangement default (§7)."""
    if item.our_share_percentage is not None:
        return Decimal(item.our_share_percentage)
    return Decimal(arrangement.default_our_share_percentage)


def record_sale(
    db: Session,
    item: ConsignmentItem,
    *,
    quantity: int,
    list_price_amt: Decimal,
    sold_price_amt: Decimal,
    order_line_id: int | None = None,
    actor_user_id: int | None = None,
) -> ConsignmentSale:
    """Record a consigned sale and the revenue split it produced.

    Which price the split is taken on is Admin's choice per arrangement (§7):
    ``DISCOUNTED_PRICE`` splits what the customer actually paid;
    ``ORIGINAL_PRICE`` splits the listed price, so a discount comes out of our
    share rather than the consignor's.
    """
    arrangement = db.get(Consignment, item.fk_consignment_id)
    if arrangement is None:
        raise NotFound("That consignment arrangement does not exist.")
    if quantity <= 0:
        raise ValidationFailed("Sale quantity must be positive.")

    percentage = split_for(item, arrangement)

    sold_gross = q(Decimal(sold_price_amt) * quantity)
    list_gross = q(Decimal(list_price_amt) * quantity)
    if arrangement.split_basis == ConsignmentSplitBasis.DISCOUNTED_PRICE:
        # Discounted basis splits what was actually paid (Part I §7).
        our_share = q(sold_gross * percentage / Decimal(100))
        their_share = q(sold_gross - our_share)
    else:
        # Original-price basis keeps the counterparty's share on list price, so
        # any customer discount comes out of our share (Part I §7).
        their_share = q(list_gross * (Decimal(100) - percentage) / Decimal(100))
        our_share = q(sold_gross - their_share)

    sale = ConsignmentSale(
        fk_consignment_item_id=item.pk_consignment_item_id,
        fk_order_line_id=order_line_id,
        quantity=quantity,
        list_price_amt=q(Decimal(list_price_amt)),
        sold_price_amt=q(Decimal(sold_price_amt)),
        split_basis=arrangement.split_basis,
        our_share_percentage=percentage,
        our_share_amt=our_share,
        their_share_amt=their_share,
        created_dt=utcnow(),
        created_by=actor_user_id,
    )
    db.add(sale)

    item.quantity_sold = (item.quantity_sold or 0) + quantity
    if item.quantity_outstanding <= 0:
        item.state = ConsignmentItemState.SOLD

    db.flush()
    log.info(
        "consignment_sale",
        extra={
            "item": item.pk_consignment_item_id,
            "quantity": quantity,
            "their_share": str(their_share),
        },
    )
    return sale


def record_sale_for_order_line(
    db: Session, order_line: OrderLine, *, actor_user_id: int | None = None
) -> ConsignmentSale | None:
    """Write the split when a consigned order line is handed over.

    Called from the fulfilment path; a no-op for ordinary owned stock.
    """
    if order_line.fk_consignment_item_id is None:
        return None

    item = db.get(ConsignmentItem, order_line.fk_consignment_item_id)
    if item is None:
        return None

    return record_sale(
        db,
        item,
        quantity=order_line.quantity,
        list_price_amt=Decimal(order_line.list_price_amt),
        sold_price_amt=Decimal(order_line.unit_price_amt),
        order_line_id=order_line.pk_order_line_id,
        actor_user_id=actor_user_id,
    )


# ---------------------------------------------------------------------------
# Recalls, returns and losses
# ---------------------------------------------------------------------------


def return_to_consignor(
    db: Session,
    item: ConsignmentItem,
    *,
    quantity: int | None = None,
    actor_user_id: int | None = None,
) -> ConsignmentItem:
    """Send unsold units back.

    ``quantity=None`` returns everything outstanding. A partial quantity is the
    "partial recall" §7 describes — a consignor asking for some units back
    before settlement, handled the same way as a full return.
    """
    outstanding = item.quantity_outstanding
    if outstanding <= 0:
        raise Conflict("There are no unsold units left to return.")

    amount = outstanding if quantity is None else int(quantity)
    if amount <= 0 or amount > outstanding:
        raise ValidationFailed(
            "That is more than is still held.",
            details={"outstanding": outstanding, "requested": amount},
        )

    arrangement = db.get(Consignment, item.fk_consignment_id)
    now = utcnow()
    pool_id = arrangement.fk_stock_pool_id
    inbound = arrangement.direction == ConsignmentDirection.INBOUND

    levels = _active_levels_for_variant(db, item.fk_product_variant_id)
    consignment_level = next(
        (l for l in levels if l.fk_stock_pool_id == pool_id), None
    )
    if consignment_level is not None:
        consignment_level.quantity_on_hand = max(
            consignment_level.quantity_on_hand - amount, 0
        )
        consignment_level.last_movement_dt = now

    if not inbound:
        # Our goods coming home: they rejoin our own sellable stock.
        own = next((l for l in levels if l.fk_stock_pool_id != pool_id), None)
        if own is not None:
            own.quantity_on_hand += amount
            own.last_movement_dt = now
            db.add(
                StockMovement(
                    fk_product_variant_id=item.fk_product_variant_id,
                    fk_stock_pool_id=own.fk_stock_pool_id,
                    movement_kind=MovementKind.CONSIGNMENT_RETURN,
                    quantity_delta=amount,
                    unit_cost_amt=own.average_cost_amt,
                    fk_consignment_id=arrangement.pk_consignment_id,
                    note=f"Returned from consignment {arrangement.reference}",
                    created_dt=now,
                    created_by=actor_user_id,
                )
            )

    db.add(
        StockMovement(
            fk_product_variant_id=item.fk_product_variant_id,
            fk_stock_pool_id=pool_id,
            movement_kind=MovementKind.CONSIGNMENT_RETURN,
            quantity_delta=-amount,
            fk_consignment_id=arrangement.pk_consignment_id,
            note=f"Returned to consignor ({arrangement.reference})",
            created_dt=now,
            created_by=actor_user_id,
        )
    )

    item.quantity_returned = (item.quantity_returned or 0) + amount
    if item.quantity_outstanding <= 0:
        item.state = (
            ConsignmentItemState.RECALLED
            if amount < outstanding
            else ConsignmentItemState.RETURNED
        )

    db.flush()
    log.info(
        "consignment_returned",
        extra={"item": item.pk_consignment_item_id, "quantity": amount},
    )
    return item


def mark_damaged_or_lost(
    db: Session,
    item: ConsignmentItem,
    *,
    quantity: int,
    reason: str = WriteOffReason.DAMAGED,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> ConsignmentItem:
    """Damage or loss in custody (§7).

    The units are excluded from sales and inventory reporting going forward, but
    their **cost stays in the item's historical cost calculation** — §7 is
    explicit that it is not simply erased, which is why this writes a WRITE_OFF
    movement carrying the unit cost rather than silently decrementing.
    """
    outstanding = item.quantity_outstanding
    if quantity <= 0 or quantity > outstanding:
        raise ValidationFailed(
            "That is more than is still held.",
            details={"outstanding": outstanding, "requested": quantity},
        )

    arrangement = db.get(Consignment, item.fk_consignment_id)
    now = utcnow()
    pool_id = arrangement.fk_stock_pool_id

    levels = _active_levels_for_variant(db, item.fk_product_variant_id)
    level = next((l for l in levels if l.fk_stock_pool_id == pool_id), None)
    unit_cost = Decimal(level.average_cost_amt or 0) if level else Decimal("0")
    if level is not None:
        level.quantity_on_hand = max(level.quantity_on_hand - quantity, 0)
        level.last_movement_dt = now

    db.add(
        StockMovement(
            fk_product_variant_id=item.fk_product_variant_id,
            fk_stock_pool_id=pool_id,
            movement_kind=MovementKind.WRITE_OFF,
            quantity_delta=-quantity,
            # Carried deliberately: the cost remains part of the item's history.
            unit_cost_amt=unit_cost,
            write_off_reason=reason,
            fk_consignment_id=arrangement.pk_consignment_id,
            note=note or f"Lost in custody ({arrangement.reference})",
            created_dt=now,
            created_by=actor_user_id,
        )
    )

    item.quantity_damaged_or_lost = (item.quantity_damaged_or_lost or 0) + quantity
    if item.quantity_outstanding <= 0:
        item.state = ConsignmentItemState.DAMAGED_OR_LOST

    db.flush()
    log.info(
        "consignment_damaged_or_lost",
        extra={"item": item.pk_consignment_item_id, "quantity": quantity},
    )
    return item


# ---------------------------------------------------------------------------
# Settlement (Part I §7, §10)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SettlementPosition:
    """What is owed on an arrangement, and to whom."""

    arrangement: Consignment
    our_share_amt: Decimal
    their_share_amt: Decimal
    unsettled_sales: list[ConsignmentSale]

    @property
    def net_owed_amt(self) -> Decimal:
        """Positive: we owe the consignor. Negative: they owe us.

        Inbound — we sold their goods, so their share is a payable.
        Outbound — they sold our goods, so our share is a receivable.
        """
        if self.arrangement.direction == ConsignmentDirection.INBOUND:
            return q(self.their_share_amt)
        return q(-self.our_share_amt)

    @property
    def we_owe(self) -> bool:
        return self.net_owed_amt > 0


def settlement_position(db: Session, arrangement: Consignment) -> SettlementPosition:
    """Everything sold but not yet settled on this arrangement.

    Reversals net themselves out automatically: a customer return writes a
    negative sale row (§7, and ``services/returns.py``), so summing the
    unsettled rows gives the true outstanding position without special-casing.
    """
    items = select(ConsignmentItem.pk_consignment_item_id).where(
        ConsignmentItem.fk_consignment_id == arrangement.pk_consignment_id,
        ConsignmentItem.scd_active_flag.is_(True),
    )
    sales = list(
        db.scalars(
            select(ConsignmentSale)
            .where(
                ConsignmentSale.fk_consignment_item_id.in_(items),
                ConsignmentSale.fk_consignment_settlement_id.is_(None),
            )
            .order_by(ConsignmentSale.pk_consignment_sale_id)
        ).all()
    )

    return SettlementPosition(
        arrangement=arrangement,
        our_share_amt=q(sum((Decimal(s.our_share_amt) for s in sales), Decimal("0"))),
        their_share_amt=q(
            sum((Decimal(s.their_share_amt) for s in sales), Decimal("0"))
        ),
        unsettled_sales=sales,
    )


def settle(
    db: Session,
    arrangement: Consignment,
    *,
    money_box_id: int | None = None,
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> ConsignmentSettlement:
    """Close out what is owed and move the money (§7, §10).

    Every sale rolled into the settlement is stamped with its id, so a second
    payout cannot double-count the same sales.
    """
    position = settlement_position(db, arrangement)
    if not position.unsettled_sales:
        raise Conflict("There is nothing outstanding to settle.")

    now = utcnow()
    settlement = ConsignmentSettlement(
        reference=_next_reference(db, now),
        fk_consignment_id=arrangement.pk_consignment_id,
        period_start_date=period_start,
        period_end_date=period_end,
        net_owed_amt=position.net_owed_amt,
        settled_dt=now,
        settled_by_user_id=actor_user_id,
        note=note,
        scd_active_from=now,
    )
    db.add(settlement)
    db.flush()

    sale_ids = [sale.pk_consignment_sale_id for sale in position.unsettled_sales]
    if sale_ids:
        # Settlement membership is a one-time clearing marker (§7). Use a Core
        # update so the ORM's TRX_ immutability guard still catches business
        # fact edits to the sale rows.
        db.execute(
            update(ConsignmentSale)
            .where(
                ConsignmentSale.pk_consignment_sale_id.in_(sale_ids),
                ConsignmentSale.fk_consignment_settlement_id.is_(None),
            )
            .values(
                fk_consignment_settlement_id=settlement.pk_consignment_settlement_id
            )
            .execution_options(synchronize_session=False)
        )
        for sale in position.unsettled_sales:
            set_committed_value(
                sale,
                "fk_consignment_settlement_id",
                settlement.pk_consignment_settlement_id,
            )

    amount = position.net_owed_amt
    if amount != 0:
        if money_box_id is None:
            raise ValidationFailed("Choose which money box the settlement uses.")
        money.record_transaction(
            db,
            # Paying the consignor takes money out; collecting from them brings
            # it in.
            direction=MoneyDirection.OUT if amount > 0 else MoneyDirection.IN,
            reason_code=MoneyReason.CONSIGNMENT_SETTLEMENT,
            allocations=[(money_box_id, -amount)],
            settlement_id=settlement.pk_consignment_settlement_id,
            description=f"Consignment settlement {settlement.reference}",
            actor_user_id=actor_user_id,
        )

    log.info(
        "consignment_settled",
        extra={"reference": settlement.reference, "net": str(amount)},
    )
    db.flush()
    return settlement


# ---------------------------------------------------------------------------
# Reporting (Part I §7: who holds what, what sold, what is owed)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HoldingRow:
    item: ConsignmentItem
    variant: ProductVariant | None
    product: object | None

    @property
    def outstanding(self) -> int:
        return self.item.quantity_outstanding


def holdings(db: Session, arrangement: Consignment) -> list[HoldingRow]:
    from app.models.catalog import Product

    items = db.scalars(
        select(ConsignmentItem)
        .where(
            ConsignmentItem.fk_consignment_id == arrangement.pk_consignment_id,
            ConsignmentItem.scd_active_flag.is_(True),
        )
        .order_by(ConsignmentItem.pk_consignment_item_id)
    ).all()

    variants = {
        v.pk_product_variant_id: v
        for v in db.scalars(
            select(ProductVariant).where(
                ProductVariant.pk_product_variant_id.in_(
                    [i.fk_product_variant_id for i in items] or [0]
                )
            )
        ).all()
    }
    products = {
        p.pk_product_id: p
        for p in db.scalars(
            select(Product).where(
                Product.pk_product_id.in_(
                    [v.fk_product_id for v in variants.values()] or [0]
                )
            )
        ).all()
    }

    return [
        HoldingRow(
            item=item,
            variant=variants.get(item.fk_product_variant_id),
            product=products.get(
                variants[item.fk_product_variant_id].fk_product_id
            )
            if item.fk_product_variant_id in variants
            else None,
        )
        for item in items
    ]


def list_arrangements(
    db: Session, *, direction: str | None = None, open_only: bool = False
) -> list[Consignment]:
    stmt = select(Consignment).where(Consignment.scd_active_flag.is_(True))
    if direction:
        stmt = stmt.where(Consignment.direction == direction)
    if open_only:
        stmt = stmt.where(Consignment.closed_dt.is_(None))
    return list(
        db.scalars(stmt.order_by(Consignment.pk_consignment_id.desc())).all()
    )


def settlements_for(db: Session, arrangement: Consignment) -> list[ConsignmentSettlement]:
    return list(
        db.scalars(
            select(ConsignmentSettlement)
            .where(
                ConsignmentSettlement.fk_consignment_id == arrangement.pk_consignment_id,
                ConsignmentSettlement.scd_active_flag.is_(True),
            )
            .order_by(ConsignmentSettlement.pk_consignment_settlement_id.desc())
        ).all()
    )


def close_arrangement(db: Session, arrangement: Consignment) -> Consignment:
    """Close an arrangement, refusing while anything is unsettled or still held."""
    position = settlement_position(db, arrangement)
    if position.unsettled_sales:
        raise Conflict("Settle the outstanding sales before closing.")

    outstanding = sum(row.outstanding for row in holdings(db, arrangement))
    if outstanding > 0:
        raise Conflict(
            "Return or write off the remaining units before closing.",
            details={"outstanding": outstanding},
        )

    arrangement.closed_dt = utcnow()
    return arrangement


def _next_reference(db: Session, now: dt.datetime) -> str:
    stem = f"CST-{now:%y%m%d}"
    count = db.scalar(
        select(func.count())
        .select_from(ConsignmentSettlement)
        .where(ConsignmentSettlement.reference.like(f"{stem}-%"))
    ) or 0
    return f"{stem}-{count + 1:03d}"


def _active_levels_for_variant(db: Session, variant_id: int) -> list[StockLevel]:
    if db.get_bind().dialect.name == "sqlite":
        db.execute(
            update(StockLevel)
            .where(
                StockLevel.fk_product_variant_id == variant_id,
                StockLevel.scd_active_flag.is_(True),
            )
            .values(quantity_reserved=StockLevel.quantity_reserved)
            .execution_options(synchronize_session=False)
        )

    return list(
        db.scalars(
            select(StockLevel)
            .where(
                StockLevel.fk_product_variant_id == variant_id,
                StockLevel.scd_active_flag.is_(True),
            )
            .order_by(StockLevel.pk_stock_level_id)
        ).all()
    )
