"""Returns (Part I §12, with the consignment reversal from §7).

The spec is emphatic that **not every return auto-refunds**: there is an
approval/condition-check step, and the item must be inspected as
returned-in-good-condition before any money moves. So the lifecycle here is a
gate, not a formality::

    REQUESTED → UNDER_INSPECTION → APPROVED → REFUNDED
                               └→ REJECTED

Money only moves on the last transition, and only once — ``effects_applied_flag``
makes a retried refund a no-op rather than paying the customer twice.

Returns are **partial by default**: a return is a set of line rows with their
own quantities, so returning 1 of 3 units is the ordinary case rather than a
special one (§12).

Effects that flow outward when a return is finalised:

* **Inventory** — units come back as ``RETURN_IN``. If they came back damaged
  they are then written off, so the ledger shows both what returned and why it
  is not sellable, rather than quietly swallowing the unit (§11, §12).
* **Sales figures** — the product's purchase count is decremented.
* **Consignment** — the original revenue split is *unwound* with a reversing
  row, not edited (§7).
* **Money** — out of a money box, or converted to رصيد (§12).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import Product, ProductVariant
from app.models.consignment import ConsignmentItem, ConsignmentSale
from app.models.enums import (
    EmailTemplateCode,
    MoneyDirection,
    MoneyReason,
    MovementKind,
    OrderStatus,
    PaymentStatus,
    RefundDestination,
    ReturnStatus,
    WriteOffReason,
)
from app.models.identity import User
from app.models.inventory import StockMovement
from app.models.orders import Order, OrderLine, OrderReturn, OrderReturnLine, Payment
from app.services import locking, money
from app.services.pricing import q

log = get_logger(__name__)

#: Reasons a customer gives. Free text lives in ``reason_detail``; this is the
#: coded part so returns can be reported on.
RETURN_REASONS: tuple[str, ...] = (
    "damaged",
    "wrong_item",
    "not_as_described",
    "changed_mind",
    "missing_parts",
    "other",
)

#: Reasons where the unit is presumed unsellable unless staff say otherwise.
UNSELLABLE_REASONS = frozenset({"damaged", "missing_parts"})

#: Terminal states in which a return lays no claim to the units it named.
#: Quantities on these rows must not count against what is still returnable,
#: or a withdrawn request would permanently consume the customer's right to
#: return the item.
VOID_STATUSES = frozenset({ReturnStatus.REJECTED, ReturnStatus.WITHDRAWN})


@dataclass(slots=True)
class ReturnLineRequest:
    """One line the customer wants to send back."""

    order_line_id: int
    quantity: int


# ---------------------------------------------------------------------------
# Raising a return
# ---------------------------------------------------------------------------


def request_return(
    db: Session,
    order: Order,
    lines: list[ReturnLineRequest],
    *,
    reason_code: str,
    reason_detail: str | None = None,
    requested_by: User | None = None,
    delivered_only: bool = False,
) -> OrderReturn:
    """Open a return against a delivered order.

    Validates quantities against what was actually fulfilled and not already
    returned — so a customer cannot return three of two units, and cannot return
    the same unit twice across separate requests.

    ``delivered_only`` must be passed for customer-raised returns; see
    :func:`_returnable_quantity`. The check lives here rather than only in the
    form that renders the options, because the form is a suggestion and the
    POST is the fact.
    """
    if reason_code not in RETURN_REASONS:
        raise ValidationFailed("Choose a return reason.")
    if not lines:
        raise ValidationFailed("Select at least one item to return.")
    if order.status == OrderStatus.CANCELLED:
        raise Conflict("A cancelled order has nothing to return.")

    now = utcnow()
    order_lines = {
        line.pk_order_line_id: line
        for line in db.scalars(
            select(OrderLine).where(
                OrderLine.fk_order_id == order.pk_order_id,
                OrderLine.scd_active_flag.is_(True),
            )
        ).all()
    }

    order_return = OrderReturn(
        return_number=_next_return_number(db, now),
        fk_order_id=order.pk_order_id,
        fk_user_id=order.fk_user_id,
        status=ReturnStatus.REQUESTED,
        reason_code=reason_code,
        reason_detail=reason_detail,
        scd_active_from=now,
    )
    db.add(order_return)
    db.flush()

    for requested in lines:
        if requested.quantity <= 0:
            continue

        order_line = order_lines.get(requested.order_line_id)
        if order_line is None:
            raise NotFound("That order line is not part of this order.")

        returnable = _returnable_quantity(db, order_line, delivered_only=delivered_only)
        if requested.quantity > returnable:
            raise ValidationFailed(
                "That is more than was delivered on this line.",
                details={
                    "order_line_id": order_line.pk_order_line_id,
                    "requested": requested.quantity,
                    "returnable": returnable,
                },
            )

        db.add(
            OrderReturnLine(
                fk_order_return_id=order_return.pk_order_return_id,
                fk_order_line_id=order_line.pk_order_line_id,
                quantity=requested.quantity,
                refund_amt=_line_refund_amount(order, order_line, requested.quantity),
                # Presumed unsellable for damage; the inspector can override.
                restock_flag=reason_code not in UNSELLABLE_REASONS,
                scd_active_from=now,
            )
        )

    db.flush()
    order_return.refund_amt = _total_refund(db, order_return)

    log.info(
        "return_requested",
        extra={
            "return": order_return.return_number,
            "order": order.order_number,
            "reason": reason_code,
        },
    )
    return order_return


def _returnable_quantity(
    db: Session, order_line: OrderLine, *, delivered_only: bool = False
) -> int:
    """Delivered units not already spoken for by another return.

    Counts across *all* live returns, so two half-returns cannot together
    exceed what was delivered.

    ``delivered_only`` drops the fallback to the ordered quantity. Staff need
    that fallback — a counter sale is handed over without ever passing through
    fulfilment bookkeeping — but a customer must not be able to open a return
    on goods that have not reached them yet.
    """
    already = db.scalar(
        select(func.coalesce(func.sum(OrderReturnLine.quantity), 0))
        .select_from(OrderReturnLine)
        .join(
            OrderReturn,
            OrderReturn.pk_order_return_id == OrderReturnLine.fk_order_return_id,
        )
        .where(
            OrderReturnLine.fk_order_line_id == order_line.pk_order_line_id,
            OrderReturnLine.scd_active_flag.is_(True),
            OrderReturn.scd_active_flag.is_(True),
            OrderReturn.status.not_in(list(VOID_STATUSES)),
        )
    ) or 0

    fulfilled = order_line.quantity_fulfilled or 0
    delivered = fulfilled if delivered_only else (fulfilled or order_line.quantity)
    return max(delivered - already, 0)


def _line_refund_amount(order: Order, order_line: OrderLine, quantity: int) -> Decimal:
    """What one returned unit is actually worth back.

    Not simply the unit price: order-level discounts (an invoice discount, a
    promocode) reduced what the customer paid, so refunding the full line price
    would hand back money that never arrived. The line's share is scaled by the
    ratio the order actually settled at, and shipping is excluded — the delivery
    happened whether or not the goods came back.
    """
    unit = Decimal(order_line.unit_price_amt)
    gross = q(unit * quantity)

    subtotal = Decimal(order.subtotal_amt or 0)
    if subtotal <= 0:
        return gross

    goods_paid = (
        subtotal
        - Decimal(order.invoice_discount_amt or 0)
        - Decimal(order.promocode_discount_amt or 0)
    )
    ratio = goods_paid / subtotal if subtotal else Decimal(1)
    return q(gross * ratio)


def _total_refund(db: Session, order_return: OrderReturn) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(OrderReturnLine.refund_amt), 0)).where(
            OrderReturnLine.fk_order_return_id == order_return.pk_order_return_id,
            OrderReturnLine.scd_active_flag.is_(True),
        )
    )
    return q(Decimal(total or 0))


def withdraw(db: Session, order_return: OrderReturn, *, by_user: User) -> OrderReturn:
    """The customer takes their request back.

    Only while it is still REQUESTED. Once staff have started inspecting, the
    outcome is theirs to record — otherwise a customer could withdraw a return
    the moment it was about to be refused and simply raise it again.

    The units this return named become returnable once more (see
    :data:`VOID_STATUSES`), so withdrawing costs the customer nothing.
    """
    if order_return.status != ReturnStatus.REQUESTED:
        raise Conflict("This return is already being processed.")

    order_return.status = ReturnStatus.WITHDRAWN
    order_return.scd_changed_by = by_user.pk_user_id

    log.info(
        "return_withdrawn",
        extra={"return": order_return.return_number, "user_id": by_user.pk_user_id},
    )
    return order_return


# ---------------------------------------------------------------------------
# The inspection gate (Part I §12)
# ---------------------------------------------------------------------------


def begin_inspection(db: Session, order_return: OrderReturn, staff: User) -> OrderReturn:
    if order_return.status not in {ReturnStatus.REQUESTED, ReturnStatus.UNDER_INSPECTION}:
        raise Conflict("This return has already been decided.")
    order_return.status = ReturnStatus.UNDER_INSPECTION
    order_return.inspected_by_user_id = staff.pk_user_id
    return order_return


def record_inspection(
    db: Session,
    order_return: OrderReturn,
    staff: User,
    *,
    condition_acceptable: bool,
    note: str | None = None,
    restock_by_line: dict[int, bool] | None = None,
) -> OrderReturn:
    """The condition check that gates any refund (§12).

    ``condition_acceptable=False`` rejects the return outright. Passing
    inspection only moves it to APPROVED — money still does not move until
    :func:`finalise_refund` is called with a destination, because §12 requires
    Admin to specify where the refund comes from.
    """
    if order_return.status == ReturnStatus.WITHDRAWN:
        raise Conflict("The customer withdrew this return.")
    if order_return.status in {ReturnStatus.REFUNDED, ReturnStatus.REJECTED}:
        raise Conflict("This return has already been decided.")

    now = utcnow()
    order_return.inspected_by_user_id = staff.pk_user_id
    order_return.inspected_dt = now
    order_return.inspection_note = note
    order_return.condition_acceptable_flag = condition_acceptable

    # The inspector has the item in hand, so their per-line judgement about
    # whether it can go back on the shelf overrides the reason-code guess.
    if restock_by_line:
        for line in _lines(db, order_return):
            if line.pk_order_return_line_id in restock_by_line:
                line.restock_flag = restock_by_line[line.pk_order_return_line_id]

    order_return.status = (
        ReturnStatus.APPROVED if condition_acceptable else ReturnStatus.REJECTED
    )

    log.info(
        "return_inspected",
        extra={
            "return": order_return.return_number,
            "acceptable": condition_acceptable,
            "status": order_return.status,
        },
    )
    return order_return


# ---------------------------------------------------------------------------
# Finalising: effects and money
# ---------------------------------------------------------------------------


def finalise_refund(
    db: Session,
    order_return: OrderReturn,
    *,
    destination: str,
    money_box_id: int | None = None,
    staff: User | None = None,
    amount_amt: Decimal | None = None,
) -> OrderReturn:
    """Apply the return's effects and move the money. Idempotent.

    ``effects_applied_flag`` is checked first: a retried request must not
    restock the same unit twice or refund the customer twice.
    """
    if order_return.status == ReturnStatus.REFUNDED:
        raise Conflict("This return has already been refunded.")
    if order_return.status != ReturnStatus.APPROVED:
        raise Conflict("This return must pass inspection before a refund is issued.")

    refund = q(Decimal(amount_amt if amount_amt is not None else order_return.refund_amt))
    if refund < 0:
        raise ValidationFailed("A refund cannot be negative.")

    if destination == RefundDestination.MONEY_BOX and money_box_id is None:
        raise ValidationFailed("Choose which money box the refund comes out of.")

    order = db.get(Order, order_return.fk_order_id)
    actor_id = staff.pk_user_id if staff else None

    if not order_return.effects_applied_flag:
        _apply_inventory_effects(db, order_return, order, staff=staff)
        _reverse_consignment_splits(db, order_return, order, actor_id=actor_id)
        _decrement_sales_figures(db, order_return)
        order_return.effects_applied_flag = True

    if refund > 0:
        _move_refund_money(
            db, order_return, order, refund,
            destination=destination, money_box_id=money_box_id, actor_id=actor_id,
        )

    order_return.refund_destination = destination
    order_return.fk_money_box_id = money_box_id
    order_return.refund_amt = refund
    order_return.refunded_dt = utcnow()
    order_return.status = ReturnStatus.REFUNDED

    _refresh_order_payment_status(db, order)
    _queue_return_email(db, order_return)

    log.info(
        "return_refunded",
        extra={
            "return": order_return.return_number,
            "destination": destination,
            "amount": str(refund),
        },
    )
    return order_return


def _apply_inventory_effects(
    db: Session, order_return: OrderReturn, order: Order, *, staff: User | None
) -> None:
    """Bring the units back, and write off the ones that came back unsellable."""
    now = utcnow()
    actor_id = staff.pk_user_id if staff else None

    for return_line in _lines(db, order_return):
        order_line = db.get(OrderLine, return_line.fk_order_line_id)
        if order_line is None:
            continue

        pool_id = order_line.fk_stock_pool_id
        if pool_id is None:
            levels = locking.lock_stock_levels(db, [order_line.fk_product_variant_id])
            pool_id = levels[0].fk_stock_pool_id if levels else None
        if pool_id is None:
            log.warning(
                "return_without_stock_pool",
                extra={"return": order_return.return_number},
            )
            continue

        levels = locking.lock_stock_levels(db, [order_line.fk_product_variant_id])
        level = next(
            (
                candidate
                for candidate in levels
                if candidate.fk_stock_pool_id == pool_id
                and candidate.fk_product_variant_id == order_line.fk_product_variant_id
            ),
            None,
        )

        # The unit physically came back, so it enters the ledger either way.
        if level is not None:
            level.quantity_on_hand += return_line.quantity
            level.last_movement_dt = now

        db.add(
            StockMovement(
                fk_product_variant_id=order_line.fk_product_variant_id,
                fk_stock_pool_id=pool_id,
                movement_kind=MovementKind.RETURN_IN,
                quantity_delta=return_line.quantity,
                unit_cost_amt=order_line.unit_cost_amt,
                fk_order_line_id=order_line.pk_order_line_id,
                note=f"Return {order_return.return_number}",
                created_dt=now,
                created_by=actor_id,
            )
        )

        if not return_line.restock_flag:
            # Came back damaged: it is on the premises but not sellable, so it
            # leaves again as a write-off rather than silently never arriving
            # (Part I §11's dedicated shrinkage movement type).
            if level is not None:
                level.quantity_on_hand -= return_line.quantity
            db.add(
                StockMovement(
                    fk_product_variant_id=order_line.fk_product_variant_id,
                    fk_stock_pool_id=pool_id,
                    movement_kind=MovementKind.WRITE_OFF,
                    quantity_delta=-return_line.quantity,
                    unit_cost_amt=order_line.unit_cost_amt,
                    fk_order_line_id=order_line.pk_order_line_id,
                    write_off_reason=WriteOffReason.DAMAGED,
                    note=f"Damaged on return {order_return.return_number}",
                    created_dt=now,
                    created_by=actor_id,
                )
            )

        order_line.quantity_returned = (order_line.quantity_returned or 0) + return_line.quantity


def _reverse_consignment_splits(
    db: Session, order_return: OrderReturn, order: Order, *, actor_id: int | None
) -> None:
    """Unwind the revenue split on a returned consigned item (Part I §7).

    A reversing row, never an edit: both the original split and its reversal
    stay visible, which is what makes a settlement statement auditable.
    """
    now = utcnow()

    for return_line in _lines(db, order_return):
        order_line = db.get(OrderLine, return_line.fk_order_line_id)
        if order_line is None or order_line.fk_consignment_item_id is None:
            continue

        original = db.scalars(
            select(ConsignmentSale)
            .where(
                ConsignmentSale.fk_order_line_id == order_line.pk_order_line_id,
                ConsignmentSale.reverses_consignment_sale_id.is_(None),
            )
            .order_by(ConsignmentSale.pk_consignment_sale_id)
        ).first()
        if original is None:
            continue

        share = (
            Decimal(return_line.quantity) / Decimal(original.quantity)
            if original.quantity
            else Decimal(0)
        )
        db.add(
            ConsignmentSale(
                fk_consignment_item_id=original.fk_consignment_item_id,
                fk_order_line_id=order_line.pk_order_line_id,
                quantity=-return_line.quantity,
                list_price_amt=original.list_price_amt,
                sold_price_amt=original.sold_price_amt,
                split_basis=original.split_basis,
                our_share_percentage=original.our_share_percentage,
                our_share_amt=q(-Decimal(original.our_share_amt) * share),
                their_share_amt=q(-Decimal(original.their_share_amt) * share),
                reverses_consignment_sale_id=original.pk_consignment_sale_id,
                created_dt=now,
                created_by=actor_id,
            )
        )

        # The units go back into the holding party's count.
        item = db.get(ConsignmentItem, original.fk_consignment_item_id)
        if item is not None:
            item.quantity_sold = max((item.quantity_sold or 0) - return_line.quantity, 0)


def _decrement_sales_figures(db: Session, order_return: OrderReturn) -> None:
    """A returned unit is not a sale, so the purchase count should not claim it."""
    for return_line in _lines(db, order_return):
        order_line = db.get(OrderLine, return_line.fk_order_line_id)
        if order_line is None:
            continue
        variant = db.get(ProductVariant, order_line.fk_product_variant_id)
        if variant is None:
            continue
        product = db.get(Product, variant.fk_product_id)
        if product is not None:
            product.purchase_count = max(product.purchase_count - return_line.quantity, 0)


def _move_refund_money(
    db: Session,
    order_return: OrderReturn,
    order: Order,
    refund: Decimal,
    *,
    destination: str,
    money_box_id: int | None,
    actor_id: int | None,
) -> None:
    """Cash out of a box, or convert to رصيد (Part I §12)."""
    if destination == RefundDestination.STORE_CREDIT:
        money.grant_store_credit(
            db,
            user_id=order_return.fk_user_id,
            amount_amt=refund,
            reason_code=MoneyReason.REFUND,
            order_id=order.pk_order_id if order else None,
            order_return_id=order_return.pk_order_return_id,
            note=f"Return {order_return.return_number}",
            actor_user_id=actor_id,
        )
    elif destination == RefundDestination.MONEY_BOX:
        money.record_transaction(
            db,
            direction=MoneyDirection.OUT,
            reason_code=MoneyReason.REFUND,
            allocations=[(money_box_id, -refund)],
            order_id=order.pk_order_id if order else None,
            order_return_id=order_return.pk_order_return_id,
            description=f"Refund for return {order_return.return_number}",
            actor_user_id=actor_id,
        )
    else:
        raise ValidationFailed("Choose a refund destination.")

    # A negative payment row so the order's paid total reflects the refund.
    if order is not None:
        original = db.scalars(
            select(Payment)
            .where(Payment.fk_order_id == order.pk_order_id, Payment.amount_amt > 0)
            .order_by(Payment.pk_payment_id)
        ).first()
        if original is not None:
            db.add(
                Payment(
                    fk_order_id=order.pk_order_id,
                    fk_payment_channel_id=money.refund_channel_id(
                        db,
                        destination=destination,
                        fallback_channel_id=original.fk_payment_channel_id,
                    ),
                    amount_amt=-refund,
                    usd_rate_used=order.usd_rate_at_sale,
                    note=f"Refund for return {order_return.return_number}",
                    created_dt=utcnow(),
                    created_by=actor_id,
                )
            )


def _refresh_order_payment_status(db: Session, order: Order | None) -> None:
    if order is None:
        return
    db.flush()
    paid = money.order_paid_amount(db, order)
    if paid <= 0 and Decimal(order.total_amt) > 0:
        order.payment_status = PaymentStatus.REFUNDED
    elif paid < Decimal(order.total_amt):
        order.payment_status = PaymentStatus.PARTIALLY_REFUNDED
    else:
        money.refresh_payment_status(db, order)


def _queue_return_email(db: Session, order_return: OrderReturn) -> None:
    from app.services.email import queue_template_email

    customer = db.get(User, order_return.fk_user_id)
    if customer is None:
        return
    queue_template_email(
        db,
        EmailTemplateCode.RETURN_PROCESSED,
        recipient=customer.email,
        language=customer.preferred_language,
        idempotency_key=f"return_processed:{order_return.pk_order_return_id}",
        params={"return_number": order_return.return_number},
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _lines(db: Session, order_return: OrderReturn) -> list[OrderReturnLine]:
    return list(
        db.scalars(
            select(OrderReturnLine)
            .where(
                OrderReturnLine.fk_order_return_id == order_return.pk_order_return_id,
                OrderReturnLine.scd_active_flag.is_(True),
            )
            .order_by(OrderReturnLine.pk_order_return_line_id)
        ).all()
    )


lines_for = _lines


def search_returns(
    db: Session,
    *,
    status: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[OrderReturn], int]:
    stmt = select(OrderReturn).where(OrderReturn.scd_active_flag.is_(True))

    if status == "open":
        stmt = stmt.where(
            OrderReturn.status.in_(
                [ReturnStatus.REQUESTED, ReturnStatus.UNDER_INSPECTION, ReturnStatus.APPROVED]
            )
        )
    elif status:
        stmt = stmt.where(OrderReturn.status == status)

    if query:
        stmt = stmt.where(OrderReturn.return_number.ilike(f"%{query.strip()}%"))

    total = db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0

    page = max(page, 1)
    rows = db.scalars(
        stmt.order_by(OrderReturn.pk_order_return_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def returnable_lines(
    db: Session, order: Order, *, delivered_only: bool = False
) -> list[tuple[OrderLine, int]]:
    """Order lines with how many units are still returnable."""
    result: list[tuple[OrderLine, int]] = []
    for line in db.scalars(
        select(OrderLine)
        .where(
            OrderLine.fk_order_id == order.pk_order_id,
            OrderLine.scd_active_flag.is_(True),
        )
        .order_by(OrderLine.pk_order_line_id)
    ).all():
        remaining = _returnable_quantity(db, line, delivered_only=delivered_only)
        if remaining > 0:
            result.append((line, remaining))
    return result


def _next_return_number(db: Session, now: dt.datetime) -> str:
    prefix = f"RET-{now:%y%m%d}"
    count = db.scalar(
        select(func.count())
        .select_from(OrderReturn)
        .where(OrderReturn.return_number.like(f"{prefix}-%"))
    ) or 0
    return f"{prefix}-{count + 1:03d}"
