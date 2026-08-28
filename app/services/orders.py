"""Order fulfilment, adjustment and cancellation (Part I §9, §8).

Three rules from the spec shape this module.

**Status lives on the line.** Split and mixed fulfilment are both explicitly
required (§9) — some items shipping now while others are backordered, some
picked up by hand while others delivered — so the order-level status is a
*rollup* computed from its lines, never set directly.

**Stock is deducted at hand-over, not at checkout.** Quantities go on hold when
the order is placed (§8); on-hand only moves when the customer actually receives
the goods. :func:`mark_delivered` is the one place that transition happens.

**Editing a placed order does not disturb the customer.** §9 is explicit: staff
may modify an order's items after placement but before fulfilment, and doing so
does *not* re-trigger stock-hold recalculation on the customer side, nor require
them to reconfirm. So the adjustments here re-reserve stock quietly and leave
the customer's view alone.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import Product, ProductVariant
from app.models.enums import (
    EmailTemplateCode,
    FulfillmentMethod,
    LineFulfillmentStatus,
    MoneyDirection,
    MoneyReason,
    MovementKind,
    OrderStatus,
    PaymentStatus,
    RefundDestination,
)
from app.models.identity import User
from app.models.inventory import StockMovement
from app.models.marketing import PromocodeRedemption
from app.models.money import MoneyTransaction
from app.models.orders import Order, OrderLine
from app.services import locking, money, promocodes
from app.services.pricing import q

log = get_logger(__name__)

#: Line states where the units are still held and not yet handed over.
OPEN_LINE_STATUSES = frozenset({
    LineFulfillmentStatus.ORDERED_FOR_PICKUP,
    LineFulfillmentStatus.ORDERED_FOR_DELIVERY,
    LineFulfillmentStatus.BACKORDERED,
    LineFulfillmentStatus.READY_FOR_PICKUP,
    LineFulfillmentStatus.ON_ROUTE,
})

#: Line states meaning the customer has the goods.
SETTLED_LINE_STATUSES = frozenset({
    LineFulfillmentStatus.DELIVERED,
    LineFulfillmentStatus.COMPLETE,
})


# ---------------------------------------------------------------------------
# Status rollup
# ---------------------------------------------------------------------------


def recalculate_status(db: Session, order: Order) -> str:
    """Derive the order status from its lines (§9).

    Computed, never assigned: with split and mixed fulfilment, the only honest
    order-level status is one that follows from what the lines actually say.
    """
    lines = active_lines(db, order)
    if not lines:
        return order.status

    statuses = {line.fulfillment_status for line in lines}

    if statuses <= {LineFulfillmentStatus.CANCELLED}:
        order.status = OrderStatus.CANCELLED
        order.cancelled_dt = order.cancelled_dt or utcnow()
    elif statuses <= (SETTLED_LINE_STATUSES | {LineFulfillmentStatus.CANCELLED}):
        order.status = OrderStatus.COMPLETE
        order.completed_dt = order.completed_dt or utcnow()
    elif statuses & (SETTLED_LINE_STATUSES | {LineFulfillmentStatus.CANCELLED}):
        # Some settled, some not — exactly the split-fulfilment case §9 requires.
        order.status = OrderStatus.PARTIALLY_FULFILLED
    elif statuses & {
        LineFulfillmentStatus.READY_FOR_PICKUP,
        LineFulfillmentStatus.ON_ROUTE,
    }:
        order.status = OrderStatus.IN_PREPARATION
    else:
        order.status = OrderStatus.PLACED

    return order.status


def active_lines(db: Session, order: Order) -> list[OrderLine]:
    return list(
        db.scalars(
            select(OrderLine)
            .where(
                OrderLine.fk_order_id == order.pk_order_id,
                OrderLine.scd_active_flag.is_(True),
            )
            .order_by(OrderLine.pk_order_line_id)
        ).all()
    )


def recalculate_totals(db: Session, order: Order) -> None:
    """Re-derive the order money from its lines plus order-level adjustments.

    Called after any line edit or discount. The promocode discount is *not*
    recomputed — it was validated and frozen at checkout, and silently changing
    it because staff edited a line would be a surprise the customer never
    agreed to.
    """
    lines = [
        line
        for line in active_lines(db, order)
        if line.fulfillment_status != LineFulfillmentStatus.CANCELLED
    ]

    subtotal = q(sum((Decimal(line.line_total_amt) for line in lines), Decimal("0")))
    item_discount = q(
        sum(
            (
                (Decimal(line.list_price_amt) - Decimal(line.unit_price_amt))
                * line.quantity
                + Decimal(line.manual_discount_amt)
                for line in lines
            ),
            Decimal("0"),
        )
    )

    order.subtotal_amt = subtotal
    order.item_discount_amt = item_discount

    invoice_discount = Decimal(order.invoice_discount_amt or 0)
    if order.invoice_discount_percentage:
        invoice_discount = q(
            subtotal * Decimal(order.invoice_discount_percentage) / Decimal(100)
        )
        order.invoice_discount_amt = invoice_discount

    order.total_amt = q(
        subtotal
        - invoice_discount
        - Decimal(order.promocode_discount_amt or 0)
        + Decimal(order.shipping_amt or 0)
    )
    money.refresh_payment_status(db, order)


# ---------------------------------------------------------------------------
# Preparation and fulfilment
# ---------------------------------------------------------------------------


def mark_prepared(
    db: Session, order: Order, staff: User, *, note: str | None = None
) -> Order:
    """Record who packed the order (§9).

    ``prepared_by_user_id`` is an immutable historical reference, not a live FK:
    the staff account may later be deactivated without breaking this record
    (§2.2).
    """
    order.prepared_by_user_id = staff.pk_user_id
    order.prepared_dt = utcnow()
    if note:
        order.internal_note = f"{order.internal_note}\n{note}" if order.internal_note else note

    for line in active_lines(db, order):
        if line.fulfillment_status in {
            LineFulfillmentStatus.ORDERED_FOR_PICKUP,
            LineFulfillmentStatus.ORDERED_FOR_DELIVERY,
        }:
            line.fulfillment_status = (
                LineFulfillmentStatus.READY_FOR_PICKUP
                if line.fulfillment_method == FulfillmentMethod.PICKUP
                else LineFulfillmentStatus.ON_ROUTE
            )

    recalculate_status(db, order)
    log.info(
        "order_prepared",
        extra={"order": order.order_number, "staff": staff.pk_user_id},
    )
    return order


def set_line_status(
    db: Session,
    order: Order,
    line: OrderLine,
    new_status: str,
    *,
    staff: User | None = None,
) -> OrderLine:
    """Move one line along the pipeline (§9).

    Hand-over is the transition that matters: moving into DELIVERED is what
    finally converts a hold into a deduction, so it routes through
    :func:`_hand_over` rather than just changing a label.
    """
    if line.fk_order_id != order.pk_order_id:
        raise NotFound("That line does not belong to this order.")
    if new_status == line.fulfillment_status:
        return line

    if new_status in SETTLED_LINE_STATUSES and line.stock_held_flag:
        _hand_over(db, order, line, staff=staff)

    if new_status == LineFulfillmentStatus.CANCELLED and line.stock_held_flag:
        _release_line(db, order, line, staff=staff)

    line.fulfillment_status = new_status
    if new_status in SETTLED_LINE_STATUSES:
        line.quantity_fulfilled = line.quantity

    recalculate_status(db, order)
    return line


def _hand_over(
    db: Session, order: Order, line: OrderLine, *, staff: User | None
) -> None:
    """Convert a hold into a deduction — the moment goods leave the shelf (§8).

    Writes two ledger rows: the reservation is released, and the units leave
    on-hand as a SALE. Both are needed, because the two projections are summed
    from different movement-kind sets.
    """
    levels = locking.lock_stock_levels(db, [line.fk_product_variant_id])
    locking.release(levels, variant_id=line.fk_product_variant_id, quantity=line.quantity)

    now = utcnow()
    remaining = line.quantity
    for level in levels:
        if remaining <= 0:
            break
        if level.fk_product_variant_id != line.fk_product_variant_id:
            continue
        take = min(level.quantity_on_hand, remaining)
        if take <= 0:
            continue
        level.quantity_on_hand -= take
        level.last_movement_dt = now
        remaining -= take

        db.add(
            StockMovement(
                fk_product_variant_id=line.fk_product_variant_id,
                fk_stock_pool_id=level.fk_stock_pool_id,
                movement_kind=MovementKind.RESERVATION_RELEASE,
                quantity_delta=-take,
                fk_order_line_id=line.pk_order_line_id,
                note=f"Hand-over for order {order.order_number}",
                created_dt=now,
                created_by=staff.pk_user_id if staff else None,
            )
        )
        db.add(
            StockMovement(
                fk_product_variant_id=line.fk_product_variant_id,
                fk_stock_pool_id=level.fk_stock_pool_id,
                movement_kind=MovementKind.SALE,
                quantity_delta=-take,
                unit_cost_amt=line.unit_cost_amt,
                fk_order_line_id=line.pk_order_line_id,
                note=f"Sold on order {order.order_number}",
                created_dt=now,
                created_by=staff.pk_user_id if staff else None,
            )
        )

    line.stock_held_flag = False

    # A consigned unit produces its revenue split at the moment it is sold
    # (Part I §7). A no-op for ordinary owned stock.
    from app.services.consignment import record_sale_for_order_line

    record_sale_for_order_line(
        db, line, actor_user_id=staff.pk_user_id if staff else None
    )

    # Purchase count is a running counter for cheap sorting; the movements above
    # remain the auditable detail behind it.
    variant = db.get(ProductVariant, line.fk_product_variant_id)
    if variant is not None:
        product = db.get(Product, variant.fk_product_id)
        if product is not None:
            product.purchase_count += line.quantity


def _release_line(
    db: Session, order: Order, line: OrderLine, *, staff: User | None
) -> None:
    """Return a held line's units to sellable stock."""
    levels = locking.lock_stock_levels(db, [line.fk_product_variant_id])
    outstanding = line.quantity - line.quantity_fulfilled
    if outstanding <= 0:
        line.stock_held_flag = False
        return

    locking.release(
        levels, variant_id=line.fk_product_variant_id, quantity=outstanding
    )

    now = utcnow()
    for level in levels:
        if level.fk_product_variant_id != line.fk_product_variant_id:
            continue
        db.add(
            StockMovement(
                fk_product_variant_id=line.fk_product_variant_id,
                fk_stock_pool_id=level.fk_stock_pool_id,
                movement_kind=MovementKind.RESERVATION_RELEASE,
                quantity_delta=-outstanding,
                fk_order_line_id=line.pk_order_line_id,
                note=f"Released from order {order.order_number}",
                created_dt=now,
                created_by=staff.pk_user_id if staff else None,
            )
        )
        break

    line.stock_held_flag = False


def mark_delivered(db: Session, order: Order, staff: User | None = None) -> Order:
    """Hand the whole order over at once — the common counter case."""
    for line in active_lines(db, order):
        if line.fulfillment_status != LineFulfillmentStatus.CANCELLED:
            set_line_status(db, order, line, LineFulfillmentStatus.DELIVERED, staff=staff)
    recalculate_status(db, order)
    return order


# ---------------------------------------------------------------------------
# Adjustments (Part I §9)
# ---------------------------------------------------------------------------


def apply_line_discount(
    db: Session,
    order: Order,
    line: OrderLine,
    *,
    percentage: Decimal | None = None,
    fixed_price_amt: Decimal | None = None,
    actor_user_id: int | None = None,
) -> OrderLine:
    """Per-item discount at fulfilment: a percentage, or a one-off price (§9)."""
    _assert_adjustable(order)

    original_unit = Decimal(line.unit_price_amt)

    if fixed_price_amt is not None:
        new_unit = q(Decimal(fixed_price_amt))
    elif percentage is not None:
        new_unit = q(original_unit * (Decimal(100) - Decimal(percentage)) / Decimal(100))
    else:
        raise ValidationFailed("Give either a percentage or a one-off price.")

    if new_unit < 0:
        raise ValidationFailed("A discount cannot make the price negative.")

    line.manual_discount_amt = q(
        Decimal(line.manual_discount_amt or 0) + (original_unit - new_unit) * line.quantity
    )
    line.unit_price_amt = new_unit
    line.line_total_amt = q(new_unit * line.quantity)

    recalculate_totals(db, order)
    return line


def apply_invoice_discount(
    db: Session,
    order: Order,
    *,
    percentage: Decimal | None = None,
    amount_amt: Decimal | None = None,
    actor_user_id: int | None = None,
) -> Order:
    """Whole-invoice discount: a percentage or a flat amount (§9)."""
    _assert_adjustable(order)

    if percentage is not None:
        order.invoice_discount_percentage = q(Decimal(percentage))
        order.invoice_discount_amt = Decimal("0")  # recomputed from the percentage
    elif amount_amt is not None:
        order.invoice_discount_percentage = None
        order.invoice_discount_amt = q(Decimal(amount_amt))
    else:
        raise ValidationFailed("Give either a percentage or a flat amount.")

    recalculate_totals(db, order)
    if order.total_amt < 0:
        raise ValidationFailed("That discount is larger than the order total.")
    return order


def set_shipping_cost(
    db: Session, order: Order, amount_amt: Decimal, *, actor_user_id: int | None = None
) -> Order:
    """Price shipping on the order (§9).

    Also clears ``shipping_quote_pending_flag`` — quoting the cost is exactly
    what "not included, will be contacted" was waiting for (§2.2).
    """
    order.shipping_amt = q(Decimal(amount_amt))
    order.shipping_quote_pending_flag = False
    recalculate_totals(db, order)
    return order


def update_line_quantity(
    db: Session,
    order: Order,
    line: OrderLine,
    new_quantity: int,
    *,
    staff: User | None = None,
) -> OrderLine:
    """Change a line's quantity on a placed order (§9).

    Per §9's decision, this does **not** re-trigger stock holds on the customer
    side or require them to reconfirm — but the store's own reservation must
    still track reality, so the difference is quietly reserved or released here.
    """
    _assert_adjustable(order)
    if new_quantity < 0:
        raise ValidationFailed("Quantity cannot be negative.")

    if new_quantity == 0:
        return set_line_status(
            db, order, line, LineFulfillmentStatus.CANCELLED, staff=staff
        )

    delta = new_quantity - line.quantity
    if delta != 0 and line.stock_held_flag:
        levels = locking.lock_stock_levels(db, [line.fk_product_variant_id])
        now = utcnow()

        if delta > 0:
            locking.reserve(
                db, levels, variant_id=line.fk_product_variant_id, quantity=delta
            )
        else:
            locking.release(
                levels, variant_id=line.fk_product_variant_id, quantity=-delta
            )

        for level in levels:
            if level.fk_product_variant_id != line.fk_product_variant_id:
                continue
            db.add(
                StockMovement(
                    fk_product_variant_id=line.fk_product_variant_id,
                    fk_stock_pool_id=level.fk_stock_pool_id,
                    movement_kind=(
                        MovementKind.RESERVATION_HOLD
                        if delta > 0
                        else MovementKind.RESERVATION_RELEASE
                    ),
                    quantity_delta=delta,
                    fk_order_line_id=line.pk_order_line_id,
                    note=f"Line adjusted on order {order.order_number}",
                    created_dt=now,
                    created_by=staff.pk_user_id if staff else None,
                )
            )
            break

    line.quantity = new_quantity
    line.line_total_amt = q(Decimal(line.unit_price_amt) * new_quantity)
    recalculate_totals(db, order)
    return line


def add_internal_note(db: Session, order: Order, note: str, staff: User) -> Order:
    """Staff-only note, never rendered on the customer's order page (§9)."""
    stamp = f"[{utcnow():%Y-%m-%d %H:%M} {staff.username}] {note}"
    order.internal_note = f"{order.internal_note}\n{stamp}" if order.internal_note else stamp
    return order


def _assert_adjustable(order: Order) -> None:
    """Staff may modify an order after placement but *before* fulfilment (§9)."""
    if order.status == OrderStatus.CANCELLED:
        raise Conflict("This order has been cancelled.")
    if order.status == OrderStatus.COMPLETE:
        raise Conflict("This order is already complete and cannot be adjusted.")


# ---------------------------------------------------------------------------
# Cancellation (Part I §8)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefundInstruction:
    """Where cancellation money goes — the Admin is prompted for this (§8)."""

    to_store_credit: bool = False
    money_box_id: int | None = None


def cancel_order(
    db: Session,
    order: Order,
    *,
    reason: str | None = None,
    cancelled_by: User | None = None,
    refund: RefundInstruction | None = None,
) -> Order:
    """Cancel an order: release stock, return the promocode use, refund money.

    §8 gives cancellation-after-payment its own money path, distinct from a
    post-delivery return, and requires the system to *prompt* for where the
    money goes rather than assuming. If anything was paid and no
    ``RefundInstruction`` is given, this refuses rather than guessing.
    """
    if order.status == OrderStatus.CANCELLED:
        raise Conflict("This order is already cancelled.")
    if order.status == OrderStatus.COMPLETE:
        raise Conflict("A completed order is returned, not cancelled.")

    paid = money.order_paid_amount(db, order)
    if paid > 0 and refund is None:
        raise ValidationFailed(
            "This order has been paid. Choose where the refund should go.",
            details={"paid_amt": str(paid)},
        )

    for line in active_lines(db, order):
        if line.fulfillment_status != LineFulfillmentStatus.CANCELLED:
            set_line_status(
                db, order, line, LineFulfillmentStatus.CANCELLED, staff=cancelled_by
            )

    if order.fk_promocode_id:
        _reverse_promocode(db, order, cancelled_by)

    if paid > 0 and refund is not None:
        _refund_cancellation(db, order, paid, refund, cancelled_by)

    order.status = OrderStatus.CANCELLED
    order.cancelled_dt = utcnow()
    order.cancellation_reason = reason
    order.cancelled_by_user_id = cancelled_by.pk_user_id if cancelled_by else None

    log.info(
        "order_cancelled",
        extra={"order": order.order_number, "refunded_amt": str(paid)},
    )
    return order


def _reverse_promocode(db: Session, order: Order, actor: User | None) -> None:
    """Give the customer their promocode use back (§13)."""
    redemptions = db.scalars(
        select(PromocodeRedemption).where(
            PromocodeRedemption.fk_order_id == order.pk_order_id,
            PromocodeRedemption.reverses_redemption_id.is_(None),
        )
    ).all()
    for redemption in redemptions:
        promocodes.reverse_redemption(
            db, redemption, actor_user_id=actor.pk_user_id if actor else None
        )


def _refund_cancellation(
    db: Session,
    order: Order,
    paid: Decimal,
    refund: RefundInstruction,
    actor: User | None,
) -> None:
    actor_id = actor.pk_user_id if actor else None

    if refund.to_store_credit:
        money.grant_store_credit(
            db,
            user_id=order.fk_user_id,
            amount_amt=paid,
            reason_code=MoneyReason.CANCELLATION_REFUND,
            order_id=order.pk_order_id,
            note=f"Cancellation of order {order.order_number}",
            actor_user_id=actor_id,
        )
    elif refund.money_box_id is not None:
        money.record_transaction(
            db,
            direction=MoneyDirection.OUT,
            reason_code=MoneyReason.CANCELLATION_REFUND,
            allocations=[(refund.money_box_id, -paid)],
            order_id=order.pk_order_id,
            description=f"Refund for cancelled order {order.order_number}",
            actor_user_id=actor_id,
        )
    else:
        raise ValidationFailed("Choose a money box or store credit for the refund.")

    # A negative payment row, so the order's paid total nets to zero.
    from app.models.orders import Payment

    original = db.scalars(
        select(Payment)
        .where(Payment.fk_order_id == order.pk_order_id)
        .order_by(Payment.pk_payment_id)
    ).first()
    db.add(
        Payment(
            fk_order_id=order.pk_order_id,
            fk_payment_channel_id=money.refund_channel_id(
                db,
                destination=(
                    RefundDestination.STORE_CREDIT
                    if refund.to_store_credit
                    else RefundDestination.MONEY_BOX
                ),
                fallback_channel_id=(
                    original.fk_payment_channel_id if original else None
                ),
            ),
            amount_amt=-paid,
            usd_rate_used=order.usd_rate_at_sale,
            note=f"Cancellation refund for {order.order_number}",
            created_dt=utcnow(),
            created_by=actor_id,
        )
    )
    db.flush()
    money.refresh_payment_status(db, order)
    order.payment_status = PaymentStatus.REFUNDED


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def search_orders(
    db: Session,
    *,
    status: str | None = None,
    payment: str | None = None,
    shipping: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[Order], int]:
    """The admin order list, filtered and paginated (Part II §2)."""
    stmt = select(Order).where(Order.scd_active_flag.is_(True))

    if status == "open":
        stmt = stmt.where(
            Order.status.in_([OrderStatus.PLACED, OrderStatus.IN_PREPARATION])
        )
    elif status:
        stmt = stmt.where(Order.status == status)

    if payment == "unpaid":
        stmt = stmt.where(
            Order.payment_status.in_(
                [PaymentStatus.NOT_PAID, PaymentStatus.PARTIALLY_PAID]
            ),
            Order.status != OrderStatus.CANCELLED,
        )
    elif payment:
        stmt = stmt.where(Order.payment_status == payment)

    if shipping == "pending":
        stmt = stmt.where(Order.shipping_quote_pending_flag.is_(True))

    if query:
        stmt = stmt.where(Order.order_number.ilike(f"%{query.strip()}%"))

    total = db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0

    page = max(page, 1)
    rows = db.scalars(
        stmt.order_by(Order.placed_dt.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def queue_status_email(db: Session, order: Order, status_label: str) -> None:
    """Tell the customer their order moved (§2.7)."""
    from app.services.email import queue_template_email

    customer = db.get(User, order.fk_user_id)
    if customer is None:
        return

    queue_template_email(
        db,
        EmailTemplateCode.ORDER_STATUS_CHANGE,
        recipient=customer.email,
        language=customer.preferred_language,
        # Keyed on order + status, so re-saving the same status cannot spam.
        idempotency_key=f"order_status:{order.pk_order_id}:{status_label}",
        params={"order_number": order.order_number, "status": status_label},
    )
