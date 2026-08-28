"""Invoice endpoints for staff and customers (Part I §9).

Two entry points, deliberately scoped differently:

* ``/admin/orders/{id}/invoice.{fmt}`` — staff, gated on ``orders.view``.
* ``/account/orders/{number}/invoice.{fmt}`` — the customer, scoped to their own
  orders so an order number cannot expose somebody else's invoice.

Both render the same document, so what the counter prints and what the customer
downloads cannot drift apart.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.context import get_context
from app.core.errors import NotAuthenticated, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.identity import User
from app.models.orders import Order
from app.services import invoices
from app.services.permissions import GrantDecision
from app.web.admin.deps import current_staff, require_permission

log = get_logger(__name__)

FORMATS = {"html", "pdf"}

#: Staff-facing, mounted under the admin router.
admin_router = APIRouter(prefix="/orders")

#: Customer-facing, mounted under /account.
account_router = APIRouter(prefix="/account/orders")


def _respond(request: Request, invoice: invoices.Invoice, fmt: str) -> Response:
    if fmt == "pdf":
        payload = invoices.render_pdf(request, invoice)
        return Response(
            payload,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    f'inline; filename="{invoices.document_filename(invoice, "pdf")}"'
                ),
                "Content-Length": str(len(payload)),
            },
        )
    return Response(invoices.render_html(request, invoice), media_type="text/html")


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------


@admin_router.get("/{order_id}/invoice.{fmt}")
def staff_invoice(
    request: Request,
    order_id: int,
    fmt: str,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("orders", "view")),
    db: Session = Depends(get_db),
) -> Response:
    if fmt not in FORMATS:
        raise ValidationFailed("Unsupported invoice format.")

    order = db.get(Order, order_id)
    if order is None or not order.scd_active_flag:
        raise NotFound("That order does not exist.")

    invoice = invoices.build(db, order, language=get_context(request).language)
    return _respond(request, invoice, fmt)


@admin_router.post("/{order_id}/invoice/email")
def email_invoice(
    request: Request,
    order_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("orders", "view")),
    db: Session = Depends(get_db),
) -> Response:
    """Send the customer their invoice (§9: "printable/emailable")."""
    order = db.get(Order, order_id)
    if order is None or not order.scd_active_flag:
        raise NotFound("That order does not exist.")

    customer_language = "ar"
    invoice = invoices.build(db, order, language=customer_language)
    invoices.queue_invoice_email(db, invoice)
    db.commit()

    return RedirectResponse(
        f"/admin/orders/{order_id}?flash=saved", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


@account_router.get("/{order_number}/invoice")
@account_router.get("/{order_number}/invoice.{fmt}")
def customer_invoice(
    request: Request,
    order_number: str,
    fmt: str = "html",
    db: Session = Depends(get_db),
) -> Response:
    """The customer's own invoice (§2.6: view/download past invoices)."""
    context = get_context(request)
    if context.user is None:
        raise NotAuthenticated("Please sign in to view your invoice.")
    if fmt not in FORMATS:
        raise ValidationFailed("Unsupported invoice format.")

    invoice = invoices.for_order_number(
        db,
        order_number,
        # Scoped to this customer — an order number must never be enough on
        # its own to read somebody else's invoice.
        user_id=context.user.pk_user_id,
        language=context.language,
    )
    return _respond(request, invoice, fmt)
