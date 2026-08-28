"""Admin order management (Part I §9).

Every write goes through ``approvals.execute_or_submit()``, never directly to
the service — that is what guarantees a Maker-Checker action cannot execute
because one route forgot to check. When an action is parked, the route says so
rather than pretending it happened.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import LineFulfillmentStatus, OrderStatus, PaymentStatus
from app.models.identity import User
from app.models.money import MoneyBox
from app.models.orders import Order, OrderLine, Payment, PaymentChannel
from app.services import approvals, money, orders
from app.services import order_actions  # noqa: F401 - registers the handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, has_permission, require_permission

router = APIRouter(prefix="/orders")

PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@router.get("")
def order_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("orders", "view")),
    db: Session = Depends(get_db),
) -> Response:
    page = max(int(request.query_params.get("page", 1) or 1), 1)
    rows, total = orders.search_orders(
        db,
        status=request.query_params.get("status"),
        payment=request.query_params.get("payment"),
        shipping=request.query_params.get("shipping"),
        query=request.query_params.get("q"),
        page=page,
        page_size=PAGE_SIZE,
    )

    # Who the order is for, batched. The list showed only an order number, so
    # answering "has Maha's order been prepared?" meant opening rows one by one.
    customer_ids = {row.fk_user_id for row in rows if row.fk_user_id}
    customers = (
        {
            user.pk_user_id: user
            for user in db.scalars(
                select(User).where(User.pk_user_id.in_(customer_ids))
            ).all()
        }
        if customer_ids
        else {}
    )

    return templates.TemplateResponse(
        request,
        "admin/orders/list.html",
        admin_context(
            db,
            staff,
            orders=rows,
            customers=customers,
            total=total,
            page=page,
            per_page=PAGE_SIZE,
            total_pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
            statuses=list(OrderStatus),
            payment_statuses=list(PaymentStatus),
        ),
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/{order_id}")
def order_detail(
    request: Request,
    order_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("orders", "view")),
    db: Session = Depends(get_db),
) -> Response:
    order = _get(db, order_id)
    lines = orders.active_lines(db, order)

    return templates.TemplateResponse(
        request,
        "admin/orders/detail.html",
        admin_context(
            db,
            staff,
            order=order,
            lines=lines,
            customer=db.get(User, order.fk_user_id),
            # The screen showed `prepared_by_user_id` straight — staff read
            # "Prepared by 4" and had no way to turn 4 into a colleague.
            prepared_by=(
                db.get(User, order.prepared_by_user_id)
                if order.prepared_by_user_id
                else None
            ),
            can_raise_return=has_permission(db, staff, "returns", "view"),
            payments=_payments(db, order),
            paid_amt=money.order_paid_amount(db, order),
            outstanding_amt=money.outstanding_balance(db, order),
            channels=_channels(db),
            boxes=_boxes(db),
            line_statuses=list(LineFulfillmentStatus),
            flash=request.query_params.get("flash"),
        ),
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@router.post("/{order_id}/prepare")
def prepare(
    request: Request,
    order_id: int,
    note: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("orders", "prepare")),
    db: Session = Depends(get_db),
) -> Response:
    order = _get(db, order_id)
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="prepare",
        params={"order_id": order_id, "note": note},
        summary_en=f"Prepare order {order.order_number}",
        summary_ar=f"تجهيز الطلب {order.order_number}",
        target_table="scd_order", target_row_id=order_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/lines/{line_id}/status")
def line_status(
    request: Request,
    order_id: int,
    line_id: int,
    new_status: str = Form(...),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("orders", "set_line_status")),
    db: Session = Depends(get_db),
) -> Response:
    order = _get(db, order_id)
    if new_status not in set(LineFulfillmentStatus):
        raise ValidationFailed("Unknown fulfilment status.")

    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="set_line_status",
        params={"order_id": order_id, "line_id": line_id, "status": new_status},
        summary_en=f"Set line {line_id} on {order.order_number} to {new_status}",
        target_table="scd_order_line", target_row_id=line_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/deliver")
def deliver(
    request: Request,
    order_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("orders", "set_line_status")),
    db: Session = Depends(get_db),
) -> Response:
    """Hand the whole order over — the common counter case.

    Submits one action per line so each is independently auditable, and so a
    partially-approvable order behaves sensibly under Maker-Checker.
    """
    order = _get(db, order_id)
    pending = False
    for line in orders.active_lines(db, order):
        if line.fulfillment_status == LineFulfillmentStatus.CANCELLED:
            continue
        result = approvals.execute_or_submit(
            db, request, decision, staff,
            module="orders", action="set_line_status",
            params={
                "order_id": order_id,
                "line_id": line.pk_order_line_id,
                "status": LineFulfillmentStatus.DELIVERED,
            },
            summary_en=f"Deliver line {line.pk_order_line_id} on {order.order_number}",
            target_table="scd_order_line", target_row_id=line.pk_order_line_id,
        )
        pending = pending or result.pending
    db.commit()
    return _redirect(order_id, "pending" if pending else "saved")


@router.post("/{order_id}/invoice-discount")
def invoice_discount(
    request: Request,
    order_id: int,
    percentage: str | None = Form(None),
    amount_amt: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(
        require_permission("orders", "apply_invoice_discount")
    ),
    db: Session = Depends(get_db),
) -> Response:
    order = _get(db, order_id)
    pct, amt = _decimal(percentage), _decimal(amount_amt)
    if pct is None and amt is None:
        raise ValidationFailed("Give either a percentage or a flat amount.")

    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="apply_invoice_discount",
        params={"order_id": order_id, "percentage": pct, "amount_amt": amt},
        summary_en=(
            f"{pct}% off order {order.order_number}"
            if pct is not None
            else f"{amt} JOD off order {order.order_number}"
        ),
        target_table="scd_order", target_row_id=order_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/lines/{line_id}/discount")
def line_discount(
    request: Request,
    order_id: int,
    line_id: int,
    percentage: str | None = Form(None),
    fixed_price_amt: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(
        require_permission("orders", "apply_item_discount")
    ),
    db: Session = Depends(get_db),
) -> Response:
    order = _get(db, order_id)
    pct, price = _decimal(percentage), _decimal(fixed_price_amt)
    if pct is None and price is None:
        raise ValidationFailed("Give either a percentage or a one-off price.")

    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="apply_item_discount",
        params={
            "order_id": order_id, "line_id": line_id,
            "percentage": pct, "fixed_price_amt": price,
        },
        summary_en=f"Discount line {line_id} on {order.order_number}",
        target_table="scd_order_line", target_row_id=line_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/payment")
def add_payment(
    request: Request,
    order_id: int,
    channel_id: int = Form(...),
    amount_amt: str = Form(...),
    money_box_id: int | None = Form(None),
    reference: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("orders", "record_payment")),
    db: Session = Depends(get_db),
) -> Response:
    """Record one payment split. A split across channels is several submissions."""
    order = _get(db, order_id)
    amount = _decimal(amount_amt)
    if amount is None or amount == 0:
        raise ValidationFailed("Enter the amount received.")

    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="record_payment",
        params={
            "order_id": order_id, "channel_id": channel_id,
            "amount_amt": amount, "money_box_id": money_box_id,
            "reference": reference,
        },
        summary_en=f"Payment of {amount} on {order.order_number}",
        target_table="scd_order", target_row_id=order_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/shipping")
def shipping_cost(
    request: Request,
    order_id: int,
    amount_amt: str = Form(...),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(
        require_permission("orders", "change_shipping_cost")
    ),
    db: Session = Depends(get_db),
) -> Response:
    order = _get(db, order_id)
    amount = _decimal(amount_amt)
    if amount is None:
        raise ValidationFailed("Enter the shipping cost.")

    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="change_shipping_cost",
        params={"order_id": order_id, "amount_amt": amount},
        summary_en=f"Shipping {amount} JOD on {order.order_number}",
        target_table="scd_order", target_row_id=order_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/lines/{line_id}/quantity")
def line_quantity(
    request: Request,
    order_id: int,
    line_id: int,
    quantity: int = Form(...),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("orders", "edit_items")),
    db: Session = Depends(get_db),
) -> Response:
    order = _get(db, order_id)
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="edit_items",
        params={"order_id": order_id, "line_id": line_id, "quantity": quantity},
        summary_en=f"Set line {line_id} quantity to {quantity} on {order.order_number}",
        target_table="scd_order_line", target_row_id=line_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/cancel")
def cancel(
    request: Request,
    order_id: int,
    reason: str | None = Form(None),
    refund_destination: str | None = Form(None),
    refund_money_box_id: int | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("orders", "cancel")),
    db: Session = Depends(get_db),
) -> Response:
    """Cancel, prompting for where any refund goes (Part I §8)."""
    order = _get(db, order_id)
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="orders", action="cancel",
        params={
            "order_id": order_id,
            "reason": reason,
            "refund_to_store_credit": refund_destination == "store_credit",
            "refund_money_box_id": (
                refund_money_box_id if refund_destination == "money_box" else None
            ),
        },
        summary_en=f"Cancel order {order.order_number}",
        summary_ar=f"إلغاء الطلب {order.order_number}",
        target_table="scd_order", target_row_id=order_id,
    )
    db.commit()
    return _back(order_id, result)


@router.post("/{order_id}/note")
def internal_note(
    request: Request,
    order_id: int,
    note: str = Form(...),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("orders", "view")),
    db: Session = Depends(get_db),
) -> Response:
    """Staff-only note. Not guarded by maker-checker: a note changes nothing."""
    order = _get(db, order_id)
    orders.add_internal_note(db, order, note, staff)
    db.commit()
    return _redirect(order_id, "saved")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(db: Session, order_id: int) -> Order:
    order = db.scalars(
        select(Order).where(
            Order.pk_order_id == order_id, Order.scd_active_flag.is_(True)
        )
    ).first()
    if order is None:
        raise NotFound("That order does not exist.")
    return order


def _payments(db: Session, order: Order) -> list[tuple[Payment, PaymentChannel | None]]:
    rows = db.scalars(
        select(Payment)
        .where(Payment.fk_order_id == order.pk_order_id)
        .order_by(Payment.pk_payment_id)
    ).all()
    channels = {c.pk_payment_channel_id: c for c in _channels(db)}
    return [(p, channels.get(p.fk_payment_channel_id)) for p in rows]


def _channels(db: Session) -> list[PaymentChannel]:
    return list(
        db.scalars(
            select(PaymentChannel)
            .where(PaymentChannel.scd_active_flag.is_(True))
            .order_by(PaymentChannel.sort_order)
        ).all()
    )


def _boxes(db: Session) -> list[MoneyBox]:
    return list(
        db.scalars(
            select(MoneyBox).where(
                MoneyBox.scd_active_flag.is_(True), MoneyBox.is_open_flag.is_(True)
            )
        ).all()
    )


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid number.") from exc


def _back(order_id: int, result: approvals.ActionResult) -> RedirectResponse:
    """Post/Redirect/Get, telling the maker whether it ran or was parked."""
    return _redirect(order_id, "pending" if result.pending else "saved")


def _redirect(order_id: int, flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/orders/{order_id}?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
