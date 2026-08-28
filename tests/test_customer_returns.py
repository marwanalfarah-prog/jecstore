"""Customer-initiated returns (Part I §12, §2.6).

Staff-raised returns are covered in :mod:`tests.test_returns`. What is new here
is that the *customer* can start one, which introduces three ways to get it
wrong:

* a customer reaching an order that is not theirs,
* a customer returning goods that have not been delivered yet,
* a customer skipping the inspection gate, or unpicking its verdict.

Each has a test below.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.base import utcnow
from app.models.enums import RefundDestination, ReturnStatus
from app.models.identity import User
from app.models.orders import Order
from app.services import money, orders, returns
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from app.web.customer_returns import FORM_ERRORS, _own_order
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


def _placed_order(db: Session, shop: dict, quantity: int = 3) -> Order:
    """Paid for, but nothing handed over yet."""
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
    db.commit()
    return order


def _delivered_order(db: Session, shop: dict, quantity: int = 3) -> Order:
    order = _placed_order(db, shop, quantity)
    orders.mark_delivered(db, order, shop["user"])
    db.commit()
    return order


def _customer_return(db: Session, order: Order, user: User, qty: int, reason="changed_mind"):
    """What the router does, minus the HTTP."""
    line = orders.active_lines(db, order)[0]
    order_return = returns.request_return(
        db, order,
        [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=qty)],
        reason_code=reason,
        requested_by=user,
        delivered_only=True,
    )
    db.commit()
    return order_return


# ---------------------------------------------------------------------------
# Scoping: an order number is not a credential (Part I §2.6)
# ---------------------------------------------------------------------------


def test_a_customer_reaches_their_own_order(db: Session, shop: dict):
    order = _delivered_order(db, shop)
    found = _own_order(db, order.order_number, shop["user"])
    assert found.pk_order_id == order.pk_order_id


def test_another_customers_order_is_simply_not_found(db: Session, shop: dict):
    """Not "forbidden" — that would confirm the order number is real."""
    order = _delivered_order(db, shop)

    stranger = User(
        fk_role_id=shop["user"].fk_role_id,
        username="stranger",
        email="stranger@example.com",
        password_hash="x",
        scd_active_from=utcnow(),
    )
    db.add(stranger)
    db.commit()

    with pytest.raises(NotFound):
        _own_order(db, order.order_number, stranger)


def test_a_guessed_order_number_is_not_found(db: Session, shop: dict):
    with pytest.raises(NotFound):
        _own_order(db, "JEC-000000-000", shop["user"])


# ---------------------------------------------------------------------------
# Only delivered goods (Part I §12)
# ---------------------------------------------------------------------------


def test_a_customer_cannot_return_goods_that_have_not_arrived(db: Session, shop: dict):
    """The order is paid for but not fulfilled: there is nothing to send back."""
    order = _placed_order(db, shop, quantity=2)

    assert returns.returnable_lines(db, order, delivered_only=True) == []

    line = orders.active_lines(db, order)[0]
    with pytest.raises(ValidationFailed):
        returns.request_return(
            db, order,
            [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=1)],
            reason_code="changed_mind",
            delivered_only=True,
        )


def test_staff_keep_the_fallback_to_the_ordered_quantity(db: Session, shop: dict):
    """A counter sale is handed over without passing through fulfilment
    bookkeeping, so the staff-facing path must still allow the return."""
    order = _placed_order(db, shop, quantity=2)

    assert returns.returnable_lines(db, order)[0][1] == 2


def test_delivered_goods_become_returnable(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    assert returns.returnable_lines(db, order, delivered_only=True)[0][1] == 3


def test_partial_delivery_limits_what_is_returnable(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    line = orders.active_lines(db, order)[0]
    line.quantity_fulfilled = 1
    db.commit()

    assert returns.returnable_lines(db, order, delivered_only=True)[0][1] == 1


# ---------------------------------------------------------------------------
# The inspection gate still applies (Part I §12)
# ---------------------------------------------------------------------------


def test_a_customer_raised_return_still_waits_for_inspection(db: Session, shop: dict):
    """Otherwise a customer could approve their own refund."""
    order = _delivered_order(db, shop, quantity=2)
    order_return = _customer_return(db, order, shop["user"], 1)

    assert order_return.status == ReturnStatus.REQUESTED
    with pytest.raises(Conflict):
        returns.finalise_refund(
            db, order_return,
            destination=RefundDestination.MONEY_BOX,
            money_box_id=shop["box"].pk_money_box_id,
        )


def test_it_produces_the_same_record_staff_would_have_raised(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=2)
    order_return = _customer_return(db, order, shop["user"], 1, reason="damaged")

    # Same numbering, same reason coding, same per-line restock presumption.
    assert order_return.return_number.startswith("RET-")
    assert order_return.reason_code == "damaged"
    assert returns.lines_for(db, order_return)[0].restock_flag is False


# ---------------------------------------------------------------------------
# Withdrawal (Part I §12)
# ---------------------------------------------------------------------------


def test_a_customer_can_withdraw_before_inspection(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=2)
    order_return = _customer_return(db, order, shop["user"], 1)

    returns.withdraw(db, order_return, by_user=shop["user"])
    db.commit()

    assert order_return.status == ReturnStatus.WITHDRAWN


def test_withdrawing_frees_the_units_to_be_returned_again(db: Session, shop: dict):
    """A change of mind must not permanently consume the right to return."""
    order = _delivered_order(db, shop, quantity=3)
    order_return = _customer_return(db, order, shop["user"], 3)
    assert returns.returnable_lines(db, order, delivered_only=True) == []

    returns.withdraw(db, order_return, by_user=shop["user"])
    db.commit()

    assert returns.returnable_lines(db, order, delivered_only=True)[0][1] == 3


def test_withdrawal_is_refused_once_inspection_has_started(db: Session, shop: dict):
    """Otherwise a customer withdraws the moment a refusal looks likely, and
    raises it again."""
    order = _delivered_order(db, shop, quantity=2)
    order_return = _customer_return(db, order, shop["user"], 1)

    returns.begin_inspection(db, order_return, shop["user"])
    db.commit()

    with pytest.raises(Conflict):
        returns.withdraw(db, order_return, by_user=shop["user"])


def test_withdrawal_is_refused_after_a_refund(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=3)
    order_return = _customer_return(db, order, shop["user"], 1)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db, order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()

    with pytest.raises(Conflict):
        returns.withdraw(db, order_return, by_user=shop["user"])


def test_staff_cannot_inspect_a_withdrawn_return(db: Session, shop: dict):
    """There is nothing to inspect: the customer kept the item."""
    order = _delivered_order(db, shop, quantity=2)
    order_return = _customer_return(db, order, shop["user"], 1)
    returns.withdraw(db, order_return, by_user=shop["user"])
    db.commit()

    with pytest.raises(Conflict):
        returns.begin_inspection(db, order_return, shop["user"])
    with pytest.raises(Conflict):
        returns.record_inspection(
            db, order_return, shop["user"], condition_acceptable=True
        )


def test_a_withdrawn_return_leaves_the_staff_queue(db: Session, shop: dict):
    order = _delivered_order(db, shop, quantity=2)
    order_return = _customer_return(db, order, shop["user"], 1)

    open_before, _ = returns.search_returns(db, status="open")
    assert order_return.pk_order_return_id in {r.pk_order_return_id for r in open_before}

    returns.withdraw(db, order_return, by_user=shop["user"])
    db.commit()

    open_after, _ = returns.search_returns(db, status="open")
    assert order_return.pk_order_return_id not in {
        r.pk_order_return_id for r in open_after
    }


def test_a_rejected_return_is_distinguishable_from_a_withdrawn_one(
    db: Session, shop: dict
):
    """A customer who changed their mind must not be shown as refused."""
    order = _delivered_order(db, shop, quantity=3)

    withdrawn = _customer_return(db, order, shop["user"], 1)
    returns.withdraw(db, withdrawn, by_user=shop["user"])

    refused = _customer_return(db, order, shop["user"], 1)
    returns.record_inspection(
        db, refused, shop["user"], condition_acceptable=False, note="water damage"
    )
    db.commit()

    assert withdrawn.status == ReturnStatus.WITHDRAWN
    assert refused.status == ReturnStatus.REJECTED


def test_a_rejected_return_still_consumes_nothing(db: Session, shop: dict):
    """Rejection and withdrawal both release the units — the customer still has
    the item either way."""
    order = _delivered_order(db, shop, quantity=2)
    order_return = _customer_return(db, order, shop["user"], 2)
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=False)
    db.commit()

    assert returns.returnable_lines(db, order, delivered_only=True)[0][1] == 2


# ---------------------------------------------------------------------------
# Form feedback
# ---------------------------------------------------------------------------


def test_every_form_error_code_has_a_message_in_both_languages():
    """The codes come off the query string, so each must resolve to real text
    rather than leaking its key onto the page.

    Checked against each catalog directly rather than through ``translate``,
    which falls back to the other language and would hide a missing Arabic
    string behind the English one.
    """
    from app.core.i18n import catalog

    for code in FORM_ERRORS:
        key = f"returns.error_{code}"
        for language in ("ar", "en"):
            assert key in catalog(language), f"{key} missing in {language}"


def test_every_return_status_has_a_customer_facing_label():
    """Including WITHDRAWN — a status with no label renders as its own code."""
    from app.core.i18n import catalog

    for status in ReturnStatus:
        key = f"returns.status_{status.value}"
        for language in ("ar", "en"):
            assert key in catalog(language), f"{key} missing in {language}"
