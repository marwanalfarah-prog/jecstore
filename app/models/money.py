"""Money boxes, store credit and exchange rates (Part I §1.1, §10, §12).

Every money movement records **which box, which channel, and what for**, and a
single transaction may split across several boxes (Part I §10). That is why a
transaction is a parent row with one or more *allocations*: the split is
first-class rather than something reconstructed from several unrelated rows.

Store credit (رصيد) is JOD-denominated internally — anything paid or owed in
USD-display is converted before it lands here (Part I §1.1). The balance is
never stored: it is the SUM of the entries, aggregated in SQL (Part II §1).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, MONEY, RATE, SCDMixin, TrxBase, UtcDateTime, bilingual, fk, pk
from app.models.enums import Currency, MoneyDirection, MoneyReason


class ExchangeRate(Base, SCDMixin):
    """The JOD→USD display rate, with a dated history (Part I §1.1).

    SCD rather than a settings row precisely so past orders can report at the
    rate that applied at time of sale: ``as_of()`` returns the version that was
    live on any given date.
    """

    __tablename__ = "scd_exchange_rate"
    __grain__ = "One version of the JOD to USD conversion rate."

    pk_exchange_rate_id: Mapped[int] = pk("exchange_rate")
    #: 1 JOD = this many USD. Default 1.41 (Part I §1.1).
    jod_to_usd_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class MoneyBox(Base, SCDMixin):
    """A box money sits in — a branch till, a safe, a bank account (Part I §10).

    ``opening_balance_amt`` is recorded here as the audited starting point; the
    *current* balance is opening + SUM(allocations), computed in SQL, never
    stored — otherwise two numbers could disagree about how much money exists.
    """

    __tablename__ = "scd_money_box"
    __grain__ = "One version of one money box."

    pk_money_box_id: Mapped[int] = pk("money_box")
    box_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name_ar, name_en = bilingual("name", 160)
    fk_branch_id: Mapped[int | None] = fk("branch", "scd_branch.pk_branch_id", nullable=True)
    opening_balance_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    opened_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    #: A closed box keeps its history and stops accepting new transactions.
    is_open_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("box_code", name="money_box_code"),)


class MoneyTransaction(TrxBase):
    """One money movement — insert-only. A correction is a reversing row.

    The amount lives on the allocations, not here, because a transaction may
    split across boxes; this row holds the *reason* and the links back to
    whatever caused it.
    """

    __tablename__ = "trx_money_transaction"
    __grain__ = "One money movement in or out, which may split across several boxes."

    pk_money_transaction_id: Mapped[int] = pk("money_transaction")
    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    fk_payment_channel_id: Mapped[int | None] = fk(
        "payment_channel", "lkp_payment_channel.pk_payment_channel_id", nullable=True
    )

    #: What it was for / where it came from — at most one of these is set.
    fk_order_id: Mapped[int | None] = fk("order", "scd_order.pk_order_id", nullable=True)
    fk_order_return_id: Mapped[int | None] = fk(
        "order_return", "scd_order_return.pk_order_return_id", nullable=True
    )
    fk_shipment_id: Mapped[int | None] = fk(
        "shipment", "scd_shipment.pk_shipment_id", nullable=True
    )
    fk_consignment_settlement_id: Mapped[int | None] = fk(
        "consignment_settlement",
        "scd_consignment_settlement.pk_consignment_settlement_id",
        nullable=True,
    )
    fk_operating_cost_id: Mapped[int | None] = fk(
        "operating_cost", "scd_operating_cost.pk_operating_cost_id", nullable=True
    )
    #: Set when this row reverses an earlier one (cancellation after payment —
    #: its own path, distinct from a post-delivery return, per Part I §8).
    reverses_money_transaction_id: Mapped[int | None] = mapped_column(Integer, index=True)

    description: Mapped[str | None] = mapped_column(Text)
    occurred_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )

    allocations: Mapped[list["MoneyAllocation"]] = relationship(back_populates="transaction")

    __table_args__ = (
        Index("ix_trx_money_transaction_reason_time", "reason_code", "occurred_dt"),
    )


class MoneyAllocation(TrxBase):
    """The share of one transaction that hits one box.

    A transaction with a single allocation is the ordinary case; more than one
    is how "split across multiple money boxes" (Part I §10) is represented
    without inventing a second transaction.
    """

    __tablename__ = "trx_money_allocation"
    __grain__ = "The portion of one money transaction affecting one money box."

    pk_money_allocation_id: Mapped[int] = pk("money_allocation")
    fk_money_transaction_id: Mapped[int] = fk(
        "money_transaction", "trx_money_transaction.pk_money_transaction_id"
    )
    fk_money_box_id: Mapped[int] = fk("money_box", "scd_money_box.pk_money_box_id")
    #: Signed JOD: positive into the box, negative out of it. Balance is a SUM.
    amount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tendered_currency: Mapped[str] = mapped_column(String(3), nullable=False, default=Currency.JOD)
    usd_rate_used: Mapped[Decimal | None] = mapped_column(RATE)

    transaction: Mapped[MoneyTransaction] = relationship(back_populates="allocations")

    __table_args__ = (
        Index("ix_trx_money_allocation_box", "fk_money_box_id", "created_dt"),
    )


class MoneyBoxReconciliation(Base, SCDMixin):
    """A cash audit: expected vs. actually counted (Part I §10)."""

    __tablename__ = "scd_money_box_reconciliation"
    __grain__ = "One version of one reconciliation of one money box at one moment."

    pk_money_box_reconciliation_id: Mapped[int] = pk("money_box_reconciliation")
    fk_money_box_id: Mapped[int] = fk("money_box", "scd_money_box.pk_money_box_id")
    counted_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    #: System-calculated at the moment of counting, frozen so the variance
    #: cannot drift as later transactions land.
    expected_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    counted_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    counted_by_user_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    #: The adjusting transaction written to bring the box into line, if any.
    fk_adjustment_transaction_id: Mapped[int | None] = fk(
        "adjustment_transaction",
        "trx_money_transaction.pk_money_transaction_id",
        nullable=True,
    )

    @property
    def variance_amt(self) -> Decimal:
        return self.counted_amt - self.expected_amt


class StoreCreditEntry(TrxBase):
    """One movement of a customer's رصيد — insert-only.

    The balance is ``SUM(amount_amt)`` over these rows, aggregated in SQL. There
    is deliberately no balance column: a stored balance and a ledger can
    disagree, and then neither can be trusted.
    """

    __tablename__ = "trx_store_credit_entry"
    __grain__ = "One credit or debit of one customer's store credit balance."

    pk_store_credit_entry_id: Mapped[int] = pk("store_credit_entry")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    #: Signed JOD — always JOD, never a USD-display value (Part I §1.1).
    amount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    fk_order_id: Mapped[int | None] = fk("order", "scd_order.pk_order_id", nullable=True)
    fk_order_return_id: Mapped[int | None] = fk(
        "order_return", "scd_order_return.pk_order_return_id", nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    expires_date: Mapped[dt.date | None] = mapped_column(Date)

    __table_args__ = (
        Index("ix_trx_store_credit_entry_user_time", "fk_user_id", "created_dt"),
    )
