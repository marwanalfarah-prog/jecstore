"""Invoices and receipts (Part I §9, §1.1, §2.6).

Two properties carry this module. §1.1: the currency must be clearly specified
whenever an invoice is downloaded. §9: an invoice reprints from data frozen at
sale, so a later rename or price change cannot rewrite history.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.models.enums import Currency
from app.services import invoices, money, orders
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


@pytest.fixture
def paid_order(db: Session, shop: dict):
    _cart(db, shop, user=shop["user"], quantity=2)
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


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_invoice_uses_values_frozen_at_sale(db: Session, shop: dict, paid_order):
    """A later price change or rename must not rewrite a past invoice (§9)."""
    invoice = invoices.build(db, paid_order, language="en")
    original_name = invoice.lines[0].name
    original_price = invoice.lines[0].unit_price_amt

    shop["product"].name_en = "Renamed Later"
    shop["product"].base_price_amt = Decimal("999.000")
    db.commit()

    reprinted = invoices.build(db, paid_order, language="en")
    assert reprinted.lines[0].name == original_name
    assert reprinted.lines[0].unit_price_amt == original_price


def test_invoice_totals_match_the_order(db: Session, paid_order):
    invoice = invoices.build(db, paid_order, language="en")
    line_total = sum((line.line_total_amt for line in invoice.lines), Decimal("0"))

    assert line_total == paid_order.subtotal_amt
    assert invoice.paid_amt == paid_order.total_amt
    assert invoice.balance_amt == Decimal("0.000")


def test_unpaid_order_shows_a_balance(db: Session, shop: dict):
    _cart(db, shop, user=shop["user"], quantity=1)
    order = place_order(
        db, _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    invoice = invoices.build(db, order, language="en")
    assert invoice.paid_amt == Decimal("0.000")
    assert invoice.balance_amt == order.total_amt


def test_invoice_lists_payments(db: Session, paid_order):
    invoice = invoices.build(db, paid_order, language="en")
    assert len(invoice.payments) == 1
    assert invoice.payments[0].amount_amt == paid_order.total_amt
    assert invoice.payments[0].is_refund is False


def test_refund_marks_it_a_credit_note(db: Session, shop: dict, paid_order):
    """A refunded order is a credit note, and the document must say so."""
    orders.cancel_order(
        db, paid_order,
        refund=orders.RefundInstruction(money_box_id=shop["box"].pk_money_box_id),
    )
    db.commit()

    invoice = invoices.build(db, paid_order, language="en")
    assert invoice.is_refunded is True


# ---------------------------------------------------------------------------
# Currency (Part I §1.1)
# ---------------------------------------------------------------------------


def test_secondary_currency_only_when_the_customer_shopped_in_usd(
    db: Session, paid_order
):
    """Printing an unasked-for conversion invites the reader to think they were
    charged in it."""
    paid_order.display_currency = Currency.JOD
    db.commit()
    assert invoices.build(db, paid_order).shows_secondary_currency is False

    paid_order.display_currency = Currency.USD
    db.commit()
    assert invoices.build(db, paid_order).shows_secondary_currency is True


def test_usd_equivalent_uses_the_rate_frozen_at_sale(db: Session, paid_order):
    """§1.1: past orders report at the rate that applied at time of sale."""
    paid_order.display_currency = Currency.USD
    db.commit()

    invoice = invoices.build(db, paid_order, language="en")
    expected = Decimal(paid_order.total_amt) * Decimal(paid_order.usd_rate_at_sale)

    rendered = invoice.secondary_total().replace(",", "")
    assert abs(Decimal(rendered) - expected) < Decimal("0.01")


# ---------------------------------------------------------------------------
# Access scoping (Part I §2.6)
# ---------------------------------------------------------------------------


def test_lookup_scoped_to_the_owning_customer(db: Session, shop: dict, paid_order):
    """An order number alone must not expose somebody else's invoice."""
    owner = shop["user"].pk_user_id

    found = invoices.for_order_number(db, paid_order.order_number, user_id=owner)
    assert found.order.pk_order_id == paid_order.pk_order_id

    with pytest.raises(NotFound):
        invoices.for_order_number(db, paid_order.order_number, user_id=owner + 999)


def test_unknown_order_number_is_not_found(db: Session):
    with pytest.raises(NotFound):
        invoices.for_order_number(db, "JEC-000000-000")


def test_filename_is_the_order_number(db: Session, paid_order):
    invoice = invoices.build(db, paid_order)
    assert invoices.document_filename(invoice, "pdf") == f"{paid_order.order_number}.pdf"


# ---------------------------------------------------------------------------
# Emailing (Part I §9)
# ---------------------------------------------------------------------------


def test_emailing_an_invoice_queues_once(db: Session, paid_order):
    """Re-sending the same invoice must not spam the customer."""
    from sqlalchemy import func, select

    from app.db.base import utcnow
    from app.models.enums import EmailTemplateCode
    from app.models.marketing import EmailOutbox, EmailTemplate

    # The test database has no seeded templates, and queue_template_email
    # correctly refuses to queue against one that does not exist.
    db.add(
        EmailTemplate(
            template_code=EmailTemplateCode.ORDER_CONFIRMATION,
            subject_ar="تأكيد الطلب {order_number}",
            subject_en="Order {order_number} confirmed",
            body_ar="شكراً. الإجمالي {total}.",
            body_en="Thank you. Total {total}.",
            scd_active_from=utcnow(),
        )
    )
    db.commit()

    invoice = invoices.build(db, paid_order, language="ar")
    invoices.queue_invoice_email(db, invoice)
    invoices.queue_invoice_email(db, invoice)
    db.commit()

    queued = db.scalar(
        select(func.count())
        .select_from(EmailOutbox)
        .where(EmailOutbox.template_code == EmailTemplateCode.ORDER_CONFIRMATION)
    )
    # Two sends, one row: the idempotency key collapses the duplicate.
    assert queued == 1


def test_emailing_is_a_no_op_without_a_template(db: Session, paid_order):
    """A missing template is logged, not raised — it must not break the order."""
    from sqlalchemy import func, select

    from app.models.marketing import EmailOutbox

    invoices.queue_invoice_email(db, invoices.build(db, paid_order))
    db.commit()

    assert db.scalar(select(func.count()).select_from(EmailOutbox)) == 0
