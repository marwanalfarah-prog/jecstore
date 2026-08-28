"""Branches, stock pools, movements, shipments and costing (Part I §6, §11).

**The ledger is the truth.** ``TRX_STOCK_MOVEMENT`` is insert-only and records
every reason stock moves. ``SCD_STOCK_LEVEL`` is a *materialised projection* of
that ledger — the one deliberate exception to the no-derived-values rule in
Part II §1, taken because:

* the stock-reservation race condition (Part I §8) needs a single row to lock,
  and you cannot lock an aggregate; and
* Part II §2 explicitly asks for a stated rule per case — view vs. materialised
  table vs. raw query. This is the materialised case, and the choice is stated.

``app/services/inventory.py`` reconciles the projection against the ledger and
reports any drift, so the redundancy stays honest rather than becoming a second,
competing truth.
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, MONEY, RATE, SCDMixin, TrxBase, UtcDateTime, bilingual, fk, pk
from app.models.enums import Currency, StockPoolKind


class Branch(Base, SCDMixin):
    """A physical branch (Part I §6)."""

    __tablename__ = "scd_branch"
    __grain__ = "One version of one branch."

    pk_branch_id: Mapped[int] = pk("branch")
    name_ar, name_en = bilingual("name", 160)
    phone_country_code: Mapped[str | None] = mapped_column(String(6))
    phone_number: Mapped[str | None] = mapped_column(String(20))
    address_ar: Mapped[str | None] = mapped_column(Text)
    address_en: Mapped[str | None] = mapped_column(Text)
    #: Rendered on an embedded map, with a Google Maps directions link built
    #: from the same pair (Part I §6).
    latitude: Mapped[Decimal | None] = mapped_column(RATE)
    longitude: Mapped[Decimal | None] = mapped_column(RATE)
    is_pickup_point_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    hours: Mapped[list["BranchHours"]] = relationship(back_populates="branch")


class BranchHours(Base, SCDMixin):
    """Standing weekly operating hours. One-off closures are homepage
    announcements instead, per the decision in Part I §4."""

    __tablename__ = "scd_branch_hours"
    __grain__ = "One version of one weekday's opening hours for one branch."

    pk_branch_hours_id: Mapped[int] = pk("branch_hours")
    fk_branch_id: Mapped[int] = fk("branch", "scd_branch.pk_branch_id")
    #: 0 = Sunday, matching the local working week.
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    opens_at: Mapped[str | None] = mapped_column(String(5), comment="HH:MM local time.")
    closes_at: Mapped[str | None] = mapped_column(String(5))
    is_closed_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    branch: Mapped[Branch] = relationship(back_populates="hours")

    __table_args__ = (UniqueConstraint("fk_branch_id", "weekday", name="hours_per_weekday"),)


class StockPool(Base, SCDMixin):
    """Somewhere stock can sit: a branch, central storage, or out on consignment.

    Modelling all three as one kind of thing is what lets a movement between any
    two of them be the same operation, and what gives the "which pool does this
    barcode draw from" decision (Part I §5.4) something concrete to point at.
    """

    __tablename__ = "scd_stock_pool"
    __grain__ = "One version of one stock-holding location."

    pk_stock_pool_id: Mapped[int] = pk("stock_pool")
    pool_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    fk_branch_id: Mapped[int | None] = fk("branch", "scd_branch.pk_branch_id", nullable=True)
    name_ar, name_en = bilingual("name", 160)
    #: False for consignment-out pools: stock we no longer physically hold is
    #: not sellable from the storefront (Part I §7).
    is_sellable_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: False for consigned-in stock, which is excluded from the cost-average
    #: pool because we do not own it (Part I §7).
    is_owned_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    branch: Mapped[Branch | None] = relationship()


class StockLevel(Base, SCDMixin):
    """Current quantity of one variant in one pool — the row checkout locks.

    ``quantity_on_hand`` counts units physically present. ``quantity_reserved``
    counts units promised to a placed order but not yet handed over: on checkout
    quantities go *on hold*, and are only deducted from on-hand at delivery
    (Part I §8). Sellable stock is therefore ``on_hand - reserved``.
    """

    __tablename__ = "scd_stock_level"
    __grain__ = "Current quantity of one product variant in one stock pool."

    pk_stock_level_id: Mapped[int] = pk("stock_level")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    fk_stock_pool_id: Mapped[int] = fk("stock_pool", "scd_stock_pool.pk_stock_pool_id")

    quantity_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Rolling average unit cost for this pool, in JOD, recomputed on every
    #: shipment-in and sale by the formula in Part I §11. Never evaluated at zero
    #: stock — an item at zero is not available at all (Part I §5.4), so the
    #: division-by-zero case never arises on the live site; the last computed
    #: value survives here as the baseline for the next shipment.
    average_cost_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)

    last_movement_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    variant: Mapped["object"] = relationship("ProductVariant")
    pool: Mapped[StockPool] = relationship()

    __table_args__ = (
        Index(
            "uq_scd_stock_level_variant_pool_active",
            "fk_product_variant_id",
            "fk_stock_pool_id",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
    )

    @property
    def quantity_sellable(self) -> int:
        return max(self.quantity_on_hand - self.quantity_reserved, 0)


class StockMovement(TrxBase):
    """Every stock movement, insert-only — the ledger of record (Part I §11).

    A correction is a new opposing row, never an edit, which is what lets the
    per-item in/out dashboard and the variance report be trusted.
    """

    __tablename__ = "trx_stock_movement"
    __grain__ = "One movement of one quantity of one variant into or out of one pool."

    pk_stock_movement_id: Mapped[int] = pk("stock_movement")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    fk_stock_pool_id: Mapped[int] = fk("stock_pool", "scd_stock_pool.pk_stock_pool_id")

    movement_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    #: Signed: positive adds to the pool, negative removes from it. One column
    #: rather than in/out pairs, so a balance is a single SUM.
    #: Signed, and which pool it signs against depends on the kind:
    #:
    #: * on-hand kinds (sale, shipment-in, transfer, write-off…) sign against
    #:   ``quantity_on_hand`` — positive in, negative out;
    #: * reservation kinds sign against ``quantity_reserved`` — a HOLD is
    #:   positive, a RELEASE negative.
    #:
    #: Summing each set reproduces its projection exactly, which is what makes
    #: the reconciliation report possible. See ``ON_HAND_MOVEMENT_KINDS`` and
    #: ``RESERVATION_MOVEMENT_KINDS`` in app/models/enums.py.
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Unit cost in JOD at the moment of the movement — frozen here so historical
    #: margins stay accurate even after cost changes (Part I §11).
    unit_cost_amt: Mapped[Decimal | None] = mapped_column(MONEY)

    #: What caused it. Exactly one is normally set.
    fk_shipment_id: Mapped[int | None] = fk(
        "shipment", "scd_shipment.pk_shipment_id", nullable=True
    )
    fk_order_line_id: Mapped[int | None] = fk(
        "order_line", "scd_order_line.pk_order_line_id", nullable=True
    )
    fk_stock_transfer_id: Mapped[int | None] = fk(
        "stock_transfer", "scd_stock_transfer.pk_stock_transfer_id", nullable=True
    )
    fk_stock_take_id: Mapped[int | None] = fk(
        "stock_take", "scd_stock_take.pk_stock_take_id", nullable=True
    )
    fk_consignment_id: Mapped[int | None] = fk(
        "consignment", "scd_consignment.pk_consignment_id", nullable=True
    )
    #: Set for WRITE_OFF: damaged / lost / expired (Part I §11).
    write_off_reason: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_trx_stock_movement_variant_time", "fk_product_variant_id", "created_dt"),
        Index("ix_trx_stock_movement_pool_time", "fk_stock_pool_id", "created_dt"),
        Index("ix_trx_stock_movement_kind_time", "movement_kind", "created_dt"),
    )


class Shipment(Base, SCDMixin):
    """An incoming purchase invoice (Part I §11)."""

    __tablename__ = "scd_shipment"
    __grain__ = "One version of one incoming shipment/purchase invoice."

    pk_shipment_id: Mapped[int] = pk("shipment")
    reference: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    supplier_name: Mapped[str | None] = mapped_column(String(200))
    invoice_date: Mapped[dt.date | None] = mapped_column(Date, index=True)

    #: Only JOD or USD are accepted for shipment costing (Part I §1.1).
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default=Currency.JOD)
    #: The rate used to convert a USD invoice to stored JOD, frozen at entry.
    usd_rate_used: Mapped[Decimal | None] = mapped_column(RATE)

    #: Default pool for the whole invoice; a line may override it. This is the
    #: "entire invoice or each item individually" decision in Part I §11.
    fk_stock_pool_id: Mapped[int | None] = fk(
        "stock_pool", "scd_stock_pool.pk_stock_pool_id", nullable=True
    )

    invoice_file_path: Mapped[str | None] = mapped_column(String(500))
    no_invoice_available_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Explicit state, so a missing file is a recorded fact rather than an omission.",
    )
    shipping_cost_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    customs_cost_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text)
    received_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    lines: Mapped[list["ShipmentLine"]] = relationship(back_populates="shipment")


class ShipmentLine(Base, SCDMixin):
    """One item on an incoming invoice, at its cost.

    This is the link that ties a product to every historical invoice it ever
    arrived on (Part I §11).
    """

    __tablename__ = "scd_shipment_line"
    __grain__ = "One version of one line of one incoming shipment."

    pk_shipment_line_id: Mapped[int] = pk("shipment_line")
    fk_shipment_id: Mapped[int] = fk("shipment", "scd_shipment.pk_shipment_id")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    #: Per-line override of the invoice's pool (Part I §11).
    fk_stock_pool_id: Mapped[int | None] = fk(
        "stock_pool", "scd_stock_pool.pk_stock_pool_id", nullable=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: As entered, in the invoice currency.
    unit_cost_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: Converted to JOD at ``usd_rate_used``; this is what costing consumes.
    unit_cost_jod_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    shipment: Mapped[Shipment] = relationship(back_populates="lines")


class StockTransfer(Base, SCDMixin):
    """Branch-to-branch movement — its own type, neither a sale nor a
    shipment-in (Part I §11)."""

    __tablename__ = "scd_stock_transfer"
    __grain__ = "One version of one stock transfer between two pools."

    pk_stock_transfer_id: Mapped[int] = pk("stock_transfer")
    reference: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    fk_from_stock_pool_id: Mapped[int] = fk(
        "from_stock_pool", "scd_stock_pool.pk_stock_pool_id"
    )
    fk_to_stock_pool_id: Mapped[int] = fk("to_stock_pool", "scd_stock_pool.pk_stock_pool_id")
    dispatched_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    #: Until this is set the units are in transit — counted out of the origin
    #: but not yet available at the destination.
    received_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    note: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["StockTransferLine"]] = relationship(back_populates="transfer")


class StockTransferLine(Base, SCDMixin):
    __tablename__ = "scd_stock_transfer_line"
    __grain__ = "One version of one variant and quantity within one stock transfer."

    pk_stock_transfer_line_id: Mapped[int] = pk("stock_transfer_line")
    fk_stock_transfer_id: Mapped[int] = fk(
        "stock_transfer", "scd_stock_transfer.pk_stock_transfer_id"
    )
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_received: Mapped[int | None] = mapped_column(Integer)

    transfer: Mapped[StockTransfer] = relationship(back_populates="lines")


class StockTake(Base, SCDMixin):
    """A physical count, reconciling system quantity against reality
    (Part I §11)."""

    __tablename__ = "scd_stock_take"
    __grain__ = "One version of one physical stock-count exercise."

    pk_stock_take_id: Mapped[int] = pk("stock_take")
    reference: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    fk_stock_pool_id: Mapped[int] = fk("stock_pool", "scd_stock_pool.pk_stock_pool_id")
    started_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    #: Adjustment movements are only written once the count is closed, so a
    #: half-finished count never corrupts live stock.
    completed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    note: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["StockTakeLine"]] = relationship(back_populates="stock_take")


class StockTakeLine(Base, SCDMixin):
    """One counted variant. The variance report is
    ``counted_quantity - system_quantity`` across these rows."""

    __tablename__ = "scd_stock_take_line"
    __grain__ = "One version of one variant's counted vs. system quantity in one stock take."

    pk_stock_take_line_id: Mapped[int] = pk("stock_take_line")
    fk_stock_take_id: Mapped[int] = fk("stock_take", "scd_stock_take.pk_stock_take_id")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    #: Frozen when the count sheet is generated, so later sales during the count
    #: do not silently move the target.
    system_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    counted_quantity: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)

    stock_take: Mapped[StockTake] = relationship(back_populates="lines")

    @property
    def variance(self) -> int | None:
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.system_quantity


class OperatingCost(Base, SCDMixin):
    """A running store cost — rent, salaries, utilities.

    Held here so the financial statements in Part I §11 cover the whole store
    process, not just item-level margin.
    """

    __tablename__ = "scd_operating_cost"
    __grain__ = "One version of one recorded operating cost."

    pk_operating_cost_id: Mapped[int] = pk("operating_cost")
    name_ar, name_en = bilingual("name", 200)
    category_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    amount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    incurred_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    is_recurring_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recurrence_months: Mapped[int | None] = mapped_column(Integer)
    fk_branch_id: Mapped[int | None] = fk("branch", "scd_branch.pk_branch_id", nullable=True)
    note: Mapped[str | None] = mapped_column(Text)
