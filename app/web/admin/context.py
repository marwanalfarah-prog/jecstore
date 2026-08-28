"""Shared render context for admin pages.

Every admin template needs the same three things: which nav items this person
may see, how many approvals are waiting for them, and how much work is sitting
in each module. Building all of it in one helper keeps it consistent and keeps
the cost predictable — the sidebar asks about a dozen permissions on every page
load, and doing that ad hoc per route is how N+1s creep in (Part II §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.identity import User
from app.services.approvals import pending_queue
from app.web.admin.deps import permission_map

#: Every permission the sidebar branches on.
NAV_PERMISSIONS = [
    "orders.view",
    "returns.view",
    "catalog.view",
    "catalog.moderate_reviews",
    "content.manage_promocodes",
    "inventory.view",
    "consignment.view",
    "money_boxes.view",
    "reports.view",
    "reports.view_financials",
    "reports.export",
    "users.view",
    "access.view",
    "users.view_sessions",
    "content.manage_homepage",
    "content.manage_branches",
    "content.manage_announcements",
    "content.manage_footer",
    "content.manage_email_templates",
    "shipping.view",
    "settings.view_audit_log",
]


@dataclass(slots=True)
class NavCounts:
    """Work waiting, per module, for the badges beside the sidebar items.

    The sidebar is on every screen, which makes it the cheapest place to answer
    "what needs me" — otherwise staff open each module in turn to discover it
    is empty. Every field is zero unless the viewer holds the permission for
    that screen, so the query only runs for someone who could act on the answer.
    """

    orders_open: int = 0
    returns_open: int = 0
    reviews_pending: int = 0


def nav_counts(db: Session, staff: User, can: dict[str, bool]) -> NavCounts:
    """Three indexed counts. Deliberately not the dashboard's fuller set.

    Anything needing a group-by or a per-row Python pass — low stock compares
    each variant against its own threshold — stays on the dashboard, which is
    one screen rather than every screen.
    """
    counts = NavCounts()

    if can.get("orders.view"):
        from app.models.enums import OrderStatus
        from app.models.orders import Order

        counts.orders_open = db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.status.in_([OrderStatus.PLACED, OrderStatus.IN_PREPARATION]),
                Order.scd_active_flag.is_(True),
            )
        ) or 0

    if can.get("returns.view"):
        from app.models.enums import ReturnStatus
        from app.models.orders import OrderReturn

        # Requested and under-inspection are the two states where a return is
        # waiting on staff; approved/refunded/rejected/withdrawn are settled.
        counts.returns_open = db.scalar(
            select(func.count())
            .select_from(OrderReturn)
            .where(
                OrderReturn.status.in_(
                    [ReturnStatus.REQUESTED, ReturnStatus.UNDER_INSPECTION]
                ),
                OrderReturn.scd_active_flag.is_(True),
            )
        ) or 0

    if can.get("catalog.moderate_reviews"):
        from app.services import reviews

        counts.reviews_pending = reviews.pending_count(db)

    return counts


def admin_context(db: Session, staff: User, **extra: Any) -> dict[str, Any]:
    """Base template context for any admin page."""
    from app.services.exports import pdf_available

    can = permission_map(db, staff, NAV_PERMISSIONS)

    context: dict[str, Any] = {
        "can": can,
        # Screens offer the print view instead where PDF cannot render.
        "pdf_available": pdf_available(),
        "pending_approvals": len(pending_queue(db, staff)),
        "nav_counts": nav_counts(db, staff, can),
        "staff": staff,
    }
    context.update(extra)
    return context
