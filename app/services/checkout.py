"""Checkout: turning a cart into an order (Part I §8).

The order of operations here is the whole point, and it is not arbitrary.

1. **Gate the shopper.** Login is required and guest checkout is not offered
   (§8); an unverified account cannot check out (§2.5).
2. **Lock the stock, then read it.** Locking first is what closes the race where
   two customers both pass validation for the last unit (§8, and
   ``app/services/locking.py``).
3. **Price against locked reality.** Only now are discounts, promocode and
   shipping resolved — so the totals reflect the current rate and current
   discount at the moment of checkout, not whatever applied when the item was
   added (§1.1).
4. **Freeze everything onto the order.** Price, list price, cost, the USD rate,
   the product names and the shipping address are all *copied* onto the order.
   A past invoice must reprint identically years later, and margin reporting
   must not shift when a cost or a rate changes (§11, §1.1).
5. **Hold, don't deduct.** Quantities go on hold; deduction happens at
   hand-over (§8).

Everything runs in one transaction. If any step raises, the locks release and
nothing is written — no half-order, no orphaned reservation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import EmailNotVerified, NotAuthenticated, OutOfStock, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import ProductVariant, VariantOptionValue
from app.models.enums import (
    ActivityEvent,
    EmailTemplateCode,
    FulfillmentMethod,
    LineFulfillmentStatus,
    MovementKind,
    OrderStatus,
    PaymentStatus,
)
from app.models.identity import Address, Country, Province, User
from app.models.inventory import StockMovement
from app.models.orders import Cart, CartLine, Order, OrderLine
from app.services import locking, promocodes, shipping
from app.services.activity import record_event
from app.services.commerce import CartView, ShopperRef, cart_view
from app.services.pricing import current_usd_rate, q

log = get_logger(__name__)

MAX_LINE_QUANTITY = 99


@dataclass(slots=True)
class CheckoutRequest:
    """What the shopper submitted at checkout."""

    fulfillment_method: str = FulfillmentMethod.PICKUP
    pickup_branch_id: int | None = None
    address_id: int | None = None
    promocode: str | None = None
    customer_note: str | None = None


@dataclass(slots=True)
class CheckoutQuote:
    """A priced, not-yet-placed order — what the review step renders.

    Produced without locking anything, because a quote is a preview. The
    authoritative numbers are recomputed under lock in :func:`place_order`.
    """

    cart: CartView
    subtotal_amt: Decimal
    promocode_discount_amt: Decimal = Decimal("0")
    promocode_result: promocodes.PromocodeResult | None = None
    promocode_error: str | None = None
    shipping_quote: shipping.ShippingQuote = field(
        default_factory=lambda: shipping.PICKUP_QUOTE
    )

    @property
    def shipping_amt(self) -> Decimal:
        return self.shipping_quote.amount_amt

    @property
    def total_amt(self) -> Decimal:
        return q(self.subtotal_amt - self.promocode_discount_amt + self.shipping_amt)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def assert_can_check_out(user: User | None) -> User:
    """Login required, and the email must be verified (§8, §2.5)."""
    if user is None:
        raise NotAuthenticated("Please sign in to complete your order.")
    if not user.email_verified_flag:
        raise EmailNotVerified()
    return user


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------


def build_quote(
    db: Session,
    shopper: ShopperRef,
    submission: CheckoutRequest,
    *,
    now: dt.datetime | None = None,
) -> CheckoutQuote:
    """Price the cart for display. Never locks, never writes."""
    now = now or utcnow()
    view = cart_view(db, shopper)
    quote = CheckoutQuote(cart=view, subtotal_amt=view.subtotal_amt)

    if not view.lines:
        return quote

    if submission.promocode:
        try:
            result = promocodes.validate(
                db,
                submission.promocode,
                user_id=shopper.user_id,
                line_totals=_line_totals(view),
                has_item_discount=_item_discount_flags(view),
                now=now,
            )
            quote.promocode_result = result
            quote.promocode_discount_amt = result.discount_amt
        except Exception as exc:  # narrow: PromocodeInvalid carries the reason
            from app.core.errors import PromocodeInvalid

            if not isinstance(exc, PromocodeInvalid):
                raise
            # A bad code must not block checkout — surface it and carry on.
            quote.promocode_error = exc.message

    country_id, province_id = _destination(db, shopper, submission)
    quote.shipping_quote = shipping.resolve(
        db,
        method=submission.fulfillment_method,
        subtotal_amt=q(view.subtotal_amt - quote.promocode_discount_amt),
        country_id=country_id,
        province_id=province_id,
    )
    return quote


def _line_totals(view: CartView) -> dict[int, Decimal]:
    """Discounted subtotal per product — what a promocode applies against."""
    totals: dict[int, Decimal] = {}
    for line in view.lines:
        pid = line.product.pk_product_id
        totals[pid] = totals.get(pid, Decimal("0")) + line.line_total_amt
    return totals


def _item_discount_flags(view: CartView) -> dict[int, bool]:
    """Which products already carry a catalog discount — for non-stacking codes."""
    flags: dict[int, bool] = {}
    for line in view.lines:
        pid = line.product.pk_product_id
        flags[pid] = flags.get(pid, False) or line.price.has_discount
    return flags


def _destination(
    db: Session, shopper: ShopperRef, submission: CheckoutRequest
) -> tuple[int | None, int | None]:
    if submission.fulfillment_method != FulfillmentMethod.SHIPPING:
        return None, None
    address = _resolve_address(db, shopper, submission.address_id)
    if address is None:
        return None, None
    return address.fk_country_id, address.fk_province_id


def _resolve_address(
    db: Session, shopper: ShopperRef, address_id: int | None
) -> Address | None:
    """The chosen address, or the customer's default. Always scoped to them —
    an address id from another account must never resolve."""
    if shopper.user_id is None:
        return None
    stmt = select(Address).where(
        Address.fk_user_id == shopper.user_id,
        Address.scd_active_flag.is_(True),
    )
    if address_id is not None:
        stmt = stmt.where(Address.pk_address_id == address_id)
    else:
        stmt = stmt.order_by(Address.is_default_flag.desc(), Address.pk_address_id)
    return db.scalars(stmt).first()


# ---------------------------------------------------------------------------
# Placing the order
# ---------------------------------------------------------------------------


def place_order(
    db: Session,
    request: Request,
    shopper: ShopperRef,
    submission: CheckoutRequest,
    *,
    now: dt.datetime | None = None,
) -> Order:
    """Convert the cart into a placed order, holding stock as it goes.

    Runs inside the caller's transaction. The stock locks taken here are held
    until that transaction commits, so the caller should commit promptly.
    """
    now = now or utcnow()

    user = db.get(User, shopper.user_id) if shopper.user_id else None
    user = assert_can_check_out(user)

    view = cart_view(db, shopper)
    if not view.lines:
        raise ValidationFailed("Your cart is empty.")

    for line in view.lines:
        if line.line.quantity < 1 or line.line.quantity > MAX_LINE_QUANTITY:
            raise ValidationFailed(
                f"Quantity for {line.product.name_en} must be between 1 and {MAX_LINE_QUANTITY}."
            )

    # --- 1. Lock first, then read. ----------------------------------------
    variant_ids = [line.variant.pk_product_variant_id for line in view.lines]
    levels = locking.lock_stock_levels(db, variant_ids)
    sellable = locking.sellable_by_variant(levels)

    for line in view.lines:
        variant_id = line.variant.pk_product_variant_id
        if sellable.get(variant_id, 0) < line.line.quantity:
            raise OutOfStock(
                f"{line.product.name_en} no longer has enough stock.",
                details={
                    "product_id": line.product.pk_product_id,
                    "requested": line.line.quantity,
                    "available": sellable.get(variant_id, 0),
                },
            )

    # --- 2. Price against locked reality. ---------------------------------
    quote = build_quote(db, shopper, submission, now=now)
    address = (
        _resolve_address(db, shopper, submission.address_id)
        if submission.fulfillment_method == FulfillmentMethod.SHIPPING
        else None
    )
    if submission.fulfillment_method == FulfillmentMethod.SHIPPING and address is None:
        raise ValidationFailed("Choose a shipping address before placing the order.")

    # --- 3. Freeze the rate, then build the order. ------------------------
    rate = current_usd_rate(db)
    order = Order(
        order_number=_next_order_number(db, now),
        fk_user_id=user.pk_user_id,
        status=OrderStatus.PLACED,
        payment_status=PaymentStatus.NOT_PAID,
        placed_dt=now,
        subtotal_amt=quote.subtotal_amt,
        item_discount_amt=_item_discount_total(view),
        promocode_discount_amt=quote.promocode_discount_amt,
        shipping_amt=quote.shipping_amt,
        total_amt=quote.total_amt,
        fk_promocode_id=(
            quote.promocode_result.promocode.pk_promocode_id
            if quote.promocode_result
            else None
        ),
        display_currency=_display_currency(request),
        usd_rate_at_sale=rate,
        fulfillment_method=submission.fulfillment_method,
        fk_pickup_branch_id=(
            submission.pickup_branch_id
            if submission.fulfillment_method == FulfillmentMethod.PICKUP
            else None
        ),
        shipping_quote_pending_flag=quote.shipping_quote.quote_on_contact,
        customer_note=(submission.customer_note or None),
        scd_active_from=now,
    )
    _copy_shipping_address(db, order, address)
    db.add(order)
    db.flush()

    # --- 4. Lines, with everything frozen onto them. ----------------------
    labels = _variant_labels(db, variant_ids)
    for line in view.lines:
        variant = line.variant
        allocation = locking.reserve(
            db,
            levels,
            variant_id=variant.pk_product_variant_id,
            quantity=line.line.quantity,
        )
        # A line records the pool it will mostly be picked from; the movement
        # rows below carry the exact per-pool split.
        primary_pool_id = allocation[0][0].fk_stock_pool_id if allocation else None
        average_cost = allocation[0][0].average_cost_amt if allocation else Decimal("0")

        order_line = OrderLine(
            fk_order_id=order.pk_order_id,
            fk_product_variant_id=variant.pk_product_variant_id,
            fk_stock_pool_id=primary_pool_id,
            quantity=line.line.quantity,
            list_price_amt=line.price.list_amt,
            unit_price_amt=line.price.final_amt,
            unit_cost_amt=q(Decimal(average_cost)),
            line_total_amt=line.line_total_amt,
            product_name_ar=line.product.name_ar,
            product_name_en=line.product.name_en,
            variant_label_ar=labels.get(variant.pk_product_variant_id, ("", ""))[0] or None,
            variant_label_en=labels.get(variant.pk_product_variant_id, ("", ""))[1] or None,
            sku=variant.sku,
            fulfillment_method=submission.fulfillment_method,
            fulfillment_status=(
                LineFulfillmentStatus.ORDERED_FOR_PICKUP
                if submission.fulfillment_method == FulfillmentMethod.PICKUP
                else LineFulfillmentStatus.ORDERED_FOR_DELIVERY
            ),
            stock_held_flag=True,
            # Tagged now so the revenue split can be computed when the sale is
            # finalised at hand-over (Part I §7). Null for ordinary owned stock.
            fk_consignment_item_id=_consignment_item_id(
                db, variant.pk_product_variant_id, primary_pool_id
            ),
            scd_active_from=now,
        )
        db.add(order_line)
        db.flush()

        # --- 5. Hold, don't deduct. --------------------------------------
        for level, units in allocation:
            db.add(
                StockMovement(
                    fk_product_variant_id=variant.pk_product_variant_id,
                    fk_stock_pool_id=level.fk_stock_pool_id,
                    movement_kind=MovementKind.RESERVATION_HOLD,
                    # Positive: this *adds to the reserved pool*. On-hand is
                    # untouched until hand-over, so reservation movements are
                    # summed separately from on-hand ones — and summing them
                    # must reproduce quantity_reserved, which is why a hold is
                    # +units and a release is -units
                    # (see RESERVATION_MOVEMENT_KINDS).
                    quantity_delta=units,
                    unit_cost_amt=level.average_cost_amt,
                    fk_order_line_id=order_line.pk_order_line_id,
                    note=f"Held for order {order.order_number}",
                    created_dt=now,
                    created_by=user.pk_user_id,
                )
            )
            level.last_movement_dt = now

    # --- 6. Record the redemption and retire the cart. --------------------
    if quote.promocode_result is not None:
        promocodes.record_redemption(
            db,
            quote.promocode_result.promocode,
            order_id=order.pk_order_id,
            user_id=user.pk_user_id,
            discount_amt=quote.promocode_discount_amt,
        )

    _convert_cart(db, view, order, user_id=user.pk_user_id, now=now)

    record_event(
        db,
        ActivityEvent.CART_CONVERTED,
        request=request,
        target_table="scd_order",
        target_row_id=order.pk_order_id,
        quantity=view.item_count,
        success=True,
    )
    _queue_confirmation_email(db, order, user)

    log.info(
        "order_placed",
        extra={
            "order_number": order.order_number,
            "user_id": user.pk_user_id,
            "lines": len(view.lines),
            "total_amt": str(order.total_amt),
        },
    )
    return order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consignment_item_id(
    db: Session, variant_id: int, stock_pool_id: int | None
) -> int | None:
    """The open consignment holding for this variant, if any (Part I §7)."""
    from app.services.consignment import item_for_variant

    item = item_for_variant(db, variant_id, stock_pool_id=stock_pool_id)
    return item.pk_consignment_item_id if item else None


def _item_discount_total(view: CartView) -> Decimal:
    """How much catalog discount the basket already carries, for reporting."""
    total = Decimal("0")
    for line in view.lines:
        if line.price.has_discount:
            total += line.price.saved_amt * line.line.quantity
    return q(total)


def _display_currency(request: Request) -> str:
    from app.core.context import get_context

    return get_context(request).currency


def _next_order_number(db: Session, now: dt.datetime) -> str:
    """``JEC-YYMMDD-NNN`` — readable at the counter, unique per day.

    Sequential rather than random because staff read these aloud on the phone.
    The uniqueness loop covers two orders placed in the same instant.
    """
    prefix = f"JEC-{now:%y%m%d}"
    placed_today = db.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.order_number.like(f"{prefix}-%"))
    ) or 0

    for offset in range(1, 200):
        candidate = f"{prefix}-{placed_today + offset:03d}"
        exists = db.scalars(
            select(Order.pk_order_id).where(Order.order_number == candidate)
        ).first()
        if exists is None:
            return candidate

    raise ValidationFailed("Could not allocate an order number. Please try again.")


def _copy_shipping_address(db: Session, order: Order, address: Address | None) -> None:
    """Copy the address onto the order rather than referencing it.

    Editing a saved address later must never rewrite where a past order was
    actually delivered (Part I §8).
    """
    if address is None:
        return
    country = db.get(Country, address.fk_country_id)
    province = db.get(Province, address.fk_province_id)
    order.ship_country_name = country.name_en if country else None
    order.ship_province_name = province.name_en if province else None
    order.ship_city = address.city
    order.ship_address_line = address.address_line
    order.ship_zip_code = address.zip_code
    order.ship_po_box = address.po_box


def _variant_labels(db: Session, variant_ids: list[int]) -> dict[int, tuple[str, str]]:
    """Human-readable ``"Colour: Black · Size: XL"`` per variant, AR and EN.

    Batched into one query — an order with ten lines should not cost ten
    round trips (Part II §2).
    """
    if not variant_ids:
        return {}

    rows = db.scalars(
        select(VariantOptionValue)
        .where(
            VariantOptionValue.fk_product_variant_id.in_(variant_ids),
            VariantOptionValue.scd_active_flag.is_(True),
        )
        .order_by(VariantOptionValue.fk_product_variant_id)
    ).all()

    parts: dict[int, list[tuple[str, str]]] = {}
    for row in rows:
        option, choice = row.option, row.choice
        if option is None or choice is None:
            continue
        parts.setdefault(row.fk_product_variant_id, []).append(
            (
                f"{option.name_ar}: {choice.value_ar}",
                f"{option.name_en}: {choice.value_en}",
            )
        )

    return {
        variant_id: (
            " · ".join(ar for ar, _ in pairs),
            " · ".join(en for _, en in pairs),
        )
        for variant_id, pairs in parts.items()
    }


def _convert_cart(
    db: Session, view: CartView, order: Order, *, user_id: int, now: dt.datetime
) -> None:
    """Retire the cart. Closed, never deleted (Part II §1)."""
    if view.cart is None:
        return
    view.cart.converted_order_id = order.pk_order_id
    view.cart.last_activity_dt = now
    for line in view.lines:
        line.line.close(changed_by=user_id, at=now)
    view.cart.close(changed_by=user_id, at=now)


def _queue_confirmation_email(db: Session, order: Order, user: User) -> None:
    from app.services.email import queue_template_email

    queue_template_email(
        db,
        EmailTemplateCode.ORDER_CONFIRMATION,
        recipient=user.email,
        language=user.preferred_language,
        # Keyed on the order, so a retried placement can never send twice.
        idempotency_key=f"order_confirmation:{order.pk_order_id}",
        params={
            "order_number": order.order_number,
            "total": f"{order.total_amt} JOD",
            "username": user.username,
        },
    )
