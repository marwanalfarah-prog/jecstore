"""Linked storefront utility pages: search, branches, tracking and newsletter."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import Response
from sqlalchemy import false, select
from sqlalchemy.orm import Session, selectinload

from app.core.context import get_context
from app.core.security import clean_text, normalize_email
from app.core.templating import templates
from app.db.session import get_db
from app.models.identity import User
from app.models.inventory import Branch
from app.models.orders import Order
from app.services.catalog import PAGE_SIZES, SORT_OPTIONS, availability_for_products
from app.services.newsletter import set_subscription
from app.services.pricing import price_products
from app.services.search import search_products, suggestions

router = APIRouter(tags=["content"])


@router.get("/search")
def search_page(
    request: Request,
    q: str | None = None,
    page: int = 1,
    per_page: str | None = None,
    sort: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    ctx = get_context(request)
    result = search_products(
        db,
        q=q,
        language=ctx.language,
        page=page,
        per_page=per_page,
        sort=sort,
    )
    total_pages = max((result.total + result.page_size - 1) // result.page_size, 1)
    return templates.TemplateResponse(
        request,
        "storefront/search.html",
        {
            "result": result,
            "products": result.products,
            "prices": price_products(db, result.products),
            "availability": availability_for_products(
                db, [product.pk_product_id for product in result.products]
            ),
            "total_pages": total_pages,
            "sort_options": SORT_OPTIONS,
            "page_sizes": PAGE_SIZES,
        },
    )


@router.get("/search/suggest")
def search_suggest(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    products = suggestions(db, q=q)
    if not products and not clean_text(q or ""):
        return Response("")
    return templates.TemplateResponse(
        request,
        "storefront/_search_suggestions.html",
        {"suggestions": products, "q": clean_text(q or "", max_length=80) or ""},
    )


@router.post("/newsletter/subscribe")
def newsletter_subscribe(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    normalized = normalize_email(email)
    if "@" not in normalized or "." not in normalized.split("@")[-1]:
        return templates.TemplateResponse(
            request,
            "partials/newsletter_form.html",
            {"newsletter_error": "newsletter.invalid_email", "email": email},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    ctx = get_context(request)
    set_subscription(
        db,
        email=normalized,
        subscribed=True,
        source="footer",
        user_id=ctx.user.pk_user_id if ctx.user else None,
        changed_by=ctx.user.pk_user_id if ctx.user else None,
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "partials/newsletter_form.html",
        {"subscribed": True, "email": normalized},
    )


@router.get("/branches")
def branches_page(request: Request, db: Session = Depends(get_db)) -> Response:
    branches = list(
        db.scalars(
            select(Branch)
            .where(Branch.scd_active_flag.is_(True))
            .options(selectinload(Branch.hours))
            .order_by(Branch.sort_order, Branch.pk_branch_id)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "content/branches.html",
        {"branches": branches},
    )


@router.get("/orders/track")
def track_order_page(
    request: Request,
    order_number: str | None = None,
    email: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    order = None
    searched = bool(clean_text(order_number or ""))
    if searched:
        stmt = (
            select(Order)
            .where(
                Order.order_number == clean_text(order_number or "", max_length=30),
                Order.scd_active_flag.is_(True),
            )
            .options(selectinload(Order.lines))
        )
        ctx = get_context(request)
        normalized_email = normalize_email(email or "")
        if ctx.user is not None:
            stmt = stmt.where(Order.fk_user_id == ctx.user.pk_user_id)
        elif normalized_email:
            stmt = stmt.join(User, User.pk_user_id == Order.fk_user_id).where(
                User.email == normalized_email,
                User.scd_active_flag.is_(True),
            )
        else:
            stmt = stmt.where(false())
        order = db.scalars(stmt).first()

    return templates.TemplateResponse(
        request,
        "content/track_order.html",
        {
            "order": order,
            "searched": searched,
            "order_number": order_number or "",
            "email": email or "",
        },
    )
