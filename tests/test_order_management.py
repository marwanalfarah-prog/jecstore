"""Order fulfilment, payments and cancellation (Part I §8, §9, §10).

The behaviours pinned here are the ones with money or stock behind them:
hand-over converts a hold into a deduction, payments land in a money box, and
cancellation-after-payment refuses to guess where the refund goes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import Conflict, ValidationFailed
from app.db.base import Base, utcnow
from app.models.catalog import Product, ProductVariant
from app.models.enums import (
    LineFulfillmentStatus,
    MoneyReason,
    MovementKind,
    ON_HAND_MOVEMENT_KINDS,
    OrderStatus,
    PaymentStatus,
    RESERVATION_MOVEMENT_KINDS,
    RoleCode,
    StockPoolKind,
)
from app.models.identity import Role, User
from app.models.inventory import StockLevel, StockMovement, StockPool
from app.models.money import MoneyAllocation, MoneyBox, MoneyTransaction, StoreCreditEntry
from app.models.orders import Order, OrderLine, Payment, PaymentChannel
from app.services import money, orders
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from app.services.pricing import q
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401 - fixtures


@pytest.fixture
def shop(db: Session, store: dict) -> dict:
    """The seeded store plus a cash channel and a till."""
    now = utcnow()
    cash = PaymentChannel(
        channel_code="cash", name_ar="نقداً", name_en="Cash", scd_active_from=now
    )
    credit = PaymentChannel(
        channel_code="store_credit", name_ar="رصيد", name_en="Store credit",
        is_store_credit_flag=True, scd_active_from=now,
    )
    box = MoneyBox(
        box_code="till", name_ar="الصندوق", name_en="Till",
        opening_balance_amt=Decimal("0"), scd_active_from=now,
    )
    db.add_all([cash, credit, box])
    db.commit()
    return {**store, "cash": cash, "credit": credit, "box": box}


def _place(db: Session, shop: dict, quantity: int = 2) -> Order:
    _cart(db, shop, user=shop["user"], quantity=quantity)
    order = place_order(
        db, _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()
    return order


# ---------------------------------------------------------------------------
# Status rollup (Part I §9)
# ---------------------------------------------------------------------------


def test_order_status_is_derived_from_its_lines(db: Session, shop: dict):
    order = _place(db, shop)
    assert order.status == OrderStatus.PLACED

    orders.mark_prepared(db, order, shop["user"])
    db.commit()
    assert order.status == OrderStatus.IN_PREPARATION


def test_partial_fulfilment_is_representable(db: Session, shop: dict):
    """Split fulfilment: one line delivered, one still open (Part I §9)."""
    order = _place(db, shop, quantity=1)

    # A second line on the same order.
    now = utcnow()
    variant2 = ProductVariant(
        fk_product_id=shop["product"].pk_product_id, sku="SKU-2", scd_active_from=now
    )
    db.add(variant2)
    db.flush()
    db.add(
        OrderLine(
            fk_order_id=order.pk_order_id,
            fk_product_variant_id=variant2.pk_product_variant_id,
            quantity=1, list_price_amt=Decimal("20.000"),
            unit_price_amt=Decimal("20.000"), line_total_amt=Decimal("20.000"),
            fulfillment_status=LineFulfillmentStatus.ORDERED_FOR_PICKUP,
            scd_active_from=now,
        )
    )
    db.commit()

    first = orders.active_lines(db, order)[0]
    orders.set_line_status(db, order, first, LineFulfillmentStatus.DELIVERED)
    db.commit()

    assert order.status == OrderStatus.PARTIALLY_FULFILLED


# ---------------------------------------------------------------------------
# Hand-over: hold becomes deduction (Part I §8)
# ---------------------------------------------------------------------------


def test_handover_deducts_on_hand_and_clears_the_hold(db: Session, shop: dict):
    order = _place(db, shop, quantity=2)

    level = db.scalars(select(StockLevel)).one()
    assert (level.quantity_on_hand, level.quantity_reserved) == (3, 2)

    orders.mark_delivered(db, order, shop["user"])
    db.commit()
    db.refresh(level)

    assert level.quantity_on_hand == 1, "units finally leave the shelf"
    assert level.quantity_reserved == 0, "the hold is cleared"
    assert level.quantity_sellable == 1


def test_handover_writes_both_ledger_rows(db: Session, shop: dict):
    """Release *and* sale: the two projections are summed from different sets."""
    order = _place(db, shop, quantity=2)
    orders.mark_delivered(db, order, shop["user"])
    db.commit()

    kinds = [m.movement_kind for m in db.scalars(select(StockMovement)).all()]
    assert MovementKind.RESERVATION_HOLD in kinds
    assert MovementKind.RESERVATION_RELEASE in kinds
    assert MovementKind.SALE in kinds


def test_movement_ledger_reconciles_with_both_projections(db: Session, shop: dict):
    """Summing each movement-kind set must reproduce its projection exactly.

    This is the invariant the reconciliation report will rely on, so it is worth
    proving now rather than discovering a sign error during a stock take.
    """
    order = _place(db, shop, quantity=2)
    orders.mark_delivered(db, order, shop["user"])
    db.commit()

    level = db.scalars(select(StockLevel)).one()

    on_hand_delta = db.scalar(
        select(func.coalesce(func.sum(StockMovement.quantity_delta), 0)).where(
            StockMovement.movement_kind.in_(ON_HAND_MOVEMENT_KINDS)
        )
    )
    reserved_delta = db.scalar(
        select(func.coalesce(func.sum(StockMovement.quantity_delta), 0)).where(
            StockMovement.movement_kind.in_(RESERVATION_MOVEMENT_KINDS)
        )
    )

    # The fixture seeds 3 units without a shipment-in movement, so on-hand
    # movements account for the change from that baseline.
    assert level.quantity_on_hand == 3 + on_hand_delta
    assert level.quantity_reserved == reserved_delta


def test_handover_increments_the_purchase_count(db: Session, shop: dict):
    before = shop["product"].purchase_count
    order = _place(db, shop, quantity=2)
    orders.mark_delivered(db, order, shop["user"])
    db.commit()
    db.refresh(shop["product"])
    assert shop["product"].purchase_count == before + 2


# ---------------------------------------------------------------------------
# Payments (Part I §9, §10)
# ---------------------------------------------------------------------------


def test_payment_lands_in_a_money_box(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)

    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=order.total_amt,
                     money_box_id=shop["box"].pk_money_box_id)],
    )
    db.commit()

    assert order.payment_status == PaymentStatus.PAID
    assert money.box_balance(db, shop["box"].pk_money_box_id) == order.total_amt

    transaction = db.scalars(select(MoneyTransaction)).one()
    assert transaction.reason_code == MoneyReason.SALE
    assert transaction.fk_order_id == order.pk_order_id


def test_partial_payment_is_allowed_and_reported(db: Session, shop: dict):
    """Partial/deposit payments are allowed (Part I §8)."""
    order = _place(db, shop, quantity=2)
    half = q(Decimal(order.total_amt) / 2)

    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=half, money_box_id=shop["box"].pk_money_box_id)],
    )
    db.commit()

    assert order.payment_status == PaymentStatus.PARTIALLY_PAID
    assert money.outstanding_balance(db, order) == q(Decimal(order.total_amt) - half)


def test_payment_can_split_across_channels(db: Session, shop: dict):
    """A single order may be split across several channels (Part I §9)."""
    order = _place(db, shop, quantity=2)
    total = Decimal(order.total_amt)
    part = q(total / 2)

    money.grant_store_credit(
        db, user_id=shop["user"].pk_user_id, amount_amt=part,
        reason_code=MoneyReason.STORE_CREDIT_TOPUP,
    )
    db.commit()

    money.record_order_payment(
        db, order,
        [
            money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                        amount_amt=total - part,
                        money_box_id=shop["box"].pk_money_box_id),
            money.Split(channel_id=shop["credit"].pk_payment_channel_id,
                        amount_amt=part),
        ],
    )
    db.commit()

    assert order.payment_status == PaymentStatus.PAID
    # Store credit never touches the till — only the cash half did.
    assert money.box_balance(db, shop["box"].pk_money_box_id) == q(total - part)
    assert money.store_credit_balance(db, shop["user"].pk_user_id) == Decimal("0.000")


def test_store_credit_cannot_be_overdrawn(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    with pytest.raises(Conflict):
        money.record_order_payment(
            db, order,
            [money.Split(channel_id=shop["credit"].pk_payment_channel_id,
                         amount_amt=Decimal("999.000"))],
        )


# ---------------------------------------------------------------------------
# Adjustments (Part I §9)
# ---------------------------------------------------------------------------


def test_invoice_discount_recalculates_the_total(db: Session, shop: dict):
    order = _place(db, shop, quantity=2)
    subtotal = Decimal(order.subtotal_amt)

    orders.apply_invoice_discount(db, order, percentage=Decimal("10"))
    db.commit()

    assert order.invoice_discount_amt == q(subtotal / 10)
    assert order.total_amt == q(subtotal - subtotal / 10)


def test_line_discount_updates_line_and_order(db: Session, shop: dict):
    order = _place(db, shop, quantity=2)
    line = orders.active_lines(db, order)[0]

    orders.apply_line_discount(db, order, line, fixed_price_amt=Decimal("15.000"))
    db.commit()

    assert line.unit_price_amt == Decimal("15.000")
    assert line.line_total_amt == Decimal("30.000")
    assert order.subtotal_amt == Decimal("30.000")
    assert line.list_price_amt == Decimal("20.000"), "the original list price is kept"


def test_editing_quantity_adjusts_the_hold(db: Session, shop: dict):
    """§9: staff may edit a placed order; the store's reservation must follow."""
    order = _place(db, shop, quantity=1)
    line = orders.active_lines(db, order)[0]
    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_reserved == 1

    orders.update_line_quantity(db, order, line, 3)
    db.commit()
    db.refresh(level)

    assert line.quantity == 3
    assert level.quantity_reserved == 3
    assert level.quantity_on_hand == 3, "on-hand is untouched until hand-over"


def test_a_completed_order_cannot_be_adjusted(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    orders.mark_delivered(db, order, shop["user"])
    db.commit()

    with pytest.raises(Conflict):
        orders.apply_invoice_discount(db, order, percentage=Decimal("10"))


def test_setting_shipping_clears_the_pending_quote_flag(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    order.shipping_quote_pending_flag = True
    db.commit()

    orders.set_shipping_cost(db, order, Decimal("3.500"))
    db.commit()

    assert order.shipping_amt == Decimal("3.500")
    assert order.shipping_quote_pending_flag is False


# ---------------------------------------------------------------------------
# Cancellation (Part I §8)
# ---------------------------------------------------------------------------


def test_cancelling_releases_the_held_stock(db: Session, shop: dict):
    order = _place(db, shop, quantity=2)
    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_reserved == 2

    orders.cancel_order(db, order, reason="customer changed their mind")
    db.commit()
    db.refresh(level)

    assert order.status == OrderStatus.CANCELLED
    assert level.quantity_reserved == 0
    assert level.quantity_on_hand == 3, "nothing was ever handed over"


def test_cancelling_a_paid_order_refuses_to_guess(db: Session, shop: dict):
    """§8 requires the system to *prompt* for where refunded money goes."""
    order = _place(db, shop, quantity=1)
    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=order.total_amt,
                     money_box_id=shop["box"].pk_money_box_id)],
    )
    db.commit()

    with pytest.raises(ValidationFailed):
        orders.cancel_order(db, order)


def test_cancellation_refund_to_money_box(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    total = Decimal(order.total_amt)
    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=total, money_box_id=shop["box"].pk_money_box_id)],
    )
    db.commit()
    assert money.box_balance(db, shop["box"].pk_money_box_id) == total

    orders.cancel_order(
        db, order,
        refund=orders.RefundInstruction(money_box_id=shop["box"].pk_money_box_id),
    )
    db.commit()

    assert money.box_balance(db, shop["box"].pk_money_box_id) == Decimal("0.000")
    assert order.payment_status == PaymentStatus.REFUNDED


def test_cancellation_refund_to_store_credit(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    total = Decimal(order.total_amt)
    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=total, money_box_id=shop["box"].pk_money_box_id)],
    )
    db.commit()

    orders.cancel_order(
        db, order, refund=orders.RefundInstruction(to_store_credit=True)
    )
    db.commit()

    assert money.store_credit_balance(db, shop["user"].pk_user_id) == total
    # The till keeps the cash — it was converted to credit, not handed back.
    assert money.box_balance(db, shop["box"].pk_money_box_id) == total


def test_a_completed_order_cannot_be_cancelled(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    orders.mark_delivered(db, order, shop["user"])
    db.commit()

    with pytest.raises(Conflict):
        orders.cancel_order(db, order)


# ---------------------------------------------------------------------------
# Reconciliation (Part I §10)
# ---------------------------------------------------------------------------


def test_reconciliation_records_the_variance(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=order.total_amt,
                     money_box_id=shop["box"].pk_money_box_id)],
    )
    db.commit()

    short_by = Decimal("1.000")
    reconciliation = money.reconcile_box(
        db, money_box_id=shop["box"].pk_money_box_id,
        counted_amt=Decimal(order.total_amt) - short_by,
    )
    db.commit()

    assert reconciliation.variance_amt == -short_by
    # Without `adjust`, the box balance is left alone — the variance is a
    # finding to investigate, not something to paper over automatically.
    assert money.box_balance(db, shop["box"].pk_money_box_id) == order.total_amt


def test_reconciliation_can_write_a_balancing_transaction(db: Session, shop: dict):
    order = _place(db, shop, quantity=1)
    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=order.total_amt,
                     money_box_id=shop["box"].pk_money_box_id)],
    )
    db.commit()

    counted = Decimal(order.total_amt) - Decimal("1.000")
    money.reconcile_box(
        db, money_box_id=shop["box"].pk_money_box_id,
        counted_amt=counted, adjust=True,
    )
    db.commit()

    assert money.box_balance(db, shop["box"].pk_money_box_id) == counted
