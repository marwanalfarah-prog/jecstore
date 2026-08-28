"""Admin dashboard — the landing screen after a staff member signs in.

Deliberately answers "what needs me right now" rather than showing vanity
totals: approvals waiting, orders to prepare, stock below its minimum. Every
tile is gated on the permission for the screen it links to, so an Employee sees
a smaller dashboard rather than a wall of numbers they cannot act on.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.base import utcnow
from app.db.session import get_db
from app.models.enums import LineFulfillmentStatus, OrderStatus, PaymentStatus
from app.models.identity import User
from app.models.inventory import StockLevel
from app.models.orders import Order, OrderLine
from app.services import reviews
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, has_permission

router = APIRouter()


@dataclass(slots=True)
class Tile:
    label_key: str
    value: str
    href: str
    hint_key: str | None = None
    tone: str = "neutral"


@router.get("/")
def dashboard(
    request: Request,
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    tiles: list[Tile] = []

    if has_permission(db, staff, "orders", "view"):
        to_prepare = db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.status.in_([OrderStatus.PLACED, OrderStatus.IN_PREPARATION]),
                Order.scd_active_flag.is_(True),
            )
        ) or 0
        unpaid = db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.payment_status.in_(
                    [PaymentStatus.NOT_PAID, PaymentStatus.PARTIALLY_PAID]
                ),
                Order.status != OrderStatus.CANCELLED,
                Order.scd_active_flag.is_(True),
            )
        ) or 0
        awaiting_quote = db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.shipping_quote_pending_flag.is_(True),
                Order.status.not_in([OrderStatus.CANCELLED, OrderStatus.COMPLETE]),
                Order.scd_active_flag.is_(True),
            )
        ) or 0

        tiles.append(
            Tile(
                "admin.tile_to_prepare",
                str(to_prepare),
                "/admin/orders?status=open",
                hint_key="admin.tile_to_prepare_hint",
            )
        )
        tiles.append(
            Tile(
                "admin.tile_unpaid",
                str(unpaid),
                "/admin/orders?payment=unpaid",
                hint_key="admin.tile_unpaid_hint",
            )
        )
        if awaiting_quote:
            # Shipping "will be contacted" orders are stuck until someone acts
            # (Part I §2.2), so surface them rather than letting them sit.
            tiles.append(
                Tile(
                    "admin.tile_awaiting_quote",
                    str(awaiting_quote),
                    "/admin/orders?shipping=pending",
                    hint_key="admin.tile_awaiting_quote_hint",
                    tone="warning",
                )
            )

    if has_permission(db, staff, "inventory", "view"):
        tiles.append(
            Tile(
                "admin.tile_low_stock",
                str(_low_stock_count(db)),
                "/admin/inventory?filter=low",
                hint_key="admin.tile_low_stock_hint",
                tone="warning",
            )
        )

    if has_permission(db, staff, "returns", "view"):
        # §12 holds every return until someone inspects it, and an uninspected
        # return is a customer waiting on their money.
        from app.models.enums import ReturnStatus
        from app.models.orders import OrderReturn

        open_returns = db.scalar(
            select(func.count())
            .select_from(OrderReturn)
            .where(
                OrderReturn.status.in_(
                    [ReturnStatus.REQUESTED, ReturnStatus.UNDER_INSPECTION]
                ),
                OrderReturn.scd_active_flag.is_(True),
            )
        ) or 0
        if open_returns:
            tiles.append(
                Tile(
                    "admin.tile_open_returns",
                    str(open_returns),
                    "/admin/returns?status=open",
                    tone="warning",
                )
            )

    if has_permission(db, staff, "catalog", "moderate_reviews"):
        # §14 holds every review until a moderator acts, so an unwatched queue
        # means customers writing reviews that silently never appear. Only
        # shown when there is something waiting.
        waiting = reviews.pending_count(db)
        if waiting:
            tiles.append(
                Tile(
                    "admin.tile_pending_reviews",
                    str(waiting),
                    # The screen is under /admin/products; /admin/reviews has
                    # never been routed, so this tile used to open a 404.
                    "/admin/products/reviews?status=pending",
                    tone="warning",
                )
            )

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        admin_context(
            db,
            staff,
            tiles=tiles,
            recent_orders=_recent_orders(db, staff),
            today=utcnow().date(),
        ),
    )


def _low_stock_count(db: Session) -> int:
    """Variants at or below their minimum level (Part I §11).

    Falls back to the product's threshold when the variant has none, which is
    the common case for single-variant products.
    """
    from app.models.catalog import Product, ProductVariant

    rows = db.execute(
        select(
            func.sum(StockLevel.quantity_on_hand - StockLevel.quantity_reserved).label("sellable"),
            func.coalesce(ProductVariant.min_stock_level, Product.min_stock_level).label("minimum"),
        )
        .join(
            ProductVariant,
            ProductVariant.pk_product_variant_id == StockLevel.fk_product_variant_id,
        )
        .join(Product, Product.pk_product_id == ProductVariant.fk_product_id)
        .where(
            StockLevel.scd_active_flag.is_(True),
            ProductVariant.scd_active_flag.is_(True),
        )
        .group_by(ProductVariant.pk_product_variant_id)
    ).all()

    return sum(
        1
        for sellable, minimum in rows
        if minimum is not None and (sellable or 0) <= minimum
    )


def _recent_orders(db: Session, staff: User, limit: int = 10) -> list[Order]:
    if not has_permission(db, staff, "orders", "view"):
        return []
    return list(
        db.scalars(
            select(Order)
            .where(Order.scd_active_flag.is_(True))
            .order_by(Order.placed_dt.desc())
            .limit(limit)
        ).all()
    )
