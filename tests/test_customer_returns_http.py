"""The customer returns flow over real HTTP (Part I §12, §2.6).

:mod:`tests.test_customer_returns` pins the rules. This module pins that a
signed-in shopper can actually reach them through a browser: the routes are
registered, the templates render in both languages, and the scoping holds when
the order number arrives from the URL rather than from a test fixture.

The app's session factory is rebound to the fixture's in-memory engine, so the
middleware, the route dependency and the test all see one database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.enums import ReturnStatus
from app.models.identity import User
from app.models.orders import Order, OrderReturn
from app.services import money, orders, sessions
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


@pytest.fixture
def client(db: Session, monkeypatch) -> TestClient:
    """A TestClient whose app talks to the fixture database."""
    import app.db.session as db_session
    import app.services.activity as activity
    import app.web.middleware as middleware
    from app.main import app

    maker = sessionmaker(
        bind=db.get_bind(), autoflush=False, expire_on_commit=False, class_=Session
    )
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    monkeypatch.setattr(middleware, "SessionLocal", maker)
    # Page-view logging writes on its own schedule and is not what is under test.
    monkeypatch.setattr(activity, "record_page_view", lambda *a, **k: None)

    return TestClient(app)


@pytest.fixture
def signed_in(client: TestClient, db: Session, shop: dict) -> TestClient:
    session = sessions.create_session(db, shop["user"], _FakeRequest())
    db.commit()
    client.cookies.set(settings.session_cookie_name, session.session_key)
    return client


@pytest.fixture
def delivered(db: Session, shop: dict) -> Order:
    _cart(db, shop, user=shop["user"], quantity=3)
    order = place_order(
        db, _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    money.record_order_payment(
        db, order,
        [money.Split(channel_id=shop["cash"].pk_payment_channel_id,
                     amount_amt=order.total_amt,
                     money_box_id=shop["box"].pk_money_box_id)],
    )
    orders.mark_delivered(db, order, shop["user"])
    db.commit()
    return order


# ---------------------------------------------------------------------------
# Reaching the pages
# ---------------------------------------------------------------------------


def test_the_returns_list_renders_for_a_signed_in_customer(signed_in: TestClient):
    response = signed_in.get("/account/returns")
    assert response.status_code == 200
    assert "state-panel" in response.text, "the empty state renders"


def test_the_request_form_lists_the_delivered_items(
    signed_in: TestClient, db: Session, delivered: Order
):
    line = orders.active_lines(db, delivered)[0]
    response = signed_in.get(f"/account/returns/new/{delivered.order_number}")

    assert response.status_code == 200
    assert f'name="qty_{line.pk_order_line_id}"' in response.text
    assert 'max="3"' in response.text, "capped at what was delivered"


def test_the_order_list_offers_a_return_link(
    signed_in: TestClient, delivered: Order
):
    response = signed_in.get("/account/orders")
    assert response.status_code == 200
    assert f"/account/returns/new/{delivered.order_number}" in response.text


def test_both_languages_render(signed_in: TestClient, delivered: Order):
    """One template serves RTL and LTR (§17.3), so both directions must render.

    The key check catches a string absent from *both* catalogs, which renders as
    its own name; per-language gaps are the parity test's job in
    :mod:`tests.test_locales`.
    """
    for language in ("ar", "en"):
        response = signed_in.get(
            f"/account/returns/new/{delivered.order_number}?lang={language}"
        )
        assert response.status_code == 200
        assert "returns." not in response.text, f"untranslated key leaked in {language}"
        assert f'dir="{"rtl" if language == "ar" else "ltr"}"' in response.text


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


def test_submitting_creates_a_return_and_redirects(
    signed_in: TestClient, db: Session, delivered: Order
):
    line = orders.active_lines(db, delivered)[0]

    response = signed_in.post(
        f"/account/returns/new/{delivered.order_number}",
        data={f"qty_{line.pk_order_line_id}": "1", "reason_code": "damaged"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/returns?flash=submitted"

    db.expire_all()
    created = db.query(OrderReturn).one()
    assert created.status == ReturnStatus.REQUESTED
    assert created.reason_code == "damaged"
    assert created.fk_user_id == delivered.fk_user_id


def test_the_new_return_shows_up_on_the_list(
    signed_in: TestClient, db: Session, delivered: Order
):
    line = orders.active_lines(db, delivered)[0]
    signed_in.post(
        f"/account/returns/new/{delivered.order_number}",
        data={f"qty_{line.pk_order_line_id}": "2", "reason_code": "wrong_item"},
    )

    db.expire_all()
    number = db.query(OrderReturn).one().return_number

    response = signed_in.get("/account/returns")
    assert number in response.text


def test_submitting_nothing_returns_to_the_form_with_a_message(
    signed_in: TestClient, db: Session, delivered: Order
):
    response = signed_in.post(
        f"/account/returns/new/{delivered.order_number}",
        data={"reason_code": "changed_mind"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("?error=no_items")
    assert db.query(OrderReturn).count() == 0

    # And the message is real text, not a leaked translation key.
    followed = signed_in.get(response.headers["location"])
    assert "returns.error_" not in followed.text


def test_a_missing_reason_returns_to_the_form(
    signed_in: TestClient, db: Session, delivered: Order
):
    line = orders.active_lines(db, delivered)[0]
    response = signed_in.post(
        f"/account/returns/new/{delivered.order_number}",
        data={f"qty_{line.pk_order_line_id}": "1"},
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("?error=no_reason")
    assert db.query(OrderReturn).count() == 0


def test_claiming_more_than_was_delivered_returns_to_the_form(
    signed_in: TestClient, db: Session, delivered: Order
):
    """The form caps this with ``max``; the server must not trust that."""
    line = orders.active_lines(db, delivered)[0]
    response = signed_in.post(
        f"/account/returns/new/{delivered.order_number}",
        data={f"qty_{line.pk_order_line_id}": "99", "reason_code": "damaged"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("?error=too_many")
    assert db.query(OrderReturn).count() == 0


def test_a_junk_error_code_is_not_reflected_back(
    signed_in: TestClient, delivered: Order
):
    """The value comes off the query string, so it must not reach the page."""
    response = signed_in.get(
        f"/account/returns/new/{delivered.order_number}?error=<script>x</script>"
    )
    assert response.status_code == 200
    assert "script>x" not in response.text


# ---------------------------------------------------------------------------
# Withdrawing
# ---------------------------------------------------------------------------


def test_a_customer_can_withdraw_their_own_request(
    signed_in: TestClient, db: Session, delivered: Order
):
    line = orders.active_lines(db, delivered)[0]
    signed_in.post(
        f"/account/returns/new/{delivered.order_number}",
        data={f"qty_{line.pk_order_line_id}": "1", "reason_code": "changed_mind"},
    )
    db.expire_all()
    created = db.query(OrderReturn).one()

    response = signed_in.post(
        f"/account/returns/{created.return_number}/cancel", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/returns?flash=withdrawn"

    db.expire_all()
    assert db.query(OrderReturn).one().status == ReturnStatus.WITHDRAWN


def test_withdrawing_an_unknown_return_is_not_found(signed_in: TestClient):
    response = signed_in.post("/account/returns/RET-999999-999/cancel")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Scoping (Part I §2.6)
# ---------------------------------------------------------------------------


@pytest.fixture
def stranger(client: TestClient, db: Session, shop: dict) -> TestClient:
    """A second signed-in customer, sharing nothing with the first."""
    from app.db.base import utcnow

    other = User(
        fk_role_id=shop["user"].fk_role_id,
        username="stranger",
        email="stranger@example.com",
        password_hash="x",
        email_verified_flag=True,
        scd_active_from=utcnow(),
    )
    db.add(other)
    db.flush()
    session = sessions.create_session(db, other, _FakeRequest())
    db.commit()

    client.cookies.set(settings.session_cookie_name, session.session_key)
    return client


def test_another_customers_order_is_a_404_not_a_403(
    stranger: TestClient, delivered: Order
):
    """A 403 would confirm the order number is real."""
    response = stranger.get(f"/account/returns/new/{delivered.order_number}")
    assert response.status_code == 404


def test_a_stranger_cannot_open_a_return_on_your_order(
    stranger: TestClient, db: Session, delivered: Order
):
    line = orders.active_lines(db, delivered)[0]
    response = stranger.post(
        f"/account/returns/new/{delivered.order_number}",
        data={f"qty_{line.pk_order_line_id}": "1", "reason_code": "damaged"},
    )

    assert response.status_code == 404
    assert db.query(OrderReturn).count() == 0


def test_a_stranger_cannot_withdraw_your_return(
    client: TestClient, db: Session, shop: dict, delivered: Order, stranger: TestClient
):
    from app.services import returns

    line = orders.active_lines(db, delivered)[0]
    mine = returns.request_return(
        db, delivered,
        [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=1)],
        reason_code="damaged",
        requested_by=shop["user"],
        delivered_only=True,
    )
    db.commit()

    response = stranger.post(f"/account/returns/{mine.return_number}/cancel")

    assert response.status_code == 404
    db.expire_all()
    assert db.query(OrderReturn).one().status == ReturnStatus.REQUESTED


def test_a_stranger_sees_none_of_your_returns(
    db: Session, shop: dict, delivered: Order, stranger: TestClient
):
    from app.services import returns

    line = orders.active_lines(db, delivered)[0]
    mine = returns.request_return(
        db, delivered,
        [returns.ReturnLineRequest(order_line_id=line.pk_order_line_id, quantity=1)],
        reason_code="damaged",
        requested_by=shop["user"],
        delivered_only=True,
    )
    db.commit()

    response = stranger.get("/account/returns")
    assert response.status_code == 200
    assert mine.return_number not in response.text
