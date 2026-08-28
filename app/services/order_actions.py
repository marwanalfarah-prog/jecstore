"""Order actions registered with the maker-checker engine (Part I §2.2.1, §9).

Every guarded order write is defined here as a handler taking only
``(db, params, actor_user_id)``. That constraint is the point: a handler must be
replayable from its stored parameters alone, because on a Maker-Checker grant it
will not run until a checker approves — possibly minutes later, in a different
request, after a restart.

Routes never call these directly. They go through
``approvals.execute_or_submit()``, which decides whether to run now or park.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.models.orders import Order, OrderLine
from app.services import approvals, money, orders


def _order(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or not order.scd_active_flag:
        raise NotFound("That order does not exist.")
    return order


def _line(db: Session, line_id: int) -> OrderLine:
    line = db.get(OrderLine, line_id)
    if line is None or not line.scd_active_flag:
        raise NotFound("That order line does not exist.")
    return line


def _staff(db: Session, actor_user_id: int | None):
    from app.models.identity import User

    return db.get(User, actor_user_id) if actor_user_id else None


# ---------------------------------------------------------------------------
# Discounts — Maker-Checker by default (Part I §2.2.1's worked example)
# ---------------------------------------------------------------------------


@approvals.register("orders", "apply_invoice_discount")
def apply_invoice_discount(db: Session, params: dict[str, Any], actor_user_id: int | None):
    order = _order(db, params["order_id"])
    orders.apply_invoice_discount(
        db,
        order,
        percentage=params.get("percentage"),
        amount_amt=params.get("amount_amt"),
        actor_user_id=actor_user_id,
    )
    return order.total_amt


@approvals.register("orders", "apply_item_discount")
def apply_item_discount(db: Session, params: dict[str, Any], actor_user_id: int | None):
    order = _order(db, params["order_id"])
    line = _line(db, params["line_id"])
    orders.apply_line_discount(
        db,
        order,
        line,
        percentage=params.get("percentage"),
        fixed_price_amt=params.get("fixed_price_amt"),
        actor_user_id=actor_user_id,
    )
    return line.line_total_amt


# ---------------------------------------------------------------------------
# Fulfilment — Single Approval by default
# ---------------------------------------------------------------------------


@approvals.register("orders", "prepare")
def prepare_order(db: Session, params: dict[str, Any], actor_user_id: int | None):
    order = _order(db, params["order_id"])
    staff = _staff(db, actor_user_id)
    orders.mark_prepared(db, order, staff, note=params.get("note"))
    orders.queue_status_email(db, order, "in_preparation")
    return order.status


@approvals.register("orders", "set_line_status")
def set_line_status(db: Session, params: dict[str, Any], actor_user_id: int | None):
    order = _order(db, params["order_id"])
    line = _line(db, params["line_id"])
    orders.set_line_status(
        db, order, line, params["status"], staff=_staff(db, actor_user_id)
    )
    orders.queue_status_email(db, order, params["status"])
    return line.fulfillment_status


@approvals.register("orders", "record_payment")
def record_payment(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Record one payment split. Multi-split payments submit one action each,
    so a checker approves each channel individually rather than in bulk."""
    order = _order(db, params["order_id"])
    money.record_order_payment(
        db,
        order,
        [
            money.Split(
                channel_id=params["channel_id"],
                amount_amt=Decimal(params["amount_amt"]),
                money_box_id=params.get("money_box_id"),
                reference=params.get("reference"),
            )
        ],
        actor_user_id=actor_user_id,
    )
    return order.payment_status


@approvals.register("orders", "change_shipping_cost")
def change_shipping_cost(db: Session, params: dict[str, Any], actor_user_id: int | None):
    order = _order(db, params["order_id"])
    orders.set_shipping_cost(
        db, order, Decimal(params["amount_amt"]), actor_user_id=actor_user_id
    )
    return order.shipping_amt


@approvals.register("orders", "edit_items")
def edit_items(db: Session, params: dict[str, Any], actor_user_id: int | None):
    order = _order(db, params["order_id"])
    line = _line(db, params["line_id"])
    orders.update_line_quantity(
        db, order, line, int(params["quantity"]), staff=_staff(db, actor_user_id)
    )
    return line.quantity


@approvals.register("orders", "cancel")
def cancel_order(db: Session, params: dict[str, Any], actor_user_id: int | None):
    order = _order(db, params["order_id"])
    refund = None
    if params.get("refund_to_store_credit") or params.get("refund_money_box_id"):
        refund = orders.RefundInstruction(
            to_store_credit=bool(params.get("refund_to_store_credit")),
            money_box_id=params.get("refund_money_box_id"),
        )
    orders.cancel_order(
        db,
        order,
        reason=params.get("reason"),
        cancelled_by=_staff(db, actor_user_id),
        refund=refund,
    )
    return order.status
