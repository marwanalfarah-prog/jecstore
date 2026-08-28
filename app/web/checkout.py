"""Checkout endpoints (Part I §8).

Login is required and guest checkout is not offered, so every route here
redirects an anonymous shopper to sign in — carrying `?next=` so they land back
on checkout rather than the homepage.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import get_context
from app.core.errors import AppError, NotFound
from app.core.logging import get_logger
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import FulfillmentMethod
from app.models.identity import Address, Country, Province
from app.models.inventory import Branch
from app.models.orders import Order, OrderLine
from app.services.checkout import CheckoutRequest, build_quote, place_order
from app.services.commerce import cart_promocode, shopper_ref

log = get_logger(__name__)
router = APIRouter(tags=["checkout"])


def _require_login(request: Request) -> RedirectResponse | None:
    if get_context(request).is_authenticated:
        return None
    return RedirectResponse(
        f"/auth/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/checkout")
def checkout_page(request: Request, db: Session = Depends(get_db)) -> Response:
    if (redirect := _require_login(request)) is not None:
        return redirect

    ctx = get_context(request)
    shopper = shopper_ref(request)

    submission = CheckoutRequest(
        fulfillment_method=request.query_params.get("method", FulfillmentMethod.PICKUP),
        address_id=_maybe_int(request.query_params.get("address_id")),
        promocode=cart_promocode(db, shopper),
    )
    quote = build_quote(db, shopper, submission)

    if not quote.cart.lines:
        return RedirectResponse("/cart", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request,
        "commerce/checkout.html",
        {
            "quote": quote,
            "submission": submission,
            "addresses": _addresses(db, ctx.user.pk_user_id),
            "branches": _pickup_branches(db),
            "email_verified": ctx.user.email_verified_flag,
        },
    )


@router.post("/checkout")
def checkout_submit(
    request: Request,
    fulfillment_method: str = Form(FulfillmentMethod.PICKUP),
    pickup_branch_id: int | None = Form(None),
    address_id: int | None = Form(None),
    promocode: str | None = Form(None),
    customer_note: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    if (redirect := _require_login(request)) is not None:
        return redirect

    shopper = shopper_ref(request)
    submission = CheckoutRequest(
        fulfillment_method=fulfillment_method,
        pickup_branch_id=pickup_branch_id,
        address_id=address_id,
        promocode=promocode,
        customer_note=customer_note,
    )

    try:
        order = place_order(db, request, shopper, submission)
        db.commit()
    except AppError as exc:
        # Stock ran out, the address is missing, the cart emptied — re-render
        # checkout with the reason rather than dropping the shopper on an error
        # page with a full cart and no idea what to do next.
        db.rollback()
        log.info("checkout_rejected", extra={"code": exc.code})
        ctx = get_context(request)
        quote = build_quote(db, shopper, submission)
        if not quote.cart.lines:
            return RedirectResponse("/cart", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request,
            "commerce/checkout.html",
            {
                "quote": quote,
                "submission": submission,
                "addresses": _addresses(db, ctx.user.pk_user_id),
                "branches": _pickup_branches(db),
                "email_verified": ctx.user.email_verified_flag,
                "error": exc.message,
            },
            status_code=exc.status_code if exc.status_code < 500 else 400,
        )

    return RedirectResponse(
        f"/checkout/confirmation/{order.order_number}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/checkout/confirmation/{order_number}")
def checkout_confirmation(
    request: Request, order_number: str, db: Session = Depends(get_db)
) -> Response:
    if (redirect := _require_login(request)) is not None:
        return redirect

    ctx = get_context(request)
    order = db.scalars(
        select(Order).where(
            Order.order_number == order_number,
            # Scoped to the signed-in customer: an order number must never
            # expose somebody else's order just because it was guessed.
            Order.fk_user_id == ctx.user.pk_user_id,
            Order.scd_active_flag.is_(True),
        )
    ).first()
    if order is None:
        raise NotFound("That order could not be found.")

    lines = db.scalars(
        select(OrderLine)
        .where(
            OrderLine.fk_order_id == order.pk_order_id,
            OrderLine.scd_active_flag.is_(True),
        )
        .order_by(OrderLine.pk_order_line_id)
    ).all()

    return templates.TemplateResponse(
        request,
        "commerce/confirmation.html",
        {"order": order, "lines": lines},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


@dataclass(slots=True)
class AddressOption:
    """One row of the address picker, with its place names already resolved.

    A view model rather than the ORM row, so the template never has to know
    that country and province live in separate lookup tables.
    """

    address: Address
    country: Country | None
    province: Province | None

    @property
    def id(self) -> int:
        return self.address.pk_address_id


def _addresses(db: Session, user_id: int) -> list[AddressOption]:
    rows = db.scalars(
        select(Address)
        .where(Address.fk_user_id == user_id, Address.scd_active_flag.is_(True))
        .order_by(Address.is_default_flag.desc(), Address.pk_address_id)
    ).all()
    if not rows:
        return []

    # Two batched lookups rather than two per address (Part II §2).
    countries = {
        c.pk_country_id: c
        for c in db.scalars(
            select(Country).where(
                Country.pk_country_id.in_({a.fk_country_id for a in rows})
            )
        ).all()
    }
    provinces = {
        p.pk_province_id: p
        for p in db.scalars(
            select(Province).where(
                Province.pk_province_id.in_({a.fk_province_id for a in rows})
            )
        ).all()
    }
    return [
        AddressOption(
            address=a,
            country=countries.get(a.fk_country_id),
            province=provinces.get(a.fk_province_id),
        )
        for a in rows
    ]


def _pickup_branches(db: Session) -> list[Branch]:
    return list(
        db.scalars(
            select(Branch)
            .where(
                Branch.scd_active_flag.is_(True),
                Branch.is_pickup_point_flag.is_(True),
            )
            .order_by(Branch.sort_order, Branch.pk_branch_id)
        ).all()
    )
