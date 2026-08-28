"""Customer account pages (Part I §2.6, §14)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.context import get_context
from app.core.errors import NotFound, ValidationFailed
from app.core.templating import templates
from app.db.base import utcnow
from app.db.session import get_db
from app.models.activity import UserSession
from app.models.catalog import Product
from app.models.enums import ActivityEvent, SessionEndReason
from app.models.identity import User
from app.models.orders import Order, Wishlist
from app.services.activity import record_event
from app.services.catalog import availability_for_products
from app.services import accounts, money, rate_limit, returns, reviews
from app.services.newsletter import is_subscribed, set_subscription
from app.services.pricing import price_products
from app.services.sessions import (
    clear_session_cookie,
    client_ip,
    end_session,
    set_session_cookie,
)

router = APIRouter(prefix="/account", tags=["account"])


@router.get("")
def account_home(request: Request, db: Session = Depends(get_db)) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    orders = _orders_for_user(db, user.pk_user_id, limit=5)
    wishlist_total = db.scalar(
        select(func.count()).select_from(Wishlist).where(
            Wishlist.fk_user_id == user.pk_user_id,
            Wishlist.scd_active_flag.is_(True),
        )
    ) or 0
    return templates.TemplateResponse(
        request,
        "account/index.html",
        {
            "user": user,
            "orders": orders,
            "wishlist_total": wishlist_total,
            "newsletter_active": is_subscribed(db, user.email),
            # §2.6 lists the رصيد balance among what a customer sees about
            # themselves. Derived from the ledger, never stored (invariant 4).
            "store_credit": money.store_credit_balance(db, user.pk_user_id),
            "flash": _flash(request, {"verification_sent"}),
        },
    )


@router.get("/orders")
def account_orders(request: Request, db: Session = Depends(get_db)) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _current_user(request, db)
    orders = _orders_for_user(db, user.pk_user_id, limit=100)
    return templates.TemplateResponse(
        request,
        "account/orders.html",
        {
            "orders": orders,
            # Offer the return link only where something is actually returnable,
            # so the customer is never sent to a page that tells them no.
            "returnable": {
                order.pk_order_id
                for order in orders
                if returns.returnable_lines(db, order, delivered_only=True)
            },
        },
    )


@router.get("/wishlist")
def account_wishlist(request: Request, db: Session = Depends(get_db)) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _current_user(request, db)
    products = list(
        db.scalars(
            select(Product)
            .join(Wishlist, Wishlist.fk_product_id == Product.pk_product_id)
            .where(
                Wishlist.fk_user_id == user.pk_user_id,
                Wishlist.scd_active_flag.is_(True),
                Product.scd_active_flag.is_(True),
                Product.is_visible_flag.is_(True),
            )
            .order_by(Wishlist.pk_wishlist_id.desc())
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "account/wishlist.html",
        {
            "products": products,
            "prices": price_products(db, products),
            "availability": availability_for_products(
                db, [product.pk_product_id for product in products]
            ),
            "ratings": reviews.rating_summaries(
                db, [product.pk_product_id for product in products]
            ),
        },
    )


@router.post("/wishlist/toggle")
def wishlist_toggle(
    request: Request,
    product_id: int = Form(...),
    # Which shape of button was pressed — the icon-only one on a product card,
    # or the labelled one on the product page. The swap has to come back in the
    # same shape, or clicking the heart on a card replaces it with a full-width
    # button in the middle of the grid.
    compact: bool = Form(False),
    db: Session = Depends(get_db),
) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
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
        select(Wishlist).where(
            Wishlist.fk_user_id == user.pk_user_id,
            Wishlist.fk_product_id == product.pk_product_id,
            Wishlist.scd_active_flag.is_(True),
        )
    ).first()
    if existing is None:
        db.add(
            Wishlist(
                fk_user_id=user.pk_user_id,
                fk_product_id=product.pk_product_id,
                scd_active_from=utcnow(),
                scd_changed_by=user.pk_user_id,
            )
        )
    else:
        existing.close(changed_by=user.pk_user_id)
    db.commit()

    # Recompute from the same query the chrome uses, so the badge and
    # /account/wishlist cannot drift apart: that page joins Product and filters
    # on active + visible, and counting wishlist rows alone meant hiding a
    # product left the badge reading 1 over an empty page.
    context = get_context(request)
    context.wishlisted_ids = set(
        db.scalars(
            select(Wishlist.fk_product_id)
            .join(Product, Product.pk_product_id == Wishlist.fk_product_id)
            .where(
                Wishlist.fk_user_id == user.pk_user_id,
                Wishlist.scd_active_flag.is_(True),
                Product.scd_active_flag.is_(True),
                Product.is_visible_flag.is_(True),
            )
        ).all()
    )
    context.wishlist_count = len(context.wishlisted_ids)

    # Both the header badge and the button that was pressed, swapped
    # out-of-band. Returning only the badge left the control itself showing the
    # state it had *before* the click.
    return templates.TemplateResponse(
        request,
        "partials/wishlist_toggled.html",
        {"product": product, "compact": compact, "oob": True},
    )


@router.get("/newsletter")
def account_newsletter(request: Request, db: Session = Depends(get_db)) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _current_user(request, db)
    return templates.TemplateResponse(
        request,
        "account/newsletter.html",
        {"newsletter_active": is_subscribed(db, user.email)},
    )


@router.post("/newsletter")
def account_newsletter_update(
    request: Request,
    subscribed: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    enabled = subscribed == "1"
    user.newsletter_opt_in_flag = enabled
    set_subscription(
        db,
        email=user.email,
        subscribed=enabled,
        source="profile",
        user_id=user.pk_user_id,
        changed_by=user.pk_user_id,
    )
    db.commit()
    return RedirectResponse("/account/newsletter", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Profile (Part I §2.6)
# ---------------------------------------------------------------------------


@router.get("/profile")
def account_profile(request: Request, db: Session = Depends(get_db)) -> Response:
    """Edit their own submitted data, and see their saved addresses (§2.6)."""
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    addresses = accounts.own_addresses(db, user.pk_user_id)

    return templates.TemplateResponse(
        request,
        "account/profile.html",
        {
            "user": user,
            "profile": user.individual,
            "company": user.company,
            "addresses": addresses,
            "places": accounts.place_names(db, addresses),
            "flash": _flash(request, {"saved"}),
            "error": None,
        },
    )


@router.post("/profile")
def account_profile_update(
    request: Request,
    first_name: str = Form(""),
    second_name: str = Form(""),
    third_name: str = Form(""),
    last_name: str = Form(""),
    phone_country_code: str = Form(""),
    phone_number: str = Form(""),
    preferred_language: str = Form(""),
    preferred_currency: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    try:
        accounts.update_profile(
            db,
            user,
            first_name=first_name or None,
            second_name=second_name,
            third_name=third_name,
            last_name=last_name or None,
            phone_country_code=phone_country_code or None,
            phone_number=phone_number or None,
            preferred_language=preferred_language,
            preferred_currency=preferred_currency,
        )
    except ValidationFailed as failure:
        db.rollback()
        addresses = accounts.own_addresses(db, user.pk_user_id)
        return templates.TemplateResponse(
            request,
            "account/profile.html",
            {
                "user": user,
                "profile": user.individual,
                "company": user.company,
                "addresses": addresses,
                "places": accounts.place_names(db, addresses),
                "flash": None,
                "error": failure.message,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    db.commit()
    return RedirectResponse(
        "/account/profile?flash=saved", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Password (Part I §2.6, §2.3)
# ---------------------------------------------------------------------------


@router.get("/password")
def account_password(request: Request, db: Session = Depends(get_db)) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    _current_user(request, db)

    return templates.TemplateResponse(
        request,
        "account/password.html",
        {"flash": _flash(request, {"changed"}), "error": None},
    )


@router.post("/password")
def account_password_update(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Change password, ending every other session (Part I §2.3).

    The session doing the changing survives, so the customer is not thrown out
    of the browser they are standing in front of.
    """
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    ctx = get_context(request)

    try:
        accounts.change_own_password(
            db,
            user,
            current_password=current_password,
            new_password=new_password,
            confirm=confirm_password,
            keep_session_key=ctx.session_key,
        )
    except ValidationFailed as failure:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "account/password.html",
            {"flash": None, "error": failure.message},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    db.commit()
    return RedirectResponse(
        "/account/password?flash=changed", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Active sessions (Part I §2.6 — the customer-facing side of §2.3/§2.8)
# ---------------------------------------------------------------------------


@router.get("/sessions")
def account_sessions(request: Request, db: Session = Depends(get_db)) -> Response:
    """See where this account is signed in, and sign other devices out."""
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    ctx = get_context(request)

    return templates.TemplateResponse(
        request,
        "account/sessions.html",
        {
            "sessions": accounts.own_sessions(db, user.pk_user_id),
            "current_session_key": ctx.session_key,
            "flash": _flash(request, {"ended", "ended_all"}),
        },
    )


@router.post("/sessions/end")
def account_session_end(
    request: Request,
    session_key: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    ctx = get_context(request)

    accounts.end_own_session(
        db, user, session_key, current_session_key=ctx.session_key
    )
    db.commit()
    return RedirectResponse(
        "/account/sessions?flash=ended", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/sessions/end-others")
def account_sessions_end_others(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    ctx = get_context(request)

    accounts.end_other_sessions(db, user, keep_session_key=ctx.session_key)
    db.commit()
    return RedirectResponse(
        "/account/sessions?flash=ended_all", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Verification (Part I §2.5)
# ---------------------------------------------------------------------------


@router.post("/resend-verification")
def resend_verification(request: Request, db: Session = Depends(get_db)) -> Response:
    """The resend button §2.5 asks for beside the "Not Verified" badge.

    Rate limited, because the button mails a real person: without it, anyone
    with a session could use it to flood the account's inbox.
    """
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect

    user = _current_user(request, db)
    if user.email_verified_flag:
        return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)

    rate_limit.enforce(
        rate_limit.password_reset_policy(), f"verify:{user.pk_user_id}"
    )

    from app.web.auth import _queue_verification_email

    _queue_verification_email(db, user, requested_ip=client_ip(request))
    db.commit()

    return RedirectResponse(
        "/account?flash=verification_sent", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/impersonation/end")
def impersonation_end(request: Request, db: Session = Depends(get_db)) -> Response:
    ctx = get_context(request)
    if not ctx.session_key or not ctx.is_impersonating:
        return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)

    session = db.scalars(
        select(UserSession).where(
            UserSession.session_key == ctx.session_key,
            UserSession.scd_active_flag.is_(True),
        )
    ).first()
    parent = None
    if session is not None:
        if session.parent_session_key:
            parent = db.scalars(
                select(UserSession).where(
                    UserSession.session_key == session.parent_session_key,
                    UserSession.scd_active_flag.is_(True),
                )
            ).first()
        end_session(db, session, SessionEndReason.IMPERSONATION_ENDED)
        record_event(
            db,
            ActivityEvent.IMPERSONATION_ENDED,
            request=request,
            context=ctx,
            target_table="scd_user",
            target_row_id=ctx.user.pk_user_id if ctx.user else None,
            success=True,
        )
        db.commit()

    response = RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)
    if parent is not None:
        set_session_cookie(response, parent)
    else:
        clear_session_cookie(response)
    return response


def _orders_for_user(db: Session, user_id: int, *, limit: int) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .where(Order.fk_user_id == user_id, Order.scd_active_flag.is_(True))
            .options(selectinload(Order.lines))
            .order_by(Order.placed_dt.desc())
            .limit(limit)
        ).all()
    )


def _current_user(request: Request, db: Session) -> User:
    ctx = get_context(request)
    user_id = ctx.user.pk_user_id if ctx.user else None
    if user_id is None:
        raise NotFound()
    user = db.scalars(
        select(User)
        .where(User.pk_user_id == user_id, User.scd_active_flag.is_(True))
        .options(selectinload(User.individual), selectinload(User.company))
    ).first()
    if user is None:
        raise NotFound()
    return user


def _flash(request: Request, allowed: set[str]) -> str | None:
    """A flash code off the query string, checked against a closed set.

    The value is turned into a message shown to the customer, so an arbitrary
    one must not reach the page.
    """
    value = request.query_params.get("flash")
    return value if value in allowed else None


def _login_redirect_if_needed(request: Request) -> Response | None:
    if get_context(request).is_authenticated:
        return None
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    location = f"/auth/login?next={quote(target, safe='')}"
    if request.headers.get("hx-request"):
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
