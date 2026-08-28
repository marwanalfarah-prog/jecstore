"""Admin panel routers.

Each module gets its own file and its own router; ``build_router()`` assembles
them under ``/admin``. Keeping them separate is what stops the panel becoming
one enormous module as the remaining spec sections land.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_router() -> APIRouter:
    from fastapi.responses import RedirectResponse

    from app.web import invoices as invoice_views
    from app.web.admin import (
        approvals,
        access,
        audit,
        branches,
        categories,
        consignment,
        content,
        dashboard,
        exports,
        inventory,
        money_boxes,
        orders,
        products,
        promocodes,
        reports,
        returns,
        search,
        sessions,
        uploads,
        users,
    )

    router = APIRouter(prefix="/admin", tags=["admin"])

    # `/admin` (no trailing slash) needs an explicit route. Starlette's
    # redirect-slashes only fires when nothing else matches, and the legacy
    # catch-all in storefront.py matches everything — so without this, typing
    # /admin lands on the 404 page instead of the dashboard.
    @router.get("", include_in_schema=False)
    def admin_root() -> RedirectResponse:
        return RedirectResponse("/admin/", status_code=307)

    router.include_router(dashboard.router)
    router.include_router(approvals.router)
    router.include_router(access.router)
    router.include_router(products.router)
    router.include_router(categories.router)
    router.include_router(promocodes.router)
    router.include_router(orders.router)
    router.include_router(returns.router)
    router.include_router(inventory.router)
    router.include_router(consignment.router)
    router.include_router(money_boxes.router)
    router.include_router(reports.router)
    router.include_router(search.router)
    router.include_router(users.router)
    router.include_router(sessions.router)
    router.include_router(audit.router)
    router.include_router(content.router)
    router.include_router(branches.router)
    router.include_router(exports.router)
    router.include_router(invoice_views.admin_router)
    router.include_router(uploads.router)
    return router
