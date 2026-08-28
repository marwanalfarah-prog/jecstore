"""Panel-wide quick search — one box that reaches any record.

The panel is organised by module, which is right for doing work and wrong for
*finding* the thing to work on: a member of staff holding a printed invoice had
to first decide that an order number lives under Orders, open that screen, and
search again there. A customer on the phone quoting a book title meant a third
detour. This collapses all of it into the box in the header.

Two behaviours, and the distinction matters at a counter:

* An **exact** hit on something unique — an order number, a barcode, a SKU —
  redirects straight to that record. Scanning a label should open the item, not
  a page listing one result.
* Anything else lists what matched, grouped by kind.

Every group is gated on the permission for the screen it links to, so this
never becomes a side door around §2.2: someone without ``orders.view`` gets no
orders section, not an empty one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.session import get_db
from app.models.catalog import Product, ProductVariant
from app.models.identity import User
from app.models.orders import Order
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, has_permission

router = APIRouter(prefix="/search")

#: Per group. Enough to show that the search worked and to pick a row out of;
#: anything longer belongs on the module's own screen, which each group links to.
LIMIT = 8


@dataclass(slots=True)
class Group:
    """One kind of record, its hits, and where to see the rest of them."""

    key: str
    hits: list["Hit"] = field(default_factory=list)
    more_href: str | None = None


@dataclass(slots=True)
class Hit:
    href: str
    title: str
    detail: str = ""
    badge: str = ""


@router.get("")
def quick_search(
    request: Request,
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    query = (request.query_params.get("q") or "").strip()
    if not query:
        return templates.TemplateResponse(
            request, "admin/search.html", admin_context(db, staff, query="", groups=[])
        )

    exact = _exact_match(db, staff, query)
    if exact is not None:
        return RedirectResponse(exact, status_code=303)

    groups = [
        group
        for group in (
            _orders(db, staff, query),
            _products(db, staff, query),
            _variants(db, staff, query),
            _customers(db, staff, query),
        )
        if group is not None and group.hits
    ]

    return templates.TemplateResponse(
        request,
        "admin/search.html",
        admin_context(db, staff, query=query, groups=groups),
    )


def _exact_match(db: Session, staff: User, query: str) -> str | None:
    """A code that identifies exactly one record — jump straight to it."""
    if has_permission(db, staff, "orders", "view"):
        order = db.scalars(
            select(Order).where(
                func.upper(Order.order_number) == query.upper(),
                Order.scd_active_flag.is_(True),
            )
        ).first()
        if order is not None:
            return f"/admin/orders/{order.pk_order_id}"

    if has_permission(db, staff, "inventory", "view"):
        # A scanner types the barcode and presses Enter, so this is the path a
        # label takes. SKU is accepted too — staff type it when a label tears.
        variant = db.scalars(
            select(ProductVariant).where(
                or_(
                    ProductVariant.barcode == query,
                    func.upper(ProductVariant.sku) == query.upper(),
                ),
                ProductVariant.scd_active_flag.is_(True),
            )
        ).first()
        if variant is not None:
            return f"/admin/inventory/item/{variant.pk_product_variant_id}"

    return None


def _orders(db: Session, staff: User, query: str) -> Group | None:
    if not has_permission(db, staff, "orders", "view"):
        return None

    rows = db.scalars(
        select(Order)
        .where(
            Order.order_number.like(f"%{query.upper()}%"),
            Order.scd_active_flag.is_(True),
        )
        .order_by(Order.placed_dt.desc())
        .limit(LIMIT)
    ).all()

    return Group(
        key="orders",
        more_href=f"/admin/orders?q={query}",
        hits=[
            Hit(
                href=f"/admin/orders/{order.pk_order_id}",
                title=order.order_number,
                detail=str(order.total_amt),
                badge=order.status,
            )
            for order in rows
        ],
    )


def _products(db: Session, staff: User, query: str) -> Group | None:
    if not has_permission(db, staff, "catalog", "view"):
        return None

    like, raw = f"%{query.lower()}%", f"%{query}%"
    rows = db.scalars(
        select(Product)
        .where(
            or_(
                func.lower(Product.name_en).like(like),
                Product.name_ar.like(raw),
                func.lower(Product.isbn).like(like),
            ),
            Product.scd_active_flag.is_(True),
        )
        .order_by(Product.pk_product_id.desc())
        .limit(LIMIT)
    ).all()

    return Group(
        key="products",
        more_href=f"/admin/products?q={query}",
        hits=[
            Hit(
                href=f"/admin/products/{product.pk_product_id}",
                title=product.name_en,
                detail=product.name_ar,
                badge="visible" if product.is_visible_flag else "hidden",
            )
            for product in rows
        ],
    )


def _variants(db: Session, staff: User, query: str) -> Group | None:
    """Partial SKU and barcode hits — an exact one never reaches here."""
    if not has_permission(db, staff, "inventory", "view"):
        return None

    like = f"%{query.upper()}%"
    rows = db.execute(
        select(ProductVariant, Product)
        .join(Product, Product.pk_product_id == ProductVariant.fk_product_id)
        .where(
            or_(
                func.upper(ProductVariant.sku).like(like),
                func.upper(ProductVariant.barcode).like(like),
            ),
            ProductVariant.scd_active_flag.is_(True),
        )
        .limit(LIMIT)
    ).all()

    return Group(
        key="inventory",
        more_href=f"/admin/inventory?q={query}",
        hits=[
            Hit(
                href=f"/admin/inventory/item/{variant.pk_product_variant_id}",
                title=variant.sku,
                detail=product.name_en,
            )
            for variant, product in rows
        ],
    )


def _customers(db: Session, staff: User, query: str) -> Group | None:
    if not has_permission(db, staff, "users", "view"):
        return None

    like = f"%{query.lower()}%"
    rows = db.scalars(
        select(User)
        .where(
            or_(
                func.lower(User.username).like(like),
                func.lower(User.email).like(like),
            ),
            User.scd_active_flag.is_(True),
        )
        .limit(LIMIT)
    ).all()

    return Group(
        key="users",
        more_href=f"/admin/users?q={query}",
        hits=[
            Hit(
                href=f"/admin/users?q={user.username}",
                title=user.username,
                detail=user.email,
                badge="" if user.is_active_flag else "inactive",
            )
            for user in rows
        ],
    )
