"""Return actions registered with the maker-checker engine (Part I §12, §2.2.1).

Issuing a refund and issuing store credit both default to Maker-Checker in the
permission registry — they move money out of the business, which is exactly the
class of action §2.2.1 exists for. Inspection does not: recording what an item
looks like is an observation, not a disbursement.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.models.enums import RefundDestination
from app.models.identity import User
from app.models.orders import OrderReturn
from app.services import approvals, returns


def _return(db: Session, return_id: int) -> OrderReturn:
    row = db.get(OrderReturn, return_id)
    if row is None or not row.scd_active_flag:
        raise NotFound("That return does not exist.")
    return row


def _staff(db: Session, actor_user_id: int | None) -> User | None:
    return db.get(User, actor_user_id) if actor_user_id else None


@approvals.register("returns", "inspect")
def inspect_return(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Record the condition check that gates any refund (§12)."""
    order_return = _return(db, params["return_id"])
    staff = _staff(db, actor_user_id)

    restock: dict[int, bool] = {}
    for key, value in params.items():
        if key.startswith("restock_"):
            restock[int(key.removeprefix("restock_"))] = bool(value)

    returns.record_inspection(
        db,
        order_return,
        staff,
        condition_acceptable=bool(params["condition_acceptable"]),
        note=params.get("note"),
        restock_by_line=restock or None,
    )
    return order_return.status


@approvals.register("returns", "issue_refund")
def issue_refund(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Refund out of a money box — Admin specifies which (§12)."""
    order_return = _return(db, params["return_id"])
    returns.finalise_refund(
        db,
        order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=params["money_box_id"],
        staff=_staff(db, actor_user_id),
        amount_amt=(
            Decimal(params["amount_amt"]) if params.get("amount_amt") is not None else None
        ),
    )
    return order_return.refund_amt


@approvals.register("returns", "issue_store_credit")
def issue_store_credit(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Convert the refund to رصيد instead of cash (§12)."""
    order_return = _return(db, params["return_id"])
    returns.finalise_refund(
        db,
        order_return,
        destination=RefundDestination.STORE_CREDIT,
        staff=_staff(db, actor_user_id),
        amount_amt=(
            Decimal(params["amount_amt"]) if params.get("amount_amt") is not None else None
        ),
    )
    return order_return.refund_amt
