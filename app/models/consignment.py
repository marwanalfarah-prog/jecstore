"""Consignment in both directions (Part I §7).

One set of tables covers both directions because the mechanics are identical
and only the sign of the split changes:

* **Outbound** — our items, held by someone else to sell (a bazaar). The stock
  leaves our sellable pool but stays ours.
* **Inbound** — their items, held by us to sell. The stock sits in our branch
  but is *not* ours: it is excluded from the cost-average pool, since we never
  paid for it (Part I §7, §11).

The per-arrangement settings on :class:`Consignment` are the ones the spec
leaves explicitly to Admin: whether the split is taken on the discounted or the
original price, and whether storewide promocodes may touch these items.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, MONEY, SCDMixin, TrxBase, UtcDateTime, fk, pk
from app.models.enums import (
    ConsignmentDirection,
    ConsignmentItemState,
    ConsignmentSplitBasis,
)


class Consignor(Base, SCDMixin):
    """The other party — a supplier who leaves stock with us, or a bazaar we
    leave stock with. May or may not also hold a customer account."""

    __tablename__ = "scd_consignor"
    __grain__ = "One version of one consignment counterparty."

    pk_consignor_id: Mapped[int] = pk("consignor")
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    contact_person: Mapped[str | None] = mapped_column(String(200))
    phone_country_code: Mapped[str | None] = mapped_column(String(6))
    phone_number: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    #: Linked when the counterparty also has a login on the platform.
    fk_user_id: Mapped[int | None] = fk("user", "scd_user.pk_user_id", nullable=True)
    note: Mapped[str | None] = mapped_column(Text)


class Consignment(Base, SCDMixin):
    """One arrangement with one counterparty, in one direction."""

    __tablename__ = "scd_consignment"
    __grain__ = "One version of one consignment arrangement."

    pk_consignment_id: Mapped[int] = pk("consignment")
    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    fk_consignor_id: Mapped[int] = fk("consignor", "scd_consignor.pk_consignor_id")
    direction: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    #: Overall split, applied to any item that does not set its own — "90/10
    #: across everything they're holding" (Part I §7). Per-item overrides live
    #: on :class:`ConsignmentItem`.
    default_our_share_percentage: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    #: Admin's call, per arrangement: does the split apply to the discounted
    #: price or the original listed price (Part I §7)?
    split_basis: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ConsignmentSplitBasis.DISCOUNTED_PRICE
    )
    #: Admin's call: may storewide promocodes apply to these items? If so, the
    #: discount routes through the split logic above (Part I §7).
    promocodes_eligible_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    #: Where outbound stock is parked, or where inbound stock is sold from.
    fk_stock_pool_id: Mapped[int | None] = fk(
        "stock_pool", "scd_stock_pool.pk_stock_pool_id", nullable=True
    )

    starts_date: Mapped[dt.date | None] = mapped_column(Date)
    ends_date: Mapped[dt.date | None] = mapped_column(Date)
    closed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    note: Mapped[str | None] = mapped_column(Text)

    consignor: Mapped[Consignor] = relationship()
    items: Mapped[list["ConsignmentItem"]] = relationship(back_populates="consignment")
    settlements: Mapped[list["ConsignmentSettlement"]] = relationship(
        back_populates="consignment"
    )


class ConsignmentItem(Base, SCDMixin):
    """One variant placed under one arrangement, with its own split if it
    differs from the arrangement default ("first item: 85/15" — Part I §7)."""

    __tablename__ = "scd_consignment_item"
    __grain__ = "One version of one variant's placement under one consignment arrangement."

    pk_consignment_item_id: Mapped[int] = pk("consignment_item")
    fk_consignment_id: Mapped[int] = fk("consignment", "scd_consignment.pk_consignment_id")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )

    quantity_placed: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Returned to the owning party, whether at the end of the arrangement or as
    #: a partial recall before settlement (Part I §7).
    quantity_returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Damaged or lost in custody: excluded from sales and inventory reporting
    #: going forward, but its cost stays in the item's historical cost
    #: calculation rather than being erased (Part I §7).
    quantity_damaged_or_lost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Agreed selling price — original price, or a price set for this
    #: arrangement (Part I §7).
    agreed_price_amt: Mapped[Decimal | None] = mapped_column(MONEY)
    our_share_percentage: Mapped[Decimal | None] = mapped_column(
        MONEY, comment="Overrides the arrangement default for this item."
    )
    state: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ConsignmentItemState.HELD, index=True
    )

    consignment: Mapped[Consignment] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_scd_consignment_item_variant", "fk_product_variant_id"),
    )

    @property
    def quantity_outstanding(self) -> int:
        """Units still held by whoever is holding them."""
        return (
            self.quantity_placed
            - self.quantity_sold
            - self.quantity_returned
            - self.quantity_damaged_or_lost
        )


class ConsignmentSale(TrxBase):
    """A sale of a consigned unit and the split it produced — insert-only.

    A customer return writes a *reversing* row rather than editing this one, so
    the original split is properly unwound and both figures stay visible in the
    audit trail (Part I §7).
    """

    __tablename__ = "trx_consignment_sale"
    __grain__ = "One sale (or reversal) of consigned units, with the resulting revenue split."

    pk_consignment_sale_id: Mapped[int] = pk("consignment_sale")
    fk_consignment_item_id: Mapped[int] = fk(
        "consignment_item", "scd_consignment_item.pk_consignment_item_id"
    )
    fk_order_line_id: Mapped[int | None] = fk(
        "order_line", "scd_order_line.pk_order_line_id", nullable=True
    )
    #: Negative on a reversal.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Both prices are recorded so the settlement report can show which basis
    #: was used and what the alternative would have been.
    list_price_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    sold_price_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    split_basis: Mapped[str] = mapped_column(String(30), nullable=False)
    our_share_percentage: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    our_share_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    their_share_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    #: Set once rolled into a settlement, so a payout cannot double-count.
    fk_consignment_settlement_id: Mapped[int | None] = fk(
        "consignment_settlement",
        "scd_consignment_settlement.pk_consignment_settlement_id",
        nullable=True,
    )
    reverses_consignment_sale_id: Mapped[int | None] = mapped_column(Integer, index=True)

    __table_args__ = (
        Index("ix_trx_consignment_sale_item_time", "fk_consignment_item_id", "created_dt"),
    )


class ConsignmentSettlement(Base, SCDMixin):
    """A periodic reconciliation of what is owed, and the payout that clears it
    — tied into the money-box system (Part I §7, §10)."""

    __tablename__ = "scd_consignment_settlement"
    __grain__ = "One version of one settlement between us and one consignment counterparty."

    pk_consignment_settlement_id: Mapped[int] = pk("consignment_settlement")
    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    fk_consignment_id: Mapped[int] = fk("consignment", "scd_consignment.pk_consignment_id")

    period_start_date: Mapped[dt.date | None] = mapped_column(Date)
    period_end_date: Mapped[dt.date | None] = mapped_column(Date)

    #: Positive means we owe them; negative means they owe us. One signed figure
    #: keeps outbound and inbound arrangements on the same footing.
    net_owed_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    settled_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    settled_by_user_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)

    consignment: Mapped[Consignment] = relationship(back_populates="settlements")
