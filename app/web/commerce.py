"""Cart and compare storefront endpoints (Part I §8, §14)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.context import get_context
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.catalog import Product
from app.services import reviews
from app.services.catalog import availability_for_products, product_url
from app.services.checkout import CheckoutRequest, build_quote
from app.services.commerce import (
    add_to_cart,
    attach_guest_cookie,
    cart_count,
    cart_promocode,
    cart_view,
    compare_count,
    compare_products,
    remove_line,
    set_cart_promocode,
    shopper_ref,
    toggle_compare,
    update_line_quantity,
)
from app.services.pricing import price_products
from app.web.account import _current_user, _login_redirect_if_needed

router = APIRouter(tags=["commerce"])


@router.get("/cart")
def cart_page(request: Request, db: Session = Depends(get_db)) -> Response:
    shopper = shopper_ref(request)
    saved_code = cart_promocode(db, shopper)

    # Quote on render rather than storing a total: the cart always reflects the
    # current rate and current discount (Part I §1.1), and a saved code that has
    # since expired shows its reason here instead of failing at checkout.
    quote = build_quote(db, shopper, CheckoutRequest(promocode=saved_code))

    return templates.TemplateResponse(
        request,
        "commerce/cart.html",
        {"cart_view": quote.cart, "quote": quote, "promocode": saved_code},
    )


@router.post("/cart/add")
def cart_add(
    request: Request,
    product_id: int = Form(...),
    variant_id: int | None = Form(None),
    quantity: int = Form(1),
    db: Session = Depends(get_db),
) -> Response:
    shopper = shopper_ref(request, create_guest=True)
    add_to_cart(
        db,
        request,
        shopper,
        product_id=product_id,
        variant_id=variant_id,
        quantity=quantity,
    )
    db.commit()

    get_context(request).cart_count = cart_count(db, shopper)
    response = templates.TemplateResponse(
        request,
        "partials/cart_indicator.html",
        {"oob": True},
    )
    attach_guest_cookie(response, shopper)
    return response


@router.post("/cart/update")
def cart_update(
    request: Request,
    line_id: int = Form(...),
    quantity: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Change a line's quantity. Zero removes it."""
    shopper = shopper_ref(request)
    update_line_quantity(db, request, shopper, line_id=line_id, quantity=quantity)
    db.commit()
    return _cart_redirect()


@router.post("/cart/remove")
def cart_remove(
    request: Request,
    line_id: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    shopper = shopper_ref(request)
    remove_line(db, request, shopper, line_id=line_id)
    db.commit()
    return _cart_redirect()


@router.post("/cart/promocode")
def cart_apply_promocode(
    request: Request,
    promocode: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Save a promocode against the cart.

    Saving, not applying: the discount is recomputed at checkout against the
    limits and expiry as they stand then (Part I §1.1, §13). Validation
    feedback is shown on the cart page, which re-quotes on render.
    """
    shopper = shopper_ref(request)
    set_cart_promocode(db, shopper, promocode)
    db.commit()
    return _cart_redirect()


def _cart_redirect() -> RedirectResponse:
    """Post/Redirect/Get, so a refresh never replays the mutation."""
    return RedirectResponse("/cart", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/compare")
def compare_page(request: Request, db: Session = Depends(get_db)) -> Response:
    shopper = shopper_ref(request)
    products = compare_products(db, shopper)
    return templates.TemplateResponse(
        request,
        "commerce/compare.html",
        {
            "products": products,
            "prices": price_products(db, products),
            "availability": availability_for_products(
                db, [product.pk_product_id for product in products]
            ),
        },
    )


@router.post("/compare/toggle")
def compare_toggle(
    request: Request,
    product_id: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    shopper = shopper_ref(request, create_guest=True)
    toggle_compare(db, shopper, product_id=product_id)
    db.commit()

    get_context(request).compare_count = compare_count(db, shopper)
    response = templates.TemplateResponse(
        request,
        "partials/compare_indicator.html",
        {"oob": True},
    )
    attach_guest_cookie(response, shopper)
    return response


# ---------------------------------------------------------------------------
# Reviews (Part I §14)
# ---------------------------------------------------------------------------

#: Feedback codes the product page may be redirected back with. Closed, because
#: the value arrives on the query string and is rendered to the customer.
REVIEW_FLASHES = frozenset(
    {"submitted", "already_reviewed", "bad_rating", "too_many"}
)

#: A person with something to say about several books is welcome; a person
#: filing twenty reviews in an hour is filling the moderation queue, not
#: reviewing. Generous enough that a genuine customer never meets it.
REVIEW_RATE_LIMIT = 10


@router.post("/products/{product_id}/review")
def submit_review(
    request: Request,
    product_id: int,
    rating: int = Form(...),
    title: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Take a customer's review. It is queued for moderation, not published.

    Login is required: §14 pairs reviews with the account area, and an
    anonymous review is unmoderatable in practice — there is nobody to hold to
    it and nothing to rate-limit.
    """
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _current_user(request, db)

    product = db.get(Product, product_id)
    if product is None or not product.scd_active_flag or not product.is_visible_flag:
        raise NotFound("That product does not exist.")

    destination = product_url(product, get_context(request).language)

    if reviews.recent_submission_count(db, user_id=user.pk_user_id) >= REVIEW_RATE_LIMIT:
        return _review_redirect(destination, "too_many")

    try:
        reviews.submit_review(
            db, product=product, user=user, rating=rating, title=title, body=body
        )
    except ValidationFailed:
        db.rollback()
        return _review_redirect(destination, "bad_rating")
    except Conflict:
        db.rollback()
        return _review_redirect(destination, "already_reviewed")

    db.commit()
    return _review_redirect(destination, "submitted")


def _review_redirect(destination: str, flash: str) -> RedirectResponse:
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(
        f"{destination}{separator}review={flash}#reviews",
        status_code=status.HTTP_303_SEE_OTHER,
    )
