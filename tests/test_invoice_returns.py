"""An invoice for an order that was partly returned (Part I §9, §12).

The bug this module pins was arithmetic, and it billed customers for goods they
had already sent back. `balance_amt` was `order.total_amt - paid_amt`, and a
refund writes a *negative* payment row. So the refund reduced what the customer
had paid without reducing what they owed, and it was counted twice:

    5 units at 10.000        total   50.000
    paid in cash             paid    50.000
    1 unit returned, 10.000  paid    40.000   ← the refund
                             balance 10.000   ← billed again for the return

An order settled in full printed a balance due for exactly the sum that had
just been handed back.

The fix keeps §9's founding rule — an invoice reprinted two years later shows
what the customer agreed to — so nothing is rewritten. The ordered quantities
and line prices stand, and the return is credited against them the way a credit
note works.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.enums import RefundDestination, ReturnStatus
from app.services import invoices, money, orders, returns
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


@pytest.fixture
def stocked(db: Session, shop: dict) -> dict:
    """The shared fixture stocks three units; these tests need more."""
    from sqlalchemy import select

    from app.models.inventory import StockLevel

    level = db.scalars(
        select(StockLevel).where(
            StockLevel.fk_product_variant_id == shop["variant"].pk_product_variant_id
        )
    ).one()
    level.quantity_on_hand = 50
    db.commit()
    return shop


@pytest.fixture
def paid_order(db: Session, stocked: dict):
    """Five units at 20.000 — 100.000 — paid in full in cash."""
    shop = stocked
    _cart(db, shop, user=shop["user"], quantity=5)
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


def _refund_one_unit(db: Session, shop: dict, order, *, destination: str,
                     quantity: int = 1):
    """Raise, inspect and settle a return for `quantity` units of line one."""
    line = orders.active_lines(db, order)[0]
    order_return = returns.request_return(
        db,
        order,
        [returns.ReturnLineRequest(
            order_line_id=line.pk_order_line_id, quantity=quantity
        )],
        reason_code="damaged",
        requested_by=shop["user"],
    )
    db.commit()

    returns.begin_inspection(db, order_return, shop["user"])
    returns.record_inspection(
        db, order_return, shop["user"], condition_acceptable=True
    )
    db.commit()

    returns.finalise_refund(
        db, order_return, staff=shop["user"], destination=destination,
        money_box_id=(
            shop["box"].pk_money_box_id
            if destination == RefundDestination.MONEY_BOX
            else None
        ),
    )
    db.commit()
    return order_return


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_a_settled_return_clears_the_balance_rather_than_creating_one(
    db: Session, shop: dict, paid_order
):
    """The headline bug: a fully settled order printed "Balance due 10.000"."""
    _refund_one_unit(db, shop, paid_order,
                     destination=RefundDestination.STORE_CREDIT)

    invoice = invoices.build(db, paid_order, language="en")

    assert invoice.order.total_amt == Decimal("100.000")
    assert invoice.returned_amt == Decimal("20.000")
    assert invoice.net_total_amt == Decimal("80.000")
    assert invoice.paid_amt == Decimal("80.000")
    assert invoice.balance_amt == Decimal("0.000"), (
        "the customer settled in full and returned a unit — they owe nothing"
    )


def test_the_original_lines_are_never_rewritten(db: Session, shop: dict, paid_order):
    """§9: an invoice reprinted later has to show what was actually agreed. The
    return is a credit against the line, not an edit of it."""
    _refund_one_unit(db, shop, paid_order,
                     destination=RefundDestination.STORE_CREDIT)

    line = invoices.build(db, paid_order, language="en").lines[0]
    assert line.quantity == 5, "the ordered quantity was rewritten"
    assert line.line_total_amt == Decimal("100.000")
    assert line.returned_quantity == 1
    assert line.kept_quantity == 4


def test_an_unsettled_return_credits_nothing(db: Session, shop: dict, paid_order):
    """§12 gates the refund on the condition check. A return still under
    inspection has moved no money and settled no goods, so crediting it would
    promise the customer something no member of staff has agreed to."""
    line = orders.active_lines(db, paid_order)[0]
    order_return = returns.request_return(
        db, paid_order,
        [returns.ReturnLineRequest(
            order_line_id=line.pk_order_line_id, quantity=2
        )],
        reason_code="damaged",
        requested_by=shop["user"],
    )
    db.commit()
    assert order_return.status == ReturnStatus.REQUESTED

    invoice = invoices.build(db, paid_order, language="en")
    assert invoice.returns == []
    assert invoice.returned_amt == Decimal("0")
    assert invoice.net_total_amt == Decimal("100.000")
    assert invoice.balance_amt == Decimal("0.000")


def test_returning_everything_leaves_nothing_owed_either_way(
    db: Session, shop: dict, paid_order
):
    _refund_one_unit(db, shop, paid_order,
                     destination=RefundDestination.MONEY_BOX, quantity=5)

    invoice = invoices.build(db, paid_order, language="en")
    assert invoice.net_total_amt == Decimal("0.000")
    assert invoice.paid_amt == Decimal("0.000")
    assert invoice.balance_amt == Decimal("0.000")
    assert invoice.is_refunded


def test_a_return_on_an_unpaid_order_still_reduces_what_is_owed(
    db: Session, stocked: dict
):
    """Goods sent back are goods not billed for, whether or not money moved."""
    shop = stocked
    _cart(db, shop, user=shop["user"], quantity=5)
    order = place_order(
        db, _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    orders.mark_delivered(db, order, shop["user"])
    db.commit()

    before = invoices.build(db, order, language="en")
    assert before.balance_amt == Decimal("100.000")

    _refund_one_unit(db, shop, order, destination=RefundDestination.STORE_CREDIT)

    after = invoices.build(db, order, language="en")
    assert after.net_total_amt == Decimal("80.000")
    assert after.balance_amt == Decimal("80.000")


# ---------------------------------------------------------------------------
# What the document says about the refund
# ---------------------------------------------------------------------------


def test_a_store_credit_refund_is_not_filed_as_cash(
    db: Session, shop: dict, paid_order
):
    """The negative payment row used to take the channel the customer had
    originally paid on, so a refund converted to رصيد was recorded — and
    printed — as "Cash". Nothing left the till, and the customer cannot spend
    store credit anywhere but here (§12)."""
    from sqlalchemy import select

    from app.models.orders import Payment, PaymentChannel

    _refund_one_unit(db, shop, paid_order,
                     destination=RefundDestination.STORE_CREDIT)

    refund = db.scalars(
        select(Payment).where(
            Payment.fk_order_id == paid_order.pk_order_id, Payment.amount_amt < 0
        )
    ).one()
    channel = db.get(PaymentChannel, refund.fk_payment_channel_id)
    assert channel.is_store_credit_flag, (
        f"a store-credit refund was filed against {channel.name_en}"
    )


def test_a_money_box_refund_keeps_the_channel_it_was_paid_out_on(
    db: Session, shop: dict, paid_order
):
    from sqlalchemy import select

    from app.models.orders import Payment

    _refund_one_unit(db, shop, paid_order,
                     destination=RefundDestination.MONEY_BOX)

    refund = db.scalars(
        select(Payment).where(
            Payment.fk_order_id == paid_order.pk_order_id, Payment.amount_amt < 0
        )
    ).one()
    assert refund.fk_payment_channel_id == shop["cash"].pk_payment_channel_id


def test_the_invoice_names_the_return_and_where_the_money_went(
    db: Session, shop: dict, paid_order
):
    order_return = _refund_one_unit(
        db, shop, paid_order, destination=RefundDestination.STORE_CREDIT
    )

    invoice = invoices.build(db, paid_order, language="en")
    assert len(invoice.returns) == 1
    shown = invoice.returns[0]
    assert shown.return_number == order_return.return_number
    assert shown.refund_amt == Decimal("20.000")
    assert shown.to_store_credit
    assert shown.lines == [("Book", 1)]


# ---------------------------------------------------------------------------
# The rendered document
# ---------------------------------------------------------------------------


def test_the_document_shows_the_credit_and_no_balance_due(
    db: Session, shop: dict, paid_order
):
    """The whole point, end to end: the printed page must not ask a settled
    customer for money."""
    from app.core.i18n import translate

    _refund_one_unit(db, shop, paid_order,
                     destination=RefundDestination.STORE_CREDIT)

    invoice = invoices.build(db, paid_order, language="en")
    html = invoices.render_html(_render_request(), invoice)

    assert translate("invoice.net_total", "en") in html
    assert translate("invoice.returned_credit", "en") in html
    assert translate("invoice.balance_due", "en") not in html, (
        "the document still bills the customer for the returned goods"
    )
    assert "80.000" in html


def _render_request():
    """A minimal request the template layer can build its globals from.

    The context has to be set explicitly: `render_html` resolves `t()` from the
    *request's* language, so without one the document renders in the site
    default and an English invoice comes out with Arabic labels.
    """
    from decimal import Decimal as D

    from starlette.datastructures import Headers, QueryParams, URL

    from app.core.context import RequestContext

    class _Request:
        url = URL("http://testserver/invoice")
        base_url = URL("http://testserver/")
        headers = Headers({})
        query_params = QueryParams("")
        cookies: dict = {}

        class _State:
            context = RequestContext(
                language="en", currency="JOD", usd_rate=D("1.41")
            )

        state = _State()
        scope: dict = {"type": "http", "headers": []}

    return _Request()
