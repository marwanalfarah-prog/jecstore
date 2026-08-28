"""Cart, checkout, orders, fulfilment and returns (Part I §8, §9, §12).

Two structural decisions drive this module:

**Fulfilment status lives on the line, not the order.** Split and mixed
fulfilment are both explicitly required (Part I §9) — some items shipping now
while others are backordered, some picked up by hand while others are
delivered — so a single order-level status could not represent reality. The
order-level status is a rollup computed from its lines.

**Orders freeze what they need.** Price, list price, cost and the USD rate are
copied onto the line at sale time (Part I §11, §1.1). A past invoice must
reprint identically years later, and margin reporting must not shift when a cost
or an exchange rate changes. The cart, by contrast, freezes *nothing*: an item
sitting in an open cart always reflects the current rate and current discount at
the moment of checkout (Part I §1.1).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
from app.models.enums import (
    Currency,
    FulfillmentMethod,
    LineFulfillmentStatus,
    OrderStatus,
    PaymentStatus,
    RefundDestination,
    ReturnStatus,
)


class Cart(Base, SCDMixin):
    """An open basket. Belongs to a user, or to an anonymous session while the
    visitor is still browsing (guest browsing is allowed; guest *checkout* is
    not — Part I §8, §14).

    Cart contents survive a session timeout so a customer who stepped away is
    not punished for it (Part I §2.3).
    """

    __tablename__ = "scd_cart"
    __grain__ = "One version of one shopping cart."

    pk_cart_id: Mapped[int] = pk("cart")
    fk_user_id: Mapped[int | None] = fk("user", "scd_user.pk_user_id", nullable=True)
    session_key: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Applied optimistically for display; re-validated at checkout, because the
    #: code's limits or expiry may have moved since it was typed.
    fk_promocode_id: Mapped[int | None] = fk(
        "promocode", "scd_promocode.pk_promocode_id", nullable=True
    )
    converted_order_id: Mapped[int | None] = mapped_column(Integer)
    abandoned_dt: Mapped[dt.datetime | None] = mapped_column(
        UtcDateTime,
        comment="Set by the abandonment job; feeds the cart-abandonment dashboard (Part I §2.8).",
    )
    last_activity_dt: Mapped[dt.datetime | None] = mapped_column(
        UtcDateTime, index=True
    )

    lines: Mapped[list["CartLine"]] = relationship(back_populates="cart")

    __table_args__ = (
        Index("ix_scd_cart_user_active", "fk_user_id", "scd_active_flag"),
    )


class CartLine(Base, SCDMixin):
    """One variant and quantity in a cart.

    Note what is *absent*: no price column. Price is resolved live on every
    render, so the cart always shows the current rate and current discount
    (Part I §1.1) rather than whatever was true when the item was added.
    """

    __tablename__ = "scd_cart_line"
    __grain__ = "One version of one variant and quantity in one cart."

    pk_cart_line_id: Mapped[int] = pk("cart_line")
    fk_cart_id: Mapped[int] = fk("cart", "scd_cart.pk_cart_id")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    added_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    cart: Mapped[Cart] = relationship(back_populates="lines")

    __table_args__ = (
        Index(
            "uq_scd_cart_line_active",
            "fk_cart_id",
            "fk_product_variant_id",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
    )


class Order(Base, SCDMixin):
    """A placed order.

    The shipping address is *copied in*, not referenced: editing a saved address
    later must never rewrite where a past order was actually delivered.
    """

    __tablename__ = "scd_order"
    __grain__ = "One version of one placed order."

    pk_order_id: Mapped[int] = pk("order")
    order_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")

    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=OrderStatus.PLACED, index=True
    )
    payment_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PaymentStatus.NOT_PAID, index=True
    )
    placed_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    completed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    cancelled_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(
        Integer, comment="Either the customer or an admin may cancel (Part I §8)."
    )

    # --- Money. Every amount is JOD; USD is display-only. ------------------
    subtotal_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    item_discount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    #: Whole-invoice discount applied at fulfilment (Part I §9).
    invoice_discount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    invoice_discount_percentage: Mapped[Decimal | None] = mapped_column(MONEY)
    promocode_discount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    shipping_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    total_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    #: Portion settled from store credit (رصيد), which is JOD-denominated.
    store_credit_applied_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)

    fk_promocode_id: Mapped[int | None] = fk(
        "promocode", "scd_promocode.pk_promocode_id", nullable=True
    )

    #: What the customer was shown, and the rate that produced it. Frozen so a
    #: past order always reports at the rate at time of sale (Part I §1.1).
    display_currency: Mapped[str] = mapped_column(String(3), nullable=False, default=Currency.JOD)
    usd_rate_at_sale: Mapped[Decimal] = mapped_column(RATE, nullable=False)

    # --- Shipping snapshot -------------------------------------------------
    #: Order-level default; each line still carries its own method so one order
    #: can mix pickup and delivery (Part I §9).
    fulfillment_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FulfillmentMethod.PICKUP
    )
    fk_pickup_branch_id: Mapped[int | None] = fk(
        "pickup_branch", "scd_branch.pk_branch_id", nullable=True
    )
    ship_country_name: Mapped[str | None] = mapped_column(String(120))
    ship_province_name: Mapped[str | None] = mapped_column(String(120))
    ship_city: Mapped[str | None] = mapped_column(String(120))
    ship_address_line: Mapped[str | None] = mapped_column(Text)
    ship_zip_code: Mapped[str | None] = mapped_column(String(20))
    ship_po_box: Mapped[str | None] = mapped_column(String(20))
    ship_phone: Mapped[str | None] = mapped_column(String(30))
    #: True when the destination falls outside the configured governorate rules
    #: and the cost is agreed by contact instead (Part I §2.2).
    shipping_quote_pending_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    #: Written by the customer at checkout (Part I §8).
    customer_note: Mapped[str | None] = mapped_column(Text)
    #: Staff-only, never rendered on the customer's order page (Part I §9).
    internal_note: Mapped[str | None] = mapped_column(Text)

    #: Who packed it (Part I §9). An immutable historical reference: the staff
    #: account may later be deactivated without breaking this record.
    prepared_by_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    prepared_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    lines: Mapped[list["OrderLine"]] = relationship(back_populates="order")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")

    __table_args__ = (
        UniqueConstraint("order_number", name="order_number"),
        Index("ix_scd_order_user_placed", "fk_user_id", "placed_dt"),
        Index("ix_scd_order_status_placed", "status", "placed_dt"),
    )


class OrderLine(Base, SCDMixin):
    """One variant on an order, with its own fulfilment method and status."""

    __tablename__ = "scd_order_line"
    __grain__ = "One version of one variant line on one order."

    pk_order_line_id: Mapped[int] = pk("order_line")
    fk_order_id: Mapped[int] = fk("order", "scd_order.pk_order_id")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    #: Which pool the units are held in and will be deducted from.
    fk_stock_pool_id: Mapped[int | None] = fk(
        "stock_pool", "scd_stock_pool.pk_stock_pool_id", nullable=True
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_fulfilled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity_returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Frozen at sale (Part I §11) ---------------------------------------
    #: What the product was listed at, before any discount.
    list_price_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: What it actually sold for, per unit, after item/category discounts.
    unit_price_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: Average cost at the moment of sale — frozen so margin history is stable
    #: even after cost later changes.
    unit_cost_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    #: Applied by staff at fulfilment time, on top of any catalog discount
    #: (Part I §9).
    manual_discount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    line_total_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    #: Names copied in so an invoice reprints identically even if the product is
    #: later renamed.
    product_name_ar, product_name_en = bilingual("product_name", 300, nullable=True)
    variant_label_ar: Mapped[str | None] = mapped_column(String(300))
    variant_label_en: Mapped[str | None] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(60))

    fulfillment_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FulfillmentMethod.PICKUP
    )
    fulfillment_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=LineFulfillmentStatus.ORDERED_FOR_PICKUP, index=True
    )
    #: True while units sit on hold — reserved, not yet deducted from sellable
    #: stock, which happens only on hand-over (Part I §8).
    stock_held_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Set when the line is a consigned item, so the revenue split can be
    #: settled — and unwound on return (Part I §7).
    fk_consignment_item_id: Mapped[int | None] = fk(
        "consignment_item", "scd_consignment_item.pk_consignment_item_id", nullable=True
    )

    order: Mapped[Order] = relationship(back_populates="lines")

    __table_args__ = (
        Index("ix_scd_order_line_variant", "fk_product_variant_id"),
        Index("ix_scd_order_line_status", "fulfillment_status"),
    )


class PaymentChannel(Base, SCDMixin):
    """A way money arrives or leaves: Cash, Visa, CliQ… defined by Admin
    (Part I §9)."""

    __tablename__ = "lkp_payment_channel"
    __grain__ = "One version of one payment channel."

    pk_payment_channel_id: Mapped[int] = pk("payment_channel")
    channel_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name_ar, name_en = bilingual("name", 120)
    #: Store credit is a channel too, so a رصيد-funded order reconciles through
    #: the same path as cash (Part I §12).
    is_store_credit_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Payment(TrxBase):
    """One payment against one order, on one channel — insert-only.

    Several rows per order is the normal case, not an edge case: an order may be
    split across channels, and partial/deposit payments are allowed with the
    balance reflected against the customer's رصيد (Part I §8, §9).
    """

    __tablename__ = "trx_payment"
    __grain__ = "One payment or refund against one order, on one channel."

    pk_payment_id: Mapped[int] = pk("payment")
    fk_order_id: Mapped[int] = fk("order", "scd_order.pk_order_id")
    fk_payment_channel_id: Mapped[int] = fk(
        "payment_channel", "lkp_payment_channel.pk_payment_channel_id"
    )
    #: Negative for a refund. One signed column keeps the order balance a SUM.
    amount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: What the customer handed over, if they paid in USD — recorded for the
    #: receipt, while the stored value stays JOD (Part I §1.1).
    tendered_currency: Mapped[str] = mapped_column(String(3), nullable=False, default=Currency.JOD)
    tendered_amt: Mapped[Decimal | None] = mapped_column(MONEY)
    usd_rate_used: Mapped[Decimal | None] = mapped_column(RATE)
    reference: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)

    order: Mapped[Order] = relationship(back_populates="payments")

    __table_args__ = (
        Index("ix_trx_payment_order", "fk_order_id"),
        Index("ix_trx_payment_channel_time", "fk_payment_channel_id", "created_dt"),
    )


class OrderReturn(Base, SCDMixin):
    """A return request. Nothing refunds automatically — the item is inspected
    first (Part I §12)."""

    __tablename__ = "scd_order_return"
    __grain__ = "One version of one return request against one order."

    pk_order_return_id: Mapped[int] = pk("order_return")
    return_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    fk_order_id: Mapped[int] = fk("order", "scd_order.pk_order_id")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")

    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ReturnStatus.REQUESTED, index=True
    )
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(Text)

    inspected_by_user_id: Mapped[int | None] = mapped_column(Integer)
    inspected_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    inspection_note: Mapped[str | None] = mapped_column(Text)
    condition_acceptable_flag: Mapped[bool | None] = mapped_column(
        Boolean, comment="The approval/condition check that gates any refund (Part I §12)."
    )

    #: Cash back out of a money box, or converted to رصيد (Part I §12).
    refund_destination: Mapped[str | None] = mapped_column(String(30))
    fk_money_box_id: Mapped[int | None] = fk(
        "money_box", "scd_money_box.pk_money_box_id", nullable=True
    )
    refund_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    #: True once stock has been put back and the consignment split unwound, so a
    #: retry cannot double-apply the effects.
    effects_applied_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refunded_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    lines: Mapped[list["OrderReturnLine"]] = relationship(back_populates="order_return")


class OrderReturnLine(Base, SCDMixin):
    """Partial returns are the norm: this is per line item and quantity, not
    per whole order (Part I §12)."""

    __tablename__ = "scd_order_return_line"
    __grain__ = "One version of one returned quantity of one order line."

    pk_order_return_line_id: Mapped[int] = pk("order_return_line")
    fk_order_return_id: Mapped[int] = fk(
        "order_return", "scd_order_return.pk_order_return_id"
    )
    fk_order_line_id: Mapped[int] = fk("order_line", "scd_order_line.pk_order_line_id")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    refund_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    #: False when the unit comes back damaged — it is written off rather than
    #: returned to sellable stock (Part I §11).
    restock_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text)

    order_return: Mapped[OrderReturn] = relationship(back_populates="lines")


class ShippingRule(Base, SCDMixin):
    """Shipping cost by Jordanian governorate, with a free-above threshold, and
    an explicit "contact the customer" state for anywhere else (Part I §2.2).
    """

    __tablename__ = "scd_shipping_rule"
    __grain__ = "One version of one shipping-cost rule for one destination."

    pk_shipping_rule_id: Mapped[int] = pk("shipping_rule")
    fk_country_id: Mapped[int | None] = fk(
        "country", "lkp_country.pk_country_id", nullable=True
    )
    #: Null with a country set means "anywhere else in this country".
    fk_province_id: Mapped[int | None] = fk(
        "province", "lkp_province.pk_province_id", nullable=True
    )
    cost_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    #: Order subtotal at or above which shipping is free. Null disables it.
    free_above_amt: Mapped[Decimal | None] = mapped_column(MONEY)
    #: "Not included — will be contacted": the order is placed with shipping
    #: unpriced and staff quote it afterwards.
    quote_on_contact_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note_ar: Mapped[str | None] = mapped_column(Text)
    note_en: Mapped[str | None] = mapped_column(Text)
    #: Most specific match wins; ties break on the higher priority.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Wishlist(Base, SCDMixin):
    """Save-for-later (Part I §14)."""

    __tablename__ = "scd_wishlist"
    __grain__ = "One version of one product saved by one user."

    pk_wishlist_id: Mapped[int] = pk("wishlist")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")

    __table_args__ = (
        Index(
            "uq_scd_wishlist_active",
            "fk_user_id",
            "fk_product_id",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
    )


class CompareEntry(Base, SCDMixin):
    """The product-compare list (Part I §14).

    Available to guests as well as logged-in customers, so it keys on either a
    user or a session — no login required to compare.
    """

    __tablename__ = "scd_compare_entry"
    __grain__ = "One version of one product on one user's or session's compare list."

    pk_compare_entry_id: Mapped[int] = pk("compare_entry")
    fk_user_id: Mapped[int | None] = fk("user", "scd_user.pk_user_id", nullable=True)
    session_key: Mapped[str | None] = mapped_column(String(64), index=True)
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")


class RecentlyViewed(Base, SCDMixin):
    """Recently viewed items (Part I §14)."""

    __tablename__ = "scd_recently_viewed"
    __grain__ = "One version of one recently viewed product for one user or session."

    pk_recently_viewed_id: Mapped[int] = pk("recently_viewed")
    fk_user_id: Mapped[int | None] = fk("user", "scd_user.pk_user_id", nullable=True)
    session_key: Mapped[str | None] = mapped_column(String(64), index=True)
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    viewed_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
