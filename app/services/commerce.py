"""Cart and compare-list helpers for the storefront (Part I §8, §14).

This module owns the cart while it is still a *wish*: adding, changing
quantities, removing lines, remembering a promocode. It deliberately does not
validate stock — the cart is not a claim on inventory. Turning that wish into a
claim is `app/services/checkout.py`, which locks stock first and prices
everything against locked reality.

Nothing here stores a price, either. That is what makes the Part I §1.1 rule
true in practice: an item sitting in an open cart always reflects the current
rate and current discount at the moment of checkout, not whatever applied when
it was added.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fastapi import Request, Response
from sqlalchemy import false, func, select
from sqlalchemy.orm import Session

from app.core.context import GUEST_SESSION_COOKIE, get_context
from app.core.errors import Conflict, NotFound, OutOfStock, ValidationFailed
from app.core.security import generate_token
from app.db.base import utcnow
from app.models.catalog import Product, ProductVariant
from app.models.enums import ActivityEvent
from app.models.orders import Cart, CartLine, CompareEntry
from app.services.activity import record_event
from app.services.catalog import AvailabilityStatus, availability_for_products
from app.services.pricing import PriceView, live_discounts_for_products, q, resolve_price

GUEST_COOKIE_MAX_AGE = 60 * 60 * 24 * 365
COMPARE_LIMIT = 4
#: Per-line ceiling. Anything larger is a wholesale enquiry, not a web order.
MAX_LINE_QUANTITY = 99


@dataclass(slots=True)
class ShopperRef:
    user_id: int | None
    session_key: str | None
    new_guest_key: str | None = None


@dataclass(slots=True)
class CartLineView:
    line: CartLine
    variant: ProductVariant
    product: Product
    price: PriceView
    line_total_amt: Decimal


@dataclass(slots=True)
class CartView:
    cart: Cart | None
    lines: list[CartLineView]
    subtotal_amt: Decimal
    item_count: int


def shopper_ref(request: Request, *, create_guest: bool = False) -> ShopperRef:
    """Resolve the current shopper to a user id or anonymous key."""
    context = get_context(request)
    if context.user is not None:
        return ShopperRef(user_id=context.user.pk_user_id, session_key=None)

    session_key = context.session_key or request.cookies.get(GUEST_SESSION_COOKIE)
    new_key = None
    if create_guest and not session_key:
        new_key = generate_token()
        session_key = new_key
        context.session_key = new_key
    return ShopperRef(user_id=None, session_key=session_key, new_guest_key=new_key)


def attach_guest_cookie(response: Response, shopper: ShopperRef) -> None:
    if not shopper.new_guest_key:
        return
    response.set_cookie(
        GUEST_SESSION_COOKIE,
        shopper.new_guest_key,
        max_age=GUEST_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _owner_filter(model, shopper: ShopperRef):
    if shopper.user_id is not None:
        return model.fk_user_id == shopper.user_id
    if shopper.session_key:
        return model.session_key == shopper.session_key
    return false()


def active_cart(db: Session, shopper: ShopperRef) -> Cart | None:
    if shopper.user_id is None and shopper.session_key is None:
        return None
    return db.scalars(
        select(Cart)
        .where(
            _owner_filter(Cart, shopper),
            Cart.converted_order_id.is_(None),
            Cart.scd_active_flag.is_(True),
        )
        .order_by(Cart.pk_cart_id.desc())
    ).first()


def get_or_create_cart(db: Session, shopper: ShopperRef) -> Cart:
    cart = active_cart(db, shopper)
    if cart is not None:
        return cart
    if shopper.user_id is None and shopper.session_key is None:
        raise ValidationFailed("A cart needs a signed-in user or an anonymous session.")
    cart = Cart(
        fk_user_id=shopper.user_id,
        session_key=shopper.session_key,
        last_activity_dt=utcnow(),
        scd_active_from=utcnow(),
    )
    db.add(cart)
    db.flush()
    return cart


def cart_count(db: Session, shopper: ShopperRef) -> int:
    cart = active_cart(db, shopper)
    if cart is None:
        return 0
    return db.scalar(
        select(func.coalesce(func.sum(CartLine.quantity), 0)).where(
            CartLine.fk_cart_id == cart.pk_cart_id,
            CartLine.scd_active_flag.is_(True),
        )
    ) or 0


def cart_view(db: Session, shopper: ShopperRef) -> CartView:
    cart = active_cart(db, shopper)
    if cart is None:
        return CartView(cart=None, lines=[], subtotal_amt=Decimal("0"), item_count=0)

    rows = db.execute(
        select(CartLine, ProductVariant, Product)
        .join(ProductVariant, ProductVariant.pk_product_variant_id == CartLine.fk_product_variant_id)
        .join(Product, Product.pk_product_id == ProductVariant.fk_product_id)
        .where(
            CartLine.fk_cart_id == cart.pk_cart_id,
            CartLine.scd_active_flag.is_(True),
            ProductVariant.scd_active_flag.is_(True),
            ProductVariant.is_active_flag.is_(True),
            Product.scd_active_flag.is_(True),
            Product.is_visible_flag.is_(True),
        )
        .order_by(CartLine.added_dt, CartLine.pk_cart_line_id)
    ).all()

    product_ids = [product.pk_product_id for _, _, product in rows]
    discounts = live_discounts_for_products(db, product_ids)
    lines: list[CartLineView] = []
    subtotal = Decimal("0")
    item_count = 0
    for line, variant, product in rows:
        list_amt = variant.price_override_amt or product.base_price_amt
        price = resolve_price(
            list_amt,
            discounts.get(product.pk_product_id, []),
            product.discount_overlap_rule,
        )
        line_total = q(price.final_amt * line.quantity)
        subtotal += line_total
        item_count += line.quantity
        lines.append(
            CartLineView(
                line=line,
                variant=variant,
                product=product,
                price=price,
                line_total_amt=line_total,
            )
        )

    return CartView(cart=cart, lines=lines, subtotal_amt=q(subtotal), item_count=item_count)


def add_to_cart(
    db: Session,
    request: Request,
    shopper: ShopperRef,
    *,
    product_id: int,
    variant_id: int | None = None,
    quantity: int = 1,
) -> Cart:
    quantity = max(1, min(quantity, MAX_LINE_QUANTITY))
    product = db.scalars(
        select(Product).where(
            Product.pk_product_id == product_id,
            Product.scd_active_flag.is_(True),
            Product.is_visible_flag.is_(True),
        )
    ).first()
    if product is None:
        raise NotFound()

    variant_stmt = select(ProductVariant).where(
        ProductVariant.fk_product_id == product.pk_product_id,
        ProductVariant.scd_active_flag.is_(True),
        ProductVariant.is_active_flag.is_(True),
    )
    if variant_id is not None:
        variant_stmt = variant_stmt.where(ProductVariant.pk_product_variant_id == variant_id)
    variant = db.scalars(
        variant_stmt.order_by(ProductVariant.sort_order, ProductVariant.pk_product_variant_id)
    ).first()
    if variant is None:
        raise NotFound("The selected product variant does not exist.")

    availability = availability_for_products(db, [product.pk_product_id])[product.pk_product_id]
    if availability.status == AvailabilityStatus.OUT_OF_STOCK:
        raise OutOfStock()

    cart = get_or_create_cart(db, shopper)
    line = db.scalars(
        select(CartLine).where(
            CartLine.fk_cart_id == cart.pk_cart_id,
            CartLine.fk_product_variant_id == variant.pk_product_variant_id,
            CartLine.scd_active_flag.is_(True),
        )
    ).first()
    if line is None:
        db.add(
            CartLine(
                fk_cart_id=cart.pk_cart_id,
                fk_product_variant_id=variant.pk_product_variant_id,
                quantity=quantity,
                added_dt=utcnow(),
                scd_active_from=utcnow(),
            )
        )
    else:
        line.quantity = min(line.quantity + quantity, MAX_LINE_QUANTITY)
    cart.last_activity_dt = utcnow()
    record_event(
        db,
        ActivityEvent.CART_ITEM_ADDED,
        request=request,
        context=get_context(request),
        target_table="scd_product_variant",
        target_row_id=variant.pk_product_variant_id,
        quantity=quantity,
        success=True,
    )
    return cart


def update_line_quantity(
    db: Session,
    request: Request,
    shopper: ShopperRef,
    *,
    line_id: int,
    quantity: int,
) -> None:
    """Set a line's quantity, or remove it when the quantity reaches zero.

    Deliberately does not check stock: the cart is a wish, not a claim. Stock is
    validated and locked at checkout (Part I §8), and blocking a shopper from
    typing "5" because only 4 are left — when another cart might release one
    before they check out — is friction for no gain.
    """
    line = _own_cart_line(db, shopper, line_id)
    if quantity <= 0:
        remove_line(db, request, shopper, line_id=line_id)
        return

    line.quantity = min(quantity, MAX_LINE_QUANTITY)
    if line.cart is not None:
        line.cart.last_activity_dt = utcnow()


def remove_line(
    db: Session, request: Request, shopper: ShopperRef, *, line_id: int
) -> None:
    """Close the line. Never deleted (Part II §1) — cart history is behavioural
    data the abandonment dashboards read (Part I §2.8)."""
    line = _own_cart_line(db, shopper, line_id)
    variant_id = line.fk_product_variant_id
    line.close(changed_by=shopper.user_id)
    if line.cart is not None:
        line.cart.last_activity_dt = utcnow()

    record_event(
        db,
        ActivityEvent.CART_ITEM_REMOVED,
        request=request,
        context=get_context(request),
        target_table="scd_product_variant",
        target_row_id=variant_id,
        success=True,
    )


def _own_cart_line(db: Session, shopper: ShopperRef, line_id: int) -> CartLine:
    """Fetch a cart line, scoped to this shopper.

    The join to ``Cart`` is the security boundary: a line id belonging to
    somebody else's cart must resolve to nothing, not to their line.
    """
    line = db.scalars(
        select(CartLine)
        .join(Cart, Cart.pk_cart_id == CartLine.fk_cart_id)
        .where(
            CartLine.pk_cart_line_id == line_id,
            CartLine.scd_active_flag.is_(True),
            _owner_filter(Cart, shopper),
            Cart.converted_order_id.is_(None),
            Cart.scd_active_flag.is_(True),
        )
    ).first()
    if line is None:
        raise NotFound("That cart item no longer exists.")
    return line


def set_cart_promocode(db: Session, shopper: ShopperRef, code: str | None) -> None:
    """Remember the shopper's code on the cart.

    Stored, not applied: the discount is recomputed at checkout against the
    limits and expiry as they stand *then* (Part I §1.1, §13). Saving it here
    only means they do not have to retype it.
    """
    cart = active_cart(db, shopper)
    if cart is None:
        return

    if not code or not code.strip():
        cart.fk_promocode_id = None
        return

    from app.services.promocodes import find_active

    promocode = find_active(db, code)
    cart.fk_promocode_id = promocode.pk_promocode_id if promocode else None


def cart_promocode(db: Session, shopper: ShopperRef) -> str | None:
    """The code saved on the cart, if any."""
    cart = active_cart(db, shopper)
    if cart is None or cart.fk_promocode_id is None:
        return None
    from app.models.marketing import Promocode

    promocode = db.get(Promocode, cart.fk_promocode_id)
    return promocode.code if promocode else None


def compare_products(db: Session, shopper: ShopperRef) -> list[Product]:
    if shopper.user_id is None and shopper.session_key is None:
        return []
    product_ids = select(CompareEntry.fk_product_id).where(
        _owner_filter(CompareEntry, shopper),
        CompareEntry.scd_active_flag.is_(True),
    )
    return list(
        db.scalars(
            select(Product)
            .where(
                Product.pk_product_id.in_(product_ids),
                Product.scd_active_flag.is_(True),
                Product.is_visible_flag.is_(True),
            )
            .order_by(Product.pk_product_id.desc())
        ).all()
    )


def compare_count(db: Session, shopper: ShopperRef) -> int:
    if shopper.user_id is None and shopper.session_key is None:
        return 0
    return db.scalar(
        select(func.count()).select_from(CompareEntry).where(
            _owner_filter(CompareEntry, shopper),
            CompareEntry.scd_active_flag.is_(True),
        )
    ) or 0


def toggle_compare(db: Session, shopper: ShopperRef, *, product_id: int) -> bool:
    product = db.scalars(
        select(Product).where(
            Product.pk_product_id == product_id,
            Product.scd_active_flag.is_(True),
            Product.is_visible_flag.is_(True),
        )
    ).first()
    if product is None:
        raise NotFound()

    existing = db.scalars(
        select(CompareEntry).where(
            _owner_filter(CompareEntry, shopper),
            CompareEntry.fk_product_id == product.pk_product_id,
            CompareEntry.scd_active_flag.is_(True),
        )
    ).first()
    if existing is not None:
        existing.close(changed_by=shopper.user_id)
        return False

    if compare_count(db, shopper) >= COMPARE_LIMIT:
        raise Conflict(f"Compare is limited to {COMPARE_LIMIT} products.")

    db.add(
        CompareEntry(
            fk_user_id=shopper.user_id,
            session_key=shopper.session_key,
            fk_product_id=product.pk_product_id,
            scd_active_from=utcnow(),
        )
    )
    return True
