"""Customer-initiated returns (Part I §12, §2.6).

§12 says returns are "supported after purchase, with a return reason captured".
Until now they could only be raised by staff, which meant a customer had to
telephone the shop to start one.

A customer-raised return is the *same* record a staff-raised one produces and
enters the *same* inspection gate — §12 is explicit that not every return
auto-refunds, and letting a customer skip that would let them approve their own
refund.

Everything here is scoped to the signed-in customer's own orders. An order
number is not a credential.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import OrderStatus
from app.models.identity import User
from app.models.orders import Order, OrderReturn
from app.services import returns
from app.web.account import _current_user, _login_redirect_if_needed

log = get_logger(__name__)
router = APIRouter(prefix="/account/returns", tags=["account"])

#: Error codes the form may be redirected back with. A closed set, because the
#: value comes off the query string: rendering an arbitrary string back to the
#: customer would let a crafted link put words in the shop's mouth.
FORM_ERRORS = frozenset({"no_items", "too_many", "no_reason"})


#: Sharing account.py's helpers keeps every ``/account/*`` page bouncing an
#: anonymous visitor to the login form the same way, rather than showing an
#: error page on some and a redirect on others. ``_current_user`` also re-loads
#: the row in *this* request's session, which the return services write to.
_customer = _current_user


def _back_to_form(order_number: str, error: str) -> RedirectResponse:
    return RedirectResponse(
        f"/account/returns/new/{quote(order_number, safe='')}?error={error}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _own_order(db: Session, order_number: str, user: User) -> Order:
    """Fetch an order, scoped to this customer.

    Scoping here rather than checking afterwards means a guessed order number
    resolves to nothing at all.
    """
    order = db.scalars(
        select(Order).where(
            Order.order_number == order_number,
            Order.fk_user_id == user.pk_user_id,
            Order.scd_active_flag.is_(True),
        )
    ).first()
    if order is None:
        raise NotFound("That order could not be found.")
    return order


@router.get("")
def my_returns(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    """The customer's own returns and their current state."""
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _customer(request, db)

    rows = db.scalars(
        select(OrderReturn)
        .where(
            OrderReturn.fk_user_id == user.pk_user_id,
            OrderReturn.scd_active_flag.is_(True),
        )
        .order_by(OrderReturn.pk_order_return_id.desc())
    ).all()

    orders = {
        o.pk_order_id: o
        for o in db.scalars(
            select(Order).where(
                Order.pk_order_id.in_([r.fk_order_id for r in rows] or [0])
            )
        ).all()
    }

    return templates.TemplateResponse(
        request,
        "account/returns.html",
        {
            "returns": rows,
            "orders": orders,
            "lines_for": {
                r.pk_order_return_id: returns.lines_for(db, r) for r in rows
            },
            "flash": request.query_params.get("flash"),
        },
    )


@router.get("/new/{order_number}")
def request_form(
    request: Request, order_number: str, db: Session = Depends(get_db)
) -> Response:
    """Choose what to send back, and why."""
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _customer(request, db)
    order = _own_order(db, order_number, user)

    returnable = returns.returnable_lines(db, order, delivered_only=True)
    error = request.query_params.get("error")

    return templates.TemplateResponse(
        request,
        "account/return_request.html",
        {
            "order": order,
            "returnable": returnable,
            "reasons": returns.RETURN_REASONS,
            "error": error if error in FORM_ERRORS else None,
        },
    )


@router.post("/new/{order_number}")
async def submit_request(
    request: Request, order_number: str, db: Session = Depends(get_db)
) -> Response:
    """Raise the return.

    Quantities are validated by the service against what was actually delivered
    and not already returned, so a customer cannot claim more than they have.
    """
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _customer(request, db)
    order = _own_order(db, order_number, user)

    if order.status == OrderStatus.CANCELLED:
        raise Conflict("A cancelled order has nothing to return.")

    form = await request.form()
    lines = [
        returns.ReturnLineRequest(
            order_line_id=int(key.removeprefix("qty_")), quantity=int(value)
        )
        for key, value in form.items()
        if key.startswith("qty_") and str(value).strip().isdigit() and int(value) > 0
    ]
    if not lines:
        return _back_to_form(order_number, "no_items")

    reason_code = str(form.get("reason_code") or "")
    if reason_code not in returns.RETURN_REASONS:
        return _back_to_form(order_number, "no_reason")

    try:
        order_return = returns.request_return(
            db,
            order,
            lines,
            reason_code=reason_code,
            reason_detail=str(form.get("reason_detail") or "") or None,
            requested_by=user,
            delivered_only=True,
        )
    except ValidationFailed:
        # Most likely a stale form: another return was raised against the same
        # line while this one sat open. Send them back to a freshly computed
        # set of quantities rather than to an error page.
        db.rollback()
        return _back_to_form(order_number, "too_many")

    db.commit()

    log.info(
        "customer_return_requested",
        extra={
            "return": order_return.return_number,
            "order": order.order_number,
            "user_id": user.pk_user_id,
        },
    )

    return RedirectResponse(
        "/account/returns?flash=submitted", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{return_number}/cancel")
def withdraw(
    request: Request, return_number: str, db: Session = Depends(get_db)
) -> Response:
    """Withdraw a request that has not been inspected yet.

    Only while it is still REQUESTED: once staff have inspected it the outcome
    is theirs to record, not the customer's to erase.
    """
    redirect = _login_redirect_if_needed(request)
    if redirect:
        return redirect
    user = _customer(request, db)

    order_return = db.scalars(
        select(OrderReturn).where(
            OrderReturn.return_number == return_number,
            OrderReturn.fk_user_id == user.pk_user_id,
            OrderReturn.scd_active_flag.is_(True),
        )
    ).first()
    if order_return is None:
        raise NotFound("That return could not be found.")

    returns.withdraw(db, order_return, by_user=user)
    db.commit()

    return RedirectResponse(
        "/account/returns?flash=withdrawn", status_code=status.HTTP_303_SEE_OTHER
    )
