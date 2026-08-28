"""Returns (Part I §12, with the consignment reversal from §7).

The behaviours worth pinning: the inspection gate genuinely blocks refunds,
partial returns work at line level, damaged goods are written off rather than
silently restocked, and a retried refund cannot pay the customer twice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, ValidationFailed
from app.db.base import utcnow
from app.models.enums import (
    MovementKind,
    PaymentStatus,
    RefundDestination,
    ReturnStatus,
)
from app.models.inventory import StockLevel, StockMovement
from app.models.orders import Order, OrderReturn
from app.services import money, orders, returns
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


def _delivered_order(db: Session, shop: dict, quantity: int = 3) -> Order:
    """A paid, delivered order — the precondition for any return."""
    _cart(db, shop, user=shop["user"], quantity=quantity)
    order = place_order(
        db, _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=order.total_amt,
                     money_box_id=shop["box"].pk_money_box_id)],
    )
    orders.mark_delivered(db, order, shop["user"])
    db.commit()
    return order


def _request(db: Session, shop: dict, order: Order, qty: int, reason="changed_mind"):
    line = orders.active_lines(db, order)[0]
    order_return = returns.request_return(
        db, order,
        [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=qty)],
        reason_code=reason,
    )
    db.commit()
    return order_return


# ---------------------------------------------------------------------------
# Raising a return
# ---------------------------------------------------------------------------


def test_partial_return_is_the_ordinary_case(db: Session, shop: dict):
    """Return 1 of 3 units, at line level (Part I §12)."""
    order = _delivered_order(db, shop, quantity=3)
    order_return = _request(db, shop, order, 1)

    assert order_return.status == ReturnStatus.REQUESTED
    lines = returns.lines_for(db, order_return)
    assert len(lines) == 1 and lines[0].quantity == 1


def test_cannot_return_more_than_was_delivered(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=2)
    line = orders.active_lines(db, order)[0]

    with pytest.raises(ValidationFailed):
        returns.request_return(
            db, order,
            [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=5)],
            reason_code="damaged",
        )


def test_two_returns_cannot_together_exceed_the_order(db: Session, shop: dict):
    """The second request must account for what the first already claimed."""
    order = _delivered_order(db, shop, quantity=3)
    _request(db, shop, order, 2)

    line = orders.active_lines(db, order)[0]
    with pytest.raises(ValidationFailed):
        returns.request_return(
            db, order,
            [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=2)],
            reason_code="damaged",
        )


def test_a_return_reason_is_required(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=1)
    line = orders.active_lines(db, order)[0]
    with pytest.raises(ValidationFailed):
        returns.request_return(
            db, order,
            [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=1)],
            reason_code="not_a_real_reason",
        )


def test_refund_amount_respects_order_level_discounts(db: Session, shop: dict):
    """Refunding the full line price would hand back money that never arrived."""
    order = _delivered_order(db, shop, quantity=2)
    # 10% invoice discount applied before the return.
    order.invoice_discount_amt = Decimal(order.subtotal_amt) / 10
    orders.recalculate_totals(db, order)
    db.commit()

    order_return = _request(db, shop, order, 1)
    line = returns.lines_for(db, order_return)[0]

    # Unit was 20.000; the customer effectively paid 90% of it.
    assert line.refund_amt == Decimal("18.000")


# ---------------------------------------------------------------------------
# The inspection gate (Part I §12)
# ---------------------------------------------------------------------------


def test_refund_is_blocked_before_inspection(db: Session, shop: dict):
    """Not every return auto-refunds — the gate is real."""
    order = _delivered_order(db, shop, quantity=2)
    order_return = _request(db, shop, order, 1)

    with pytest.raises(Conflict):
        returns.finalise_refund(
            db, order_return,
            destination=RefundDestination.MONEY_BOX,
            money_box_id=shop["box"].pk_money_box_id,
        )


def test_failing_inspection_rejects_the_return(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=2)
    order_return = _request(db, shop, order, 1)

    returns.record_inspection(
        db, order_return, shop["user"],
        condition_acceptable=False, note="water damage",
    )
    db.commit()

    assert order_return.status == ReturnStatus.REJECTED
    with pytest.raises(Conflict):
        returns.finalise_refund(
            db, order_return,
            destination=RefundDestination.MONEY_BOX,
            money_box_id=shop["box"].pk_money_box_id,
        )


def test_passing_inspection_approves_but_moves_no_money(db: Session, shop: dict):
    """Approval and disbursement are separate steps (§12)."""
    order = _delivered_order(db, shop, quantity=2)
    before = money.box_balance(db, shop["box"].pk_money_box_id)

    order_return = _request(db, shop, order, 1)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    db.commit()

    assert order_return.status == ReturnStatus.APPROVED
    assert money.box_balance(db, shop["box"].pk_money_box_id) == before


# ---------------------------------------------------------------------------
# Effects on inventory and sales figures
# ---------------------------------------------------------------------------


def test_accepted_return_restocks_the_unit(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_on_hand == 0  # fixture seeds 3, all delivered

    order_return = _request(db, shop, order, 1)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()
    db.refresh(level)

    assert level.quantity_on_hand == 1, "the unit is back on the shelf"


def test_damaged_return_is_written_off_not_restocked(db: Session, shop: dict):
    """The unit came back but is unsellable — both facts belong in the ledger."""
    order = _delivered_order(db, shop, quantity=3)
    level = db.scalars(select(StockLevel)).one()

    order_return = _request(db, shop, order, 1, reason="damaged")
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()
    db.refresh(level)

    assert level.quantity_on_hand == 0, "a damaged unit does not become sellable"

    kinds = [m.movement_kind for m in db.scalars(select(StockMovement)).all()]
    assert MovementKind.RETURN_IN in kinds, "it physically came back"
    assert MovementKind.WRITE_OFF in kinds, "and then left as shrinkage"


def test_inspector_can_override_the_restock_decision(db: Session, shop: dict):
    """The inspector has the item in hand; their judgement beats the reason code."""
    order = _delivered_order(db, shop, quantity=3)
    order_return = _request(db, shop, order, 1, reason="damaged")
    line = returns.lines_for(db, order_return)[0]
    assert line.restock_flag is False

    returns.record_inspection(
        db, order_return, shop["user"], condition_acceptable=True,
        restock_by_line={line.pk_order_return_line_id: True},
    )
    db.commit()
    assert line.restock_flag is True


def test_return_decrements_the_purchase_count(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    db.refresh(shop["product"])
    after_sale = shop["product"].purchase_count

    order_return = _request(db, shop, order, 2)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()
    db.refresh(shop["product"])

    assert shop["product"].purchase_count == after_sale - 2


# ---------------------------------------------------------------------------
# The money (Part I §12)
# ---------------------------------------------------------------------------


def test_refund_out_of_a_money_box(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    before = money.box_balance(db, shop["box"].pk_money_box_id)

    order_return = _request(db, shop, order, 1)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()

    assert order_return.status == ReturnStatus.REFUNDED
    assert money.box_balance(db, shop["box"].pk_money_box_id) == before - order_return.refund_amt


def test_refund_converted_to_store_credit(db: Session, shop: dict):
    """The رصيد alternative to a cash refund (§12)."""
    order = _delivered_order(db, shop, quantity=3)
    till_before = money.box_balance(db, shop["box"].pk_money_box_id)

    order_return = _request(db, shop, order, 1)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return, destination=RefundDestination.STORE_CREDIT
    )
    db.commit()

    assert money.store_credit_balance(db, shop["user"].pk_user_id) == order_return.refund_amt
    assert money.box_balance(db, shop["box"].pk_money_box_id) == till_before, (
        "no cash left the till"
    )


def test_money_box_refund_requires_a_box(db: Session, shop: dict):
    """§12: Admin specifies which money box the refund comes out of."""
    order = _delivered_order(db, shop, quantity=2)
    order_return = _request(db, shop, order, 1)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)

    with pytest.raises(ValidationFailed):
        returns.finalise_refund(
            db, order_return, destination=RefundDestination.MONEY_BOX
        )


def test_refunding_twice_is_refused(db: Session, shop: dict):
    """A retried refund must not pay the customer twice."""
    order = _delivered_order(db, shop, quantity=3)
    order_return = _request(db, shop, order, 1)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()
    balance = money.box_balance(db, shop["box"].pk_money_box_id)

    with pytest.raises(Conflict):
        returns.finalise_refund(
            db, order_return,
            destination=RefundDestination.MONEY_BOX,
            money_box_id=shop["box"].pk_money_box_id,
        )
    assert money.box_balance(db, shop["box"].pk_money_box_id) == balance


def test_full_return_marks_the_order_refunded(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    line = orders.active_lines(db, order)[0]

    order_return = returns.request_return(
        db, order,
        [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=3)],
        reason_code="not_as_described",
    )
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()

    assert order.payment_status == PaymentStatus.REFUNDED


def test_returnable_lines_shrink_as_returns_are_raised(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    assert returns.returnable_lines(db, order)[0][1] == 3

    _request(db, shop, order, 1)
    assert returns.returnable_lines(db, order)[0][1] == 2
