"""Checkout, stock locking and promocode behaviour (Part I §8, §13).

The centrepiece is :func:`test_concurrent_checkout_cannot_oversell`. Part I §8
calls out that re-validating stock is not enough — two customers can both pass
validation for the last unit inside the same window. That is precisely what this
test reproduces, and it is the reason ``app/services/locking.py`` exists.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import EmailNotVerified, NotAuthenticated, OutOfStock, PromocodeInvalid
from app.db.base import Base, utcnow
from app.models.catalog import Category, Product, ProductCategory, ProductVariant
from app.models.enums import (
    FulfillmentMethod,
    MovementKind,
    PromocodeKind,
    RoleCode,
    StockPoolKind,
)
from app.models.identity import Role, User
from app.models.inventory import StockLevel, StockMovement, StockPool
from app.models.marketing import Promocode
from app.models.money import ExchangeRate
from app.models.orders import Cart, CartLine, Order, OrderLine
from app.services import locking, promocodes
from app.services.checkout import CheckoutRequest, assert_can_check_out, place_order
from app.services.commerce import ShopperRef


# ---------------------------------------------------------------------------
# Fixtures — a minimal but real store
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Session:
    """A fresh in-memory database per test, on one shared connection.

    ``StaticPool`` keeps every session on the same connection so ``:memory:``
    survives across them.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def store(db: Session) -> dict:
    """One customer, one product with one variant, and 3 units in stock."""
    now = utcnow()

    db.add(ExchangeRate(jod_to_usd_rate=Decimal("1.41"), scd_active_from=now))
    role = Role(
        role_code=RoleCode.CUSTOMER, name_ar="زبون", name_en="Customer",
        scd_active_from=now,
    )
    db.add(role)
    db.flush()

    user = User(
        fk_role_id=role.pk_role_id,
        username="shopper",
        email="shopper@example.com",
        password_hash="x",
        email_verified_flag=True,
        scd_active_from=now,
    )
    db.add(user)

    category = Category(
        name_ar="كتب", name_en="Books", slug_ar="كتب", slug_en="books",
        ancestor_path="/", scd_active_from=now,
    )
    db.add(category)
    db.flush()

    product = Product(
        name_ar="كتاب", name_en="Book",
        slug_ar="كتاب", slug_en="book",
        base_price_amt=Decimal("20.000"),
        published_dt=now,
        scd_active_from=now,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductCategory(
            fk_product_id=product.pk_product_id,
            fk_category_id=category.pk_category_id,
            is_primary_flag=True,
            scd_active_from=now,
        )
    )

    variant = ProductVariant(
        fk_product_id=product.pk_product_id, sku="SKU-1", scd_active_from=now
    )
    db.add(variant)

    pool = StockPool(
        pool_kind=StockPoolKind.BRANCH, name_ar="الفرع", name_en="Branch",
        is_sellable_flag=True, is_owned_flag=True, scd_active_from=now,
    )
    db.add(pool)
    db.flush()

    db.add(
        StockLevel(
            fk_product_variant_id=variant.pk_product_variant_id,
            fk_stock_pool_id=pool.pk_stock_pool_id,
            quantity_on_hand=3,
            quantity_reserved=0,
            average_cost_amt=Decimal("12.000"),
            scd_active_from=now,
        )
    )
    db.commit()

    return {"user": user, "product": product, "variant": variant, "pool": pool}


def _cart(db: Session, store: dict, *, user: User, quantity: int) -> Cart:
    now = utcnow()
    cart = Cart(fk_user_id=user.pk_user_id, last_activity_dt=now, scd_active_from=now)
    db.add(cart)
    db.flush()
    db.add(
        CartLine(
            fk_cart_id=cart.pk_cart_id,
            fk_product_variant_id=store["variant"].pk_product_variant_id,
            quantity=quantity,
            added_dt=now,
            scd_active_from=now,
        )
    )
    db.commit()
    return cart


class _FakeRequest:
    """Enough of a Request for the activity log and currency lookup."""

    def __init__(self) -> None:
        from app.core.context import RequestContext

        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.client = None
        self.url = type("U", (), {"path": "/checkout"})()
        self.state = type("S", (), {})()
        self.state.context = RequestContext(
            language="en", currency="JOD", usd_rate=Decimal("1.41")
        )


# ---------------------------------------------------------------------------
# Gating (Part I §8, §2.5)
# ---------------------------------------------------------------------------


def test_anonymous_shopper_cannot_check_out():
    """Guest checkout is not offered (Part I §8)."""
    with pytest.raises(NotAuthenticated):
        assert_can_check_out(None)


def test_unverified_account_cannot_check_out(db: Session, store: dict):
    """An unverified account persists indefinitely but cannot order (§2.5)."""
    user = store["user"]
    user.email_verified_flag = False
    with pytest.raises(EmailNotVerified):
        assert_can_check_out(user)


# ---------------------------------------------------------------------------
# The race condition (Part I §8)
# ---------------------------------------------------------------------------


def test_reserve_holds_without_deducting_on_hand(db: Session, store: dict):
    """Checkout puts stock on hold; it is deducted only at hand-over (§8)."""
    variant_id = store["variant"].pk_product_variant_id
    levels = locking.lock_stock_levels(db, [variant_id])

    locking.reserve(db, levels, variant_id=variant_id, quantity=2)
    db.commit()

    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_on_hand == 3, "on-hand must not move until hand-over"
    assert level.quantity_reserved == 2
    assert level.quantity_sellable == 1


def test_reserve_refuses_to_oversell(db: Session, store: dict):
    variant_id = store["variant"].pk_product_variant_id
    levels = locking.lock_stock_levels(db, [variant_id])

    with pytest.raises(OutOfStock):
        locking.reserve(db, levels, variant_id=variant_id, quantity=4)


def test_concurrent_checkout_cannot_oversell(db: Session, store: dict):
    """Two shoppers, three units, two carts of two. Exactly one must win.

    This is the scenario Part I §8 describes: both carts pass a naive
    availability check (3 >= 2), so without locking both orders would be
    written and the store would owe four units it does not have.
    """
    user_a = store["user"]
    now = utcnow()
    user_b = User(
        fk_role_id=user_a.fk_role_id,
        username="shopper2",
        email="shopper2@example.com",
        password_hash="x",
        email_verified_flag=True,
        scd_active_from=now,
    )
    db.add(user_b)
    db.commit()

    _cart(db, store, user=user_a, quantity=2)
    _cart(db, store, user=user_b, quantity=2)

    request = _FakeRequest()
    submission = CheckoutRequest(fulfillment_method=FulfillmentMethod.PICKUP)

    placed, rejected = 0, 0
    for user in (user_a, user_b):
        try:
            place_order(db, request, ShopperRef(user_id=user.pk_user_id, session_key=None), submission)
            db.commit()
            placed += 1
        except OutOfStock:
            db.rollback()
            rejected += 1

    assert (placed, rejected) == (1, 1), "exactly one of the two orders must succeed"

    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_reserved == 2
    assert level.quantity_sellable == 1, "the third unit stays sellable"


# ---------------------------------------------------------------------------
# Order creation (Part I §8, §11, §1.1)
# ---------------------------------------------------------------------------


def test_place_order_freezes_price_cost_and_rate(db: Session, store: dict):
    """Price, cost and the USD rate are copied onto the order (§11, §1.1)."""
    user = store["user"]
    _cart(db, store, user=user, quantity=2)

    order = place_order(
        db,
        _FakeRequest(),
        ShopperRef(user_id=user.pk_user_id, session_key=None),
        CheckoutRequest(fulfillment_method=FulfillmentMethod.PICKUP),
    )
    db.commit()

    assert order.order_number.startswith("JEC-")
    assert order.usd_rate_at_sale == Decimal("1.410000")
    assert order.subtotal_amt == Decimal("40.000")

    line = db.scalars(select(OrderLine)).one()
    assert line.unit_price_amt == Decimal("20.000")
    assert line.list_price_amt == Decimal("20.000")
    assert line.unit_cost_amt == Decimal("12.000"), "cost frozen at time of sale"
    assert line.stock_held_flag is True

    # Later price changes must not rewrite the placed order.
    store["product"].base_price_amt = Decimal("99.000")
    db.commit()
    db.refresh(line)
    assert line.unit_price_amt == Decimal("20.000")


def test_place_order_writes_reservation_movements(db: Session, store: dict):
    """The hold is recorded in the insert-only ledger, negative-signed."""
    user = store["user"]
    _cart(db, store, user=user, quantity=2)

    place_order(
        db,
        _FakeRequest(),
        ShopperRef(user_id=user.pk_user_id, session_key=None),
        CheckoutRequest(fulfillment_method=FulfillmentMethod.PICKUP),
    )
    db.commit()

    movement = db.scalars(select(StockMovement)).one()
    assert movement.movement_kind == MovementKind.RESERVATION_HOLD
    # Positive: a hold adds to the *reserved* pool, so summing the reservation
    # movements reproduces quantity_reserved exactly.
    assert movement.quantity_delta == 2

    level = db.scalars(select(StockLevel)).one()
    assert movement.quantity_delta == level.quantity_reserved


def test_placing_an_order_retires_the_cart(db: Session, store: dict):
    """The cart is closed and linked to the order — never deleted (Part II §1)."""
    user = store["user"]
    cart = _cart(db, store, user=user, quantity=1)

    order = place_order(
        db,
        _FakeRequest(),
        ShopperRef(user_id=user.pk_user_id, session_key=None),
        CheckoutRequest(fulfillment_method=FulfillmentMethod.PICKUP),
    )
    db.commit()
    db.refresh(cart)

    assert cart.converted_order_id == order.pk_order_id
    assert cart.scd_active_flag is False
    assert cart.scd_active_to is not None


def test_empty_cart_is_rejected(db: Session, store: dict):
    from app.core.errors import ValidationFailed

    user = store["user"]
    with pytest.raises(ValidationFailed):
        place_order(
            db,
            _FakeRequest(),
            ShopperRef(user_id=user.pk_user_id, session_key=None),
            CheckoutRequest(),
        )


# ---------------------------------------------------------------------------
# Promocodes (Part I §13)
# ---------------------------------------------------------------------------


@pytest.fixture
def promocode(db: Session) -> Promocode:
    code = Promocode(
        code="SAVE10",
        promocode_kind=PromocodeKind.PERCENTAGE,
        percentage=Decimal("10"),
        scd_active_from=utcnow(),
    )
    db.add(code)
    db.commit()
    return code


def test_percentage_promocode_applies(db: Session, store: dict, promocode: Promocode):
    result = promocodes.validate(
        db, "save10", user_id=None,
        line_totals={store["product"].pk_product_id: Decimal("40.000")},
    )
    assert result.discount_amt == Decimal("4.000")


def test_promocode_lookup_is_case_insensitive(db: Session, store: dict, promocode: Promocode):
    assert promocodes.find_active(db, "  save10 ") is not None


def test_capped_promocode_respects_its_ceiling(db: Session, store: dict):
    db.add(
        Promocode(
            code="CAP",
            promocode_kind=PromocodeKind.PERCENTAGE_CAPPED,
            percentage=Decimal("50"),
            max_discount_amt=Decimal("5.000"),
            scd_active_from=utcnow(),
        )
    )
    db.commit()
    result = promocodes.validate(
        db, "CAP", user_id=None,
        line_totals={store["product"].pk_product_id: Decimal("100.000")},
    )
    assert result.discount_amt == Decimal("5.000"), "50% of 100 capped at 5"


def test_flat_promocode_never_exceeds_the_basket(db: Session, store: dict):
    """A promocode is a discount, never a payout."""
    db.add(
        Promocode(
            code="TENOFF",
            promocode_kind=PromocodeKind.FIXED_AMOUNT,
            fixed_amount_amt=Decimal("10.000"),
            scd_active_from=utcnow(),
        )
    )
    db.commit()
    result = promocodes.validate(
        db, "TENOFF", user_id=None,
        line_totals={store["product"].pk_product_id: Decimal("6.000")},
    )
    assert result.discount_amt == Decimal("6.000")


def test_expired_promocode_is_rejected(db: Session, store: dict):
    db.add(
        Promocode(
            code="OLD",
            promocode_kind=PromocodeKind.PERCENTAGE,
            percentage=Decimal("10"),
            expires_dt=utcnow() - dt.timedelta(days=1),
            scd_active_from=utcnow(),
        )
    )
    db.commit()
    with pytest.raises(PromocodeInvalid):
        promocodes.validate(
            db, "OLD", user_id=None,
            line_totals={store["product"].pk_product_id: Decimal("40.000")},
        )


def test_minimum_order_is_enforced(db: Session, store: dict):
    db.add(
        Promocode(
            code="BIG",
            promocode_kind=PromocodeKind.PERCENTAGE,
            percentage=Decimal("10"),
            minimum_order_amt=Decimal("50.000"),
            scd_active_from=utcnow(),
        )
    )
    db.commit()
    with pytest.raises(PromocodeInvalid):
        promocodes.validate(
            db, "BIG", user_id=None,
            line_totals={store["product"].pk_product_id: Decimal("40.000")},
        )


def test_single_use_globally_blocks_a_second_use(db: Session, store: dict):
    """Distinct from the per-customer cap (Part I §13)."""
    code = Promocode(
        code="ONCE",
        promocode_kind=PromocodeKind.PERCENTAGE,
        percentage=Decimal("10"),
        single_use_globally_flag=True,
        scd_active_from=utcnow(),
    )
    db.add(code)
    db.flush()

    _cart(db, store, user=store["user"], quantity=1)
    order = place_order(
        db,
        _FakeRequest(),
        ShopperRef(user_id=store["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    promocodes.record_redemption(
        db, code, order_id=order.pk_order_id,
        user_id=store["user"].pk_user_id, discount_amt=Decimal("2.000"),
    )
    db.commit()

    with pytest.raises(PromocodeInvalid):
        promocodes.validate(
            db, "ONCE", user_id=None,
            line_totals={store["product"].pk_product_id: Decimal("40.000")},
        )


# ---------------------------------------------------------------------------
# Shipping (Part I §2.2)
# ---------------------------------------------------------------------------


@pytest.fixture
def shipping_rules(db: Session) -> dict:
    """Amman, rest-of-Jordan, and a global contact-us fallback."""
    from app.models.identity import Country, Province
    from app.models.orders import ShippingRule

    now = utcnow()
    jordan = Country(
        iso_code="JO", phone_code="+962", name_ar="الأردن", name_en="Jordan",
        scd_active_from=now,
    )
    db.add(jordan)
    db.flush()
    amman = Province(
        fk_country_id=jordan.pk_country_id, name_ar="عمّان", name_en="Amman",
        scd_active_from=now,
    )
    irbid = Province(
        fk_country_id=jordan.pk_country_id, name_ar="إربد", name_en="Irbid",
        scd_active_from=now,
    )
    db.add_all([amman, irbid])
    db.flush()

    db.add_all([
        ShippingRule(
            fk_country_id=jordan.pk_country_id, fk_province_id=amman.pk_province_id,
            cost_amt=Decimal("2.000"), free_above_amt=Decimal("30.000"),
            priority=20, scd_active_from=now,
        ),
        ShippingRule(
            fk_country_id=jordan.pk_country_id, cost_amt=Decimal("3.500"),
            free_above_amt=Decimal("50.000"), priority=10, scd_active_from=now,
        ),
        ShippingRule(
            cost_amt=Decimal("0"), quote_on_contact_flag=True,
            priority=0, scd_active_from=now,
        ),
    ])
    db.commit()
    return {"jordan": jordan, "amman": amman, "irbid": irbid}


def test_pickup_is_never_charged(db: Session, shipping_rules: dict):
    from app.services import shipping

    quote = shipping.resolve(
        db, method=FulfillmentMethod.PICKUP, subtotal_amt=Decimal("5.000")
    )
    assert quote.amount_amt == Decimal("0")
    assert quote.quote_on_contact is False


def test_most_specific_rule_wins(db: Session, shipping_rules: dict):
    """A governorate rule beats the country-wide fallback."""
    from app.services import shipping

    quote = shipping.resolve(
        db,
        method=FulfillmentMethod.SHIPPING,
        subtotal_amt=Decimal("10.000"),
        country_id=shipping_rules["jordan"].pk_country_id,
        province_id=shipping_rules["amman"].pk_province_id,
    )
    assert quote.amount_amt == Decimal("2.000")


def test_country_rule_covers_other_governorates(db: Session, shipping_rules: dict):
    from app.services import shipping

    quote = shipping.resolve(
        db,
        method=FulfillmentMethod.SHIPPING,
        subtotal_amt=Decimal("10.000"),
        country_id=shipping_rules["jordan"].pk_country_id,
        province_id=shipping_rules["irbid"].pk_province_id,
    )
    assert quote.amount_amt == Decimal("3.500")


def test_free_above_threshold(db: Session, shipping_rules: dict):
    from app.services import shipping

    quote = shipping.resolve(
        db,
        method=FulfillmentMethod.SHIPPING,
        subtotal_amt=Decimal("35.000"),
        country_id=shipping_rules["jordan"].pk_country_id,
        province_id=shipping_rules["amman"].pk_province_id,
    )
    assert quote.is_free
    assert quote.free_threshold_met is True


def test_unknown_destination_is_quoted_on_contact(db: Session, shipping_rules: dict):
    """"Not included, will be contacted" — the third outcome §2.2 requires.

    The order is still placeable; it is just flagged for staff to price.
    """
    from app.services import shipping

    quote = shipping.resolve(
        db,
        method=FulfillmentMethod.SHIPPING,
        subtotal_amt=Decimal("10.000"),
        country_id=9999,
        province_id=9999,
    )
    assert quote.quote_on_contact is True
    assert quote.amount_amt == Decimal("0")


def test_reversal_returns_the_use_to_the_customer(db: Session, store: dict):
    """A cancelled order gives the use back — via a negative row, not a delete."""
    code = Promocode(
        code="REUSE",
        promocode_kind=PromocodeKind.PERCENTAGE,
        percentage=Decimal("10"),
        max_uses_per_customer=1,
        scd_active_from=utcnow(),
    )
    db.add(code)
    db.flush()

    _cart(db, store, user=store["user"], quantity=1)
    order = place_order(
        db,
        _FakeRequest(),
        ShopperRef(user_id=store["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    redemption = promocodes.record_redemption(
        db, code, order_id=order.pk_order_id,
        user_id=store["user"].pk_user_id, discount_amt=Decimal("2.000"),
    )
    db.commit()
    assert promocodes.redemption_count(db, code.pk_promocode_id) == 1

    promocodes.reverse_redemption(db, redemption)
    db.commit()
    assert promocodes.redemption_count(db, code.pk_promocode_id) == 0
