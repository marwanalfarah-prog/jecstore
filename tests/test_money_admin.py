"""Money-box admin support: boxes, split allocations, costs, statements (§10, §11)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.models.enums import LineFulfillmentStatus, MoneyDirection, MoneyReason
from app.models.money import MoneyAllocation, MoneyTransaction
from app.services import money, money_actions, orders
from tests.test_checkout import db, store  # noqa: F401 - fixtures
from tests.test_order_management import _place, shop  # noqa: F401 - fixtures


def test_create_and_close_box_preserves_balance_but_blocks_new_allocations(db: Session):
    box = money.create_box(
        db,
        box_code="safe",
        name_ar="الخزنة",
        name_en="Safe",
        opening_balance_amt=Decimal("25.000"),
    )
    db.commit()

    assert money.box_balance(db, box.pk_money_box_id) == Decimal("25.000")

    money.close_box(db, box.pk_money_box_id)
    db.commit()

    with pytest.raises(Conflict):
        money.record_transaction(
            db,
            direction=MoneyDirection.IN,
            reason_code=MoneyReason.OTHER,
            allocations=[(box.pk_money_box_id, Decimal("1.000"))],
        )


def test_manual_transaction_action_replays_split_allocations(db: Session, shop: dict):
    second = money.create_box(
        db,
        box_code="safe",
        name_ar="الخزنة",
        name_en="Safe",
    )
    transaction = money_actions.create_transaction(
        db,
        {
            "operation": "transaction",
            "direction": MoneyDirection.IN,
            "reason_code": MoneyReason.OTHER,
            "allocation_count": 2,
            "allocation_box_id_1": shop["box"].pk_money_box_id,
            "allocation_amount_amt_1": Decimal("10.000"),
            "allocation_box_id_2": second.pk_money_box_id,
            "allocation_amount_amt_2": Decimal("5.000"),
            "description": "Split deposit",
        },
        actor_user_id=None,
    )
    db.commit()

    allocations = db.scalars(
        select(MoneyAllocation).where(
            MoneyAllocation.fk_money_transaction_id
            == transaction.pk_money_transaction_id
        )
    ).all()
    assert len(allocations) == 2
    assert money.box_balance(db, shop["box"].pk_money_box_id) == Decimal("10.000")
    assert money.box_balance(db, second.pk_money_box_id) == Decimal("5.000")


def test_box_ledger_returns_running_balance(db: Session, shop: dict):
    money.record_transaction(
        db,
        direction=MoneyDirection.IN,
        reason_code=MoneyReason.OTHER,
        allocations=[(shop["box"].pk_money_box_id, Decimal("10.000"))],
    )
    money.record_transaction(
        db,
        direction=MoneyDirection.OUT,
        reason_code=MoneyReason.OTHER,
        allocations=[(shop["box"].pk_money_box_id, Decimal("-4.000"))],
    )
    db.commit()

    rows = money.box_ledger(db, shop["box"].pk_money_box_id)

    assert [row.running_balance_amt for row in rows] == [
        Decimal("10.000"),
        Decimal("6.000"),
    ]


def test_operating_cost_action_records_cost_and_money_movement(db: Session, shop: dict):
    today = dt.date.today()
    cost = money_actions.create_transaction(
        db,
        {
            "operation": "operating_cost",
            "name_ar": "إيجار",
            "name_en": "Rent",
            "category_code": "rent",
            "amount_amt": Decimal("12.000"),
            "incurred_date": today,
            "money_box_id": shop["box"].pk_money_box_id,
            "is_recurring_flag": True,
            "recurrence_months": 1,
        },
        actor_user_id=None,
    )
    db.commit()

    transaction = db.scalars(
        select(MoneyTransaction).where(
            MoneyTransaction.fk_operating_cost_id == cost.pk_operating_cost_id
        )
    ).one()
    assert transaction.reason_code == MoneyReason.OPERATING_COST
    assert cost.is_recurring_flag is True
    assert cost.recurrence_months == 1
    assert money.box_balance(db, shop["box"].pk_money_box_id) == Decimal("-12.000")


def test_financial_statement_uses_payments_frozen_cogs_and_costs(
    db: Session, shop: dict
):
    order = _place(db, shop, quantity=1)
    orders.mark_prepared(db, order, shop["user"])
    for line in orders.active_lines(db, order):
        orders.set_line_status(
            db,
            order,
            line,
            LineFulfillmentStatus.DELIVERED,
            staff=shop["user"],
        )
    money.record_order_payment(
        db,
        order,
        [
            money.Split(
                channel_id=shop["cash"].pk_payment_channel_id,
                amount_amt=order.total_amt,
                money_box_id=shop["box"].pk_money_box_id,
            )
        ],
    )
    money.record_operating_cost(
        db,
        name_ar="كهرباء",
        name_en="Electricity",
        category_code="utilities",
        amount_amt=Decimal("2.000"),
        incurred_date=dt.date.today(),
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()

    statement = money.financial_statement(
        db,
        start_date=dt.date.today(),
        end_date=dt.date.today(),
    )

    assert statement.gross_sales_amt == Decimal(order.total_amt)
    assert statement.refunds_amt == Decimal("0.000")
    assert statement.cogs_amt == Decimal("12.000")
    assert statement.operating_costs_amt == Decimal("2.000")
    assert statement.net_profit_amt == Decimal(order.total_amt) - Decimal("14.000")
