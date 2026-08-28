"""Money boxes, payments and store credit (Part I §9, §10, §12).

Part I §10 requires that every money movement records **which box, which
channel, and what it was for**, and that a single transaction may split across
several boxes. So a movement is always a parent ``TRX_MONEY_TRANSACTION`` (the
reason, and the link back to whatever caused it) plus one or more
``TRX_MONEY_ALLOCATION`` rows (the share hitting each box). One allocation is
the ordinary case; more than one is the split.

Balances are never stored. A box's balance is its opening balance plus the SUM
of its allocations, aggregated in SQL (Part II §1) — a stored balance and a
ledger can disagree, and then neither can be trusted. The same is true of a
customer's رصيد.

Everything here is insert-only. A correction is a reversing row, which is what
makes the cancellation-after-payment path in §8 distinct from a post-delivery
return rather than an edit of the original.
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
from app.models.enums import Currency, MoneyDirection, MoneyReason
from app.models.consignment import ConsignmentSettlement
from app.models.inventory import OperatingCost
from app.models.money import (
    MoneyAllocation,
    MoneyBox,
    MoneyBoxReconciliation,
    MoneyTransaction,
    StoreCreditEntry,
)
from app.models.orders import Order, OrderLine, Payment, PaymentChannel
from app.services.pricing import q

log = get_logger(__name__)


@dataclass(slots=True)
class Split:
    """One share of a payment: an amount, a channel, and the box it lands in."""

    channel_id: int
    amount_amt: Decimal
    money_box_id: int | None = None
    reference: str | None = None


@dataclass(slots=True)
class BoxLedgerRow:
    """One allocation in a box ledger, with the running balance after it."""

    allocation: MoneyAllocation
    transaction: MoneyTransaction
    running_balance_amt: Decimal


@dataclass(slots=True)
class MovementTotal:
    """Signed money-box movement total for one reason code."""

    reason_code: str
    amount_amt: Decimal


@dataclass(slots=True)
class FinancialStatement:
    """End-to-end store financial summary for one inclusive date range."""

    start_date: dt.date
    end_date: dt.date
    gross_sales_amt: Decimal
    refunds_amt: Decimal
    net_sales_amt: Decimal
    cogs_amt: Decimal
    gross_profit_amt: Decimal
    operating_costs_amt: Decimal
    consignment_payouts_amt: Decimal
    consignment_collections_amt: Decimal
    net_profit_amt: Decimal
    money_in_amt: Decimal
    money_out_amt: Decimal
    movement_totals: list[MovementTotal]


# ---------------------------------------------------------------------------
# Balances — always computed, never stored
# ---------------------------------------------------------------------------


def box_balance(db: Session, money_box_id: int, *, as_of: dt.datetime | None = None) -> Decimal:
    """Opening balance plus every allocation, summed in SQL."""
    box = db.get(MoneyBox, money_box_id)
    if box is None:
        raise NotFound("That money box does not exist.")

    stmt = (
        select(func.coalesce(func.sum(MoneyAllocation.amount_amt), 0))
        .select_from(MoneyAllocation)
        .where(MoneyAllocation.fk_money_box_id == money_box_id)
    )
    if as_of is not None:
        stmt = stmt.where(MoneyAllocation.created_dt <= as_of)

    return q(Decimal(box.opening_balance_amt) + Decimal(db.scalar(stmt) or 0))


def box_balances(db: Session) -> dict[int, Decimal]:
    """Every open box's balance, in one query rather than one per box."""
    sums = dict(
        db.execute(
            select(
                MoneyAllocation.fk_money_box_id,
                func.coalesce(func.sum(MoneyAllocation.amount_amt), 0),
            ).group_by(MoneyAllocation.fk_money_box_id)
        ).all()
    )
    boxes = db.scalars(
        select(MoneyBox).where(MoneyBox.scd_active_flag.is_(True))
    ).all()
    return {
        box.pk_money_box_id: q(
            Decimal(box.opening_balance_amt)
            + Decimal(sums.get(box.pk_money_box_id, 0) or 0)
        )
        for box in boxes
    }


def create_box(
    db: Session,
    *,
    box_code: str,
    name_ar: str,
    name_en: str,
    opening_balance_amt: Decimal = Decimal("0"),
    branch_id: int | None = None,
    description: str | None = None,
    actor_user_id: int | None = None,
) -> MoneyBox:
    """Create a money box.

    The opening amount is the audited starting point stored on the SCD row; all
    later movement stays in ``TRX_MONEY_ALLOCATION`` (Part I §10).
    """
    code = (box_code or "").strip()
    if not code:
        raise ValidationFailed("Money box code is required.")
    if not (name_ar or "").strip() or not (name_en or "").strip():
        raise ValidationFailed("Money box names are required in both languages.")
    if db.scalars(select(MoneyBox).where(MoneyBox.box_code == code)).first() is not None:
        raise Conflict("That money box code already exists.")

    now = utcnow()
    box = MoneyBox(
        box_code=code,
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        fk_branch_id=branch_id,
        opening_balance_amt=q(Decimal(opening_balance_amt or 0)),
        opened_dt=now,
        is_open_flag=True,
        description=(description or None),
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(box)
    db.flush()
    return box


def close_box(
    db: Session,
    money_box_id: int,
    *,
    actor_user_id: int | None = None,
) -> MoneyBox:
    """Close a box without deleting its ledger history (Part II §6)."""
    box = db.get(MoneyBox, money_box_id)
    if box is None or not box.scd_active_flag:
        raise NotFound("That money box does not exist.")
    if not box.is_open_flag:
        raise Conflict("That money box is already closed.")
    box.is_open_flag = False
    box.scd_changed_by = actor_user_id
    db.flush()
    return box


def box_ledger(
    db: Session,
    money_box_id: int,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    reason_code: str | None = None,
) -> list[BoxLedgerRow]:
    """Ledger rows for one box, with a running balance over the filtered range."""
    box = db.get(MoneyBox, money_box_id)
    if box is None or not box.scd_active_flag:
        raise NotFound("That money box does not exist.")

    start_dt, end_dt = _date_bounds(start_date, end_date)
    opening = Decimal(box.opening_balance_amt)
    if start_dt is not None:
        before = db.scalar(
            select(func.coalesce(func.sum(MoneyAllocation.amount_amt), 0))
            .join(
                MoneyTransaction,
                MoneyTransaction.pk_money_transaction_id
                == MoneyAllocation.fk_money_transaction_id,
            )
            .where(MoneyAllocation.fk_money_box_id == money_box_id)
            .where(MoneyTransaction.occurred_dt < start_dt)
        )
        opening = q(opening + Decimal(before or 0))

    stmt = (
        select(MoneyAllocation, MoneyTransaction)
        .join(
            MoneyTransaction,
            MoneyTransaction.pk_money_transaction_id
            == MoneyAllocation.fk_money_transaction_id,
        )
        .where(MoneyAllocation.fk_money_box_id == money_box_id)
        .order_by(MoneyTransaction.occurred_dt, MoneyAllocation.pk_money_allocation_id)
    )
    if start_dt is not None:
        stmt = stmt.where(MoneyTransaction.occurred_dt >= start_dt)
    if end_dt is not None:
        stmt = stmt.where(MoneyTransaction.occurred_dt < end_dt)
    if reason_code:
        stmt = stmt.where(MoneyTransaction.reason_code == reason_code)

    running = q(opening)
    rows: list[BoxLedgerRow] = []
    for allocation, transaction in db.execute(stmt).all():
        running = q(running + Decimal(allocation.amount_amt))
        rows.append(
            BoxLedgerRow(
                allocation=allocation,
                transaction=transaction,
                running_balance_amt=running,
            )
        )
    return rows


def store_credit_balance(db: Session, user_id: int) -> Decimal:
    """A customer's رصيد — the SUM of their ledger entries (Part I §1.1).

    Always JOD: anything paid or owed in USD-display is converted before it
    lands in the ledger, so this needs no currency argument.
    """
    total = db.scalar(
        select(func.coalesce(func.sum(StoreCreditEntry.amount_amt), 0)).where(
            StoreCreditEntry.fk_user_id == user_id
        )
    )
    return q(Decimal(total or 0))


# ---------------------------------------------------------------------------
# Recording money movements
# ---------------------------------------------------------------------------


def record_transaction(
    db: Session,
    *,
    direction: str,
    reason_code: str,
    allocations: list[tuple[int, Decimal]],
    channel_id: int | None = None,
    order_id: int | None = None,
    order_return_id: int | None = None,
    shipment_id: int | None = None,
    settlement_id: int | None = None,
    operating_cost_id: int | None = None,
    reverses_id: int | None = None,
    description: str | None = None,
    actor_user_id: int | None = None,
    occurred_dt: dt.datetime | None = None,
) -> MoneyTransaction:
    """Write one money movement and its box allocations.

    ``allocations`` is ``[(money_box_id, signed_amount), …]`` — several entries
    is how "split across multiple money boxes" (§10) is represented without
    inventing a second transaction.
    """
    if not allocations:
        raise ValidationFailed("A money transaction must affect at least one box.")

    now = occurred_dt or utcnow()
    transaction = MoneyTransaction(
        reference=_next_reference(db, now),
        direction=direction,
        reason_code=reason_code,
        fk_payment_channel_id=channel_id,
        fk_order_id=order_id,
        fk_order_return_id=order_return_id,
        fk_shipment_id=shipment_id,
        fk_consignment_settlement_id=settlement_id,
        fk_operating_cost_id=operating_cost_id,
        reverses_money_transaction_id=reverses_id,
        description=description,
        occurred_dt=now,
        created_dt=now,
        created_by=actor_user_id,
    )
    db.add(transaction)
    db.flush()

    for box_id, amount in allocations:
        box = db.get(MoneyBox, box_id)
        if box is None or not box.scd_active_flag:
            raise NotFound("That money box does not exist.")
        if not box.is_open_flag:
            raise Conflict(f"Money box '{box.name_en}' is closed.")

        db.add(
            MoneyAllocation(
                fk_money_transaction_id=transaction.pk_money_transaction_id,
                fk_money_box_id=box_id,
                amount_amt=q(amount),
                created_dt=now,
                created_by=actor_user_id,
            )
        )

    log.info(
        "money_transaction",
        extra={
            "reference": transaction.reference,
            "reason": reason_code,
            "boxes": len(allocations),
        },
    )
    return transaction


def reverse_transaction(
    db: Session,
    original: MoneyTransaction,
    *,
    reason_code: str,
    actor_user_id: int | None = None,
    description: str | None = None,
) -> MoneyTransaction:
    """Write the mirror image of an earlier movement.

    Used by cancellation-after-payment, which §8 gives its own path distinct
    from a post-delivery return. The original row stands untouched — it is a
    fact that happened.
    """
    allocations = db.scalars(
        select(MoneyAllocation).where(
            MoneyAllocation.fk_money_transaction_id == original.pk_money_transaction_id
        )
    ).all()

    return record_transaction(
        db,
        direction=(
            MoneyDirection.OUT
            if original.direction == MoneyDirection.IN
            else MoneyDirection.IN
        ),
        reason_code=reason_code,
        allocations=[(a.fk_money_box_id, -Decimal(a.amount_amt)) for a in allocations],
        channel_id=original.fk_payment_channel_id,
        order_id=original.fk_order_id,
        order_return_id=original.fk_order_return_id,
        reverses_id=original.pk_money_transaction_id,
        description=description or f"Reversal of {original.reference}",
        actor_user_id=actor_user_id,
    )


# ---------------------------------------------------------------------------
# Operating costs and financial statements (Part I §11)
# ---------------------------------------------------------------------------


def refund_channel_id(
    db: Session, *, destination: str, fallback_channel_id: int | None
) -> int | None:
    """Which channel a refund should be filed under.

    Store credit is its own channel, flagged `is_store_credit_flag`. Filing a
    رصيد refund against the channel the customer originally paid on records
    cash leaving a till that nothing left, and prints "Cash" on an invoice for
    money the customer can only spend in this shop (§12).
    """
    from app.models.enums import RefundDestination
    from app.models.orders import PaymentChannel

    if destination != RefundDestination.STORE_CREDIT:
        return fallback_channel_id

    channel = db.scalars(
        select(PaymentChannel).where(
            PaymentChannel.is_store_credit_flag.is_(True),
            PaymentChannel.scd_active_flag.is_(True),
        )
    ).first()
    return channel.pk_payment_channel_id if channel else fallback_channel_id


def record_operating_cost(
    db: Session,
    *,
    name_ar: str,
    name_en: str,
    category_code: str,
    amount_amt: Decimal,
    incurred_date: dt.date,
    money_box_id: int,
    is_recurring_flag: bool = False,
    recurrence_months: int | None = None,
    branch_id: int | None = None,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> OperatingCost:
    """Record a store operating cost and the cash movement that paid it."""
    amount = q(abs(Decimal(amount_amt or 0)))
    if amount <= 0:
        raise ValidationFailed("Operating cost amount must be positive.")
    if not (name_ar or "").strip() or not (name_en or "").strip():
        raise ValidationFailed("Cost names are required in both languages.")
    if not (category_code or "").strip():
        raise ValidationFailed("Operating cost category is required.")
    if is_recurring_flag and (recurrence_months is None or recurrence_months <= 0):
        raise ValidationFailed("Recurring costs need a positive recurrence interval.")

    now = utcnow()
    cost = OperatingCost(
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        category_code=category_code.strip(),
        amount_amt=amount,
        incurred_date=incurred_date,
        is_recurring_flag=bool(is_recurring_flag),
        recurrence_months=recurrence_months if is_recurring_flag else None,
        fk_branch_id=branch_id,
        note=note or None,
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(cost)
    db.flush()

    record_transaction(
        db,
        direction=MoneyDirection.OUT,
        reason_code=MoneyReason.OPERATING_COST,
        allocations=[(money_box_id, -amount)],
        operating_cost_id=cost.pk_operating_cost_id,
        description=f"Operating cost: {cost.name_en}",
        actor_user_id=actor_user_id,
        occurred_dt=dt.datetime.combine(
            incurred_date, dt.time.min, tzinfo=dt.timezone.utc
        ),
    )
    db.flush()
    return cost


def operating_costs(
    db: Session,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    category_code: str | None = None,
) -> list[OperatingCost]:
    """Recorded operating costs, newest first."""
    stmt = (
        select(OperatingCost)
        .where(OperatingCost.scd_active_flag.is_(True))
        .order_by(OperatingCost.incurred_date.desc(), OperatingCost.pk_operating_cost_id.desc())
    )
    if start_date is not None:
        stmt = stmt.where(OperatingCost.incurred_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(OperatingCost.incurred_date <= end_date)
    if category_code:
        stmt = stmt.where(OperatingCost.category_code == category_code)
    return list(db.scalars(stmt).all())


def financial_statement(
    db: Session,
    *,
    start_date: dt.date,
    end_date: dt.date,
) -> FinancialStatement:
    """Build the §11 financial statement from source ledgers.

    Sales and refunds come from ``TRX_PAYMENT``; COGS comes from frozen
    ``OrderLine.unit_cost_amt`` at fulfilment; consignment settlement signs are
    kept explicit so inbound payouts reduce profit while outbound collections
    increase it.
    """
    if end_date < start_date:
        raise ValidationFailed("End date must be on or after start date.")

    start_dt, end_dt = _date_bounds(start_date, end_date)

    gross_sales = _sum_decimal(
        db,
        select(func.coalesce(func.sum(Payment.amount_amt), 0))
        .where(Payment.created_dt >= start_dt)
        .where(Payment.created_dt < end_dt)
        .where(Payment.amount_amt > 0),
    )
    refunds = abs(
        _sum_decimal(
            db,
            select(func.coalesce(func.sum(Payment.amount_amt), 0))
            .where(Payment.created_dt >= start_dt)
            .where(Payment.created_dt < end_dt)
            .where(Payment.amount_amt < 0),
        )
    )
    net_sales = q(gross_sales - refunds)

    cogs = _sum_decimal(
        db,
        select(
            func.coalesce(
                func.sum(
                    (OrderLine.quantity_fulfilled - OrderLine.quantity_returned)
                    * OrderLine.unit_cost_amt
                ),
                0,
            )
        )
        .join(Order, Order.pk_order_id == OrderLine.fk_order_id)
        .where(OrderLine.scd_active_flag.is_(True))
        .where(Order.scd_active_flag.is_(True))
        .where(Order.placed_dt >= start_dt)
        .where(Order.placed_dt < end_dt),
    )

    operating = _sum_decimal(
        db,
        select(func.coalesce(func.sum(OperatingCost.amount_amt), 0))
        .where(OperatingCost.scd_active_flag.is_(True))
        .where(OperatingCost.incurred_date >= start_date)
        .where(OperatingCost.incurred_date <= end_date),
    )

    payouts = _sum_decimal(
        db,
        select(func.coalesce(func.sum(ConsignmentSettlement.net_owed_amt), 0))
        .where(ConsignmentSettlement.scd_active_flag.is_(True))
        .where(ConsignmentSettlement.settled_dt >= start_dt)
        .where(ConsignmentSettlement.settled_dt < end_dt)
        .where(ConsignmentSettlement.net_owed_amt > 0),
    )
    collections = abs(
        _sum_decimal(
            db,
            select(func.coalesce(func.sum(ConsignmentSettlement.net_owed_amt), 0))
            .where(ConsignmentSettlement.scd_active_flag.is_(True))
            .where(ConsignmentSettlement.settled_dt >= start_dt)
            .where(ConsignmentSettlement.settled_dt < end_dt)
            .where(ConsignmentSettlement.net_owed_amt < 0),
        )
    )

    movement_rows = db.execute(
        select(
            MoneyTransaction.reason_code,
            func.coalesce(func.sum(MoneyAllocation.amount_amt), 0),
        )
        .join(
            MoneyAllocation,
            MoneyAllocation.fk_money_transaction_id
            == MoneyTransaction.pk_money_transaction_id,
        )
        .where(MoneyTransaction.occurred_dt >= start_dt)
        .where(MoneyTransaction.occurred_dt < end_dt)
        .group_by(MoneyTransaction.reason_code)
        .order_by(MoneyTransaction.reason_code)
    ).all()
    movement_totals = [
        MovementTotal(reason_code=reason, amount_amt=q(Decimal(total or 0)))
        for reason, total in movement_rows
    ]
    money_in = q(
        sum(
            (row.amount_amt for row in movement_totals if row.amount_amt > 0),
            Decimal("0"),
        )
    )
    money_out = q(
        abs(
            sum(
                (row.amount_amt for row in movement_totals if row.amount_amt < 0),
                Decimal("0"),
            )
        )
    )

    gross_profit = q(net_sales - cogs)
    net_profit = q(gross_profit - operating - payouts + collections)
    return FinancialStatement(
        start_date=start_date,
        end_date=end_date,
        gross_sales_amt=q(gross_sales),
        refunds_amt=q(refunds),
        net_sales_amt=net_sales,
        cogs_amt=q(cogs),
        gross_profit_amt=gross_profit,
        operating_costs_amt=q(operating),
        consignment_payouts_amt=q(payouts),
        consignment_collections_amt=q(collections),
        net_profit_amt=net_profit,
        money_in_amt=money_in,
        money_out_amt=money_out,
        movement_totals=movement_totals,
    )


# ---------------------------------------------------------------------------
# Order payments (Part I §9)
# ---------------------------------------------------------------------------


def record_order_payment(
    db: Session,
    order: Order,
    splits: list[Split],
    *,
    actor_user_id: int | None = None,
    default_box_id: int | None = None,
) -> list[Payment]:
    """Record payment against an order, possibly split across channels.

    A single order may be settled across several channels, and partial/deposit
    payments are allowed with the balance reflected against the customer's رصيد
    (Part I §8, §9). Store-credit splits move the customer's ledger instead of a
    money box — the money never enters the till, so pretending it did would put
    the cash count out.
    """
    if not splits:
        raise ValidationFailed("Record at least one payment split.")

    payments: list[Payment] = []
    now = utcnow()

    for split in splits:
        if split.amount_amt == 0:
            continue

        channel = db.get(PaymentChannel, split.channel_id)
        if channel is None or not channel.scd_active_flag:
            raise NotFound("That payment channel does not exist.")

        payments.append(
            Payment(
                fk_order_id=order.pk_order_id,
                fk_payment_channel_id=split.channel_id,
                amount_amt=q(split.amount_amt),
                tendered_currency=Currency.JOD,
                usd_rate_used=order.usd_rate_at_sale,
                reference=split.reference,
                created_dt=now,
                created_by=actor_user_id,
            )
        )
        db.add(payments[-1])

        if channel.is_store_credit_flag:
            # Spending رصيد: debit the customer's ledger, touch no box.
            spend_store_credit(
                db,
                user_id=order.fk_user_id,
                amount_amt=split.amount_amt,
                order_id=order.pk_order_id,
                actor_user_id=actor_user_id,
            )
            continue

        box_id = split.money_box_id or default_box_id
        if box_id is None:
            raise ValidationFailed(
                f"Choose which money box the {channel.name_en} payment goes into."
            )

        record_transaction(
            db,
            direction=(
                MoneyDirection.IN if split.amount_amt > 0 else MoneyDirection.OUT
            ),
            reason_code=MoneyReason.SALE,
            allocations=[(box_id, split.amount_amt)],
            channel_id=split.channel_id,
            order_id=order.pk_order_id,
            description=f"Payment for order {order.order_number}",
            actor_user_id=actor_user_id,
        )

    db.flush()
    refresh_payment_status(db, order)
    return payments


def order_paid_amount(db: Session, order: Order) -> Decimal:
    """Net paid on this order — payments minus refunds, summed in SQL."""
    total = db.scalar(
        select(func.coalesce(func.sum(Payment.amount_amt), 0)).where(
            Payment.fk_order_id == order.pk_order_id
        )
    )
    return q(Decimal(total or 0))


def refresh_payment_status(db: Session, order: Order) -> str:
    """Derive the order's payment status from its payment ledger.

    Derived rather than set by hand so the badge on the order and the money that
    actually arrived can never disagree.
    """
    from app.models.enums import PaymentStatus

    paid = order_paid_amount(db, order)
    total = Decimal(order.total_amt)

    if paid <= 0:
        status = PaymentStatus.REFUNDED if total > 0 and paid < 0 else PaymentStatus.NOT_PAID
    elif paid >= total:
        status = PaymentStatus.PAID
    else:
        status = PaymentStatus.PARTIALLY_PAID

    order.payment_status = status
    return status


def outstanding_balance(db: Session, order: Order) -> Decimal:
    """What is still owed. Negative means the customer is owed a refund."""
    return q(Decimal(order.total_amt) - order_paid_amount(db, order))


# ---------------------------------------------------------------------------
# Store credit (رصيد)
# ---------------------------------------------------------------------------


def grant_store_credit(
    db: Session,
    *,
    user_id: int,
    amount_amt: Decimal,
    reason_code: str,
    order_id: int | None = None,
    order_return_id: int | None = None,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> StoreCreditEntry:
    """Credit a customer's رصيد. Always a positive JOD amount."""
    if amount_amt <= 0:
        raise ValidationFailed("Store credit granted must be a positive amount.")

    entry = StoreCreditEntry(
        fk_user_id=user_id,
        amount_amt=q(amount_amt),
        reason_code=reason_code,
        fk_order_id=order_id,
        fk_order_return_id=order_return_id,
        note=note,
        created_dt=utcnow(),
        created_by=actor_user_id,
    )
    db.add(entry)
    return entry


def spend_store_credit(
    db: Session,
    *,
    user_id: int,
    amount_amt: Decimal,
    order_id: int | None = None,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> StoreCreditEntry:
    """Debit a customer's رصيد, refusing to overdraw it."""
    from app.models.enums import MoneyReason as Reason

    amount = q(abs(Decimal(amount_amt)))
    available = store_credit_balance(db, user_id)
    if amount > available:
        raise Conflict(
            "That is more store credit than the customer has.",
            details={"available_amt": str(available), "requested_amt": str(amount)},
        )

    entry = StoreCreditEntry(
        fk_user_id=user_id,
        amount_amt=-amount,
        reason_code=Reason.STORE_CREDIT_SPEND,
        fk_order_id=order_id,
        note=note,
        created_dt=utcnow(),
        created_by=actor_user_id,
    )
    db.add(entry)
    return entry


# ---------------------------------------------------------------------------
# Reconciliation (Part I §10)
# ---------------------------------------------------------------------------


def reconcile_box(
    db: Session,
    *,
    money_box_id: int,
    counted_amt: Decimal,
    actor_user_id: int | None = None,
    note: str | None = None,
    adjust: bool = False,
) -> MoneyBoxReconciliation:
    """Record a cash count: expected vs. actually counted.

    ``expected_amt`` is frozen at the moment of counting so the variance cannot
    drift as later transactions land. When ``adjust`` is set, a balancing
    transaction is written so the box agrees with reality going forward — the
    variance itself stays on the record either way.
    """
    now = utcnow()
    expected = box_balance(db, money_box_id)

    reconciliation = MoneyBoxReconciliation(
        fk_money_box_id=money_box_id,
        counted_dt=now,
        expected_amt=expected,
        counted_amt=q(counted_amt),
        counted_by_user_id=actor_user_id,
        note=note,
        scd_active_from=now,
    )
    db.add(reconciliation)
    db.flush()

    variance = reconciliation.variance_amt
    if adjust and variance != 0:
        adjustment = record_transaction(
            db,
            direction=MoneyDirection.IN if variance > 0 else MoneyDirection.OUT,
            reason_code=MoneyReason.RECONCILIATION_ADJUSTMENT,
            allocations=[(money_box_id, variance)],
            description=f"Reconciliation adjustment ({variance:+})",
            actor_user_id=actor_user_id,
            occurred_dt=now,
        )
        reconciliation.fk_adjustment_transaction_id = (
            adjustment.pk_money_transaction_id
        )

    log.info(
        "money_box_reconciled",
        extra={"box": money_box_id, "variance": str(variance), "adjusted": adjust},
    )
    return reconciliation


def _date_bounds(
    start_date: dt.date | None,
    end_date: dt.date | None,
) -> tuple[dt.datetime | None, dt.datetime | None]:
    start_dt = (
        dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
        if start_date is not None
        else None
    )
    end_dt = (
        dt.datetime.combine(
            end_date + dt.timedelta(days=1),
            dt.time.min,
            tzinfo=dt.timezone.utc,
        )
        if end_date is not None
        else None
    )
    return start_dt, end_dt


def _sum_decimal(db: Session, stmt) -> Decimal:
    return q(Decimal(db.scalar(stmt) or 0))


def _next_reference(db: Session, now: dt.datetime) -> str:
    """``MTX-YYMMDD-NNNN`` — readable on a printed cash sheet."""
    prefix = f"MTX-{now:%y%m%d}"
    count = db.scalar(
        select(func.count())
        .select_from(MoneyTransaction)
        .where(MoneyTransaction.reference.like(f"{prefix}-%"))
    ) or 0
    return f"{prefix}-{count + 1:04d}"
