"""Admin returns (Part I §12).

The screen deliberately mirrors the gate: you cannot reach the refund controls
until the return has passed inspection, because §12 requires the condition check
to come first and not every return auto-refunds.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import OrderStatus, ReturnStatus
from app.models.identity import User
from app.models.money import MoneyBox
from app.models.orders import Order, OrderLine, OrderReturn
from app.services import approvals, returns
from app.services import return_actions  # noqa: F401 - registers the handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/returns")
PAGE_SIZE = 25


@router.get("")
def return_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("returns", "view")),
    db: Session = Depends(get_db),
) -> Response:
    page = max(int(request.query_params.get("page", 1) or 1), 1)
    rows, total = returns.search_returns(
        db,
        status=request.query_params.get("status"),
        query=request.query_params.get("q"),
        page=page,
        page_size=PAGE_SIZE,
    )
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
        "admin/returns/list.html",
        admin_context(
            db, staff,
            returns=rows,
            orders=orders,
            total=total,
            page=page,
            per_page=PAGE_SIZE,
            total_pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
            statuses=list(ReturnStatus),
        ),
    )


@router.get("/new")
def new_return(
    request: Request,
    order_id: int | None = None,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("returns", "view")),
    db: Session = Depends(get_db),
) -> Response:
    """Raise a return against an order, line by line (§12: partial by default).

    ``order_id`` is optional so the screen can be reached from the Returns list
    rather than only from one order. Without it, the first step is choosing the
    order — which is what a member of staff at the counter holding a book and
    an invoice actually needs. It used to be a required query parameter, so the
    only route to this screen was hand-editing the URL, and following the
    language toggle off it produced a 422.
    """
    if order_id is None:
        return templates.TemplateResponse(
            request,
            "admin/returns/new.html",
            admin_context(
                db, staff,
                order=None,
                returnable=[],
                reasons=returns.RETURN_REASONS,
                candidates=_returnable_orders(db, request.query_params.get("q")),
                query=request.query_params.get("q") or "",
            ),
        )

    order = db.get(Order, order_id)
    if order is None:
        raise NotFound("That order does not exist.")

    return templates.TemplateResponse(
        request,
        "admin/returns/new.html",
        admin_context(
            db, staff,
            order=order,
            returnable=returns.returnable_lines(db, order),
            reasons=returns.RETURN_REASONS,
            candidates=[],
            query="",
        ),
    )


def _returnable_orders(db: Session, query: str | None, *, limit: int = 15) -> list[Order]:
    """Orders a return could plausibly be raised against.

    Cancelled orders are excluded — there is nothing to send back — and the
    rest come newest-first, because a counter return is nearly always recent.
    """
    stmt = (
        select(Order)
        .where(
            Order.status != OrderStatus.CANCELLED,
            Order.scd_active_flag.is_(True),
        )
        .order_by(Order.placed_dt.desc())
        .limit(limit)
    )
    if query:
        stmt = stmt.where(Order.order_number.like(f"%{query.strip().upper()}%"))
    return list(db.scalars(stmt).all())


@router.post("/new")
async def create_return(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("returns", "view")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    order_id = int(form["order_id"])
    order = db.get(Order, order_id)
    if order is None:
        raise NotFound("That order does not exist.")

    # One quantity field per line: qty_<order_line_id>.
    lines = [
        returns.ReturnLineRequest(
            order_line_id=int(key.removeprefix("qty_")), quantity=int(value)
        )
        for key, value in form.items()
        if key.startswith("qty_") and str(value).strip().isdigit() and int(value) > 0
    ]
    if not lines:
        raise ValidationFailed("Select at least one item to return.")

    order_return = returns.request_return(
        db, order, lines,
        reason_code=str(form.get("reason_code", "other")),
        reason_detail=str(form.get("reason_detail") or "") or None,
        requested_by=staff,
    )
    db.commit()

    return RedirectResponse(
        f"/admin/returns/{order_return.pk_order_return_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{return_id}")
def return_detail(
    request: Request,
    return_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("returns", "view")),
    db: Session = Depends(get_db),
) -> Response:
    order_return = _get(db, return_id)
    lines = returns.lines_for(db, order_return)
    order_lines = {
        line.pk_order_line_id: line
        for line in db.scalars(
            select(OrderLine).where(
                OrderLine.pk_order_line_id.in_([l.fk_order_line_id for l in lines] or [0])
            )
        ).all()
    }

    return templates.TemplateResponse(
        request,
        "admin/returns/detail.html",
        admin_context(
            db, staff,
            order_return=order_return,
            order=db.get(Order, order_return.fk_order_id),
            customer=db.get(User, order_return.fk_user_id),
            lines=lines,
            order_lines=order_lines,
            boxes=_boxes(db),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/{return_id}/inspect")
async def inspect(
    request: Request,
    return_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("returns", "inspect")),
    db: Session = Depends(get_db),
) -> Response:
    order_return = _get(db, return_id)
    form = await request.form()

    acceptable = form.get("condition_acceptable") == "1"
    params: dict = {
        "return_id": return_id,
        "condition_acceptable": acceptable,
        "note": str(form.get("note") or "") or None,
    }
    # Per-line restock decisions — the inspector has the item in hand.
    for line in returns.lines_for(db, order_return):
        key = f"restock_{line.pk_order_return_line_id}"
        params[key] = form.get(key) == "1"

    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="returns", action="inspect",
        params=params,
        summary_en=(
            f"Inspect return {order_return.return_number}: "
            f"{'accepted' if acceptable else 'rejected'}"
        ),
        target_table="scd_order_return", target_row_id=return_id,
    )
    db.commit()
    return _back(return_id, result)


@router.post("/{return_id}/refund")
def refund(
    request: Request,
    return_id: int,
    destination: str = Form(...),
    money_box_id: int | None = Form(None),
    amount_amt: str | None = Form(None),
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    """Issue the refund — to a money box, or as رصيد (§12).

    The two destinations are separate permissions, so a role can be allowed to
    grant store credit without being able to take cash out of a till.
    """
    order_return = _get(db, return_id)
    amount = _decimal(amount_amt)

    if destination == "store_credit":
        module_action = ("returns", "issue_store_credit")
        params = {"return_id": return_id, "amount_amt": amount}
        summary = f"Store credit for return {order_return.return_number}"
    else:
        if money_box_id is None:
            raise ValidationFailed("Choose which money box the refund comes out of.")
        module_action = ("returns", "issue_refund")
        params = {
            "return_id": return_id,
            "money_box_id": money_box_id,
            "amount_amt": amount,
        }
        summary = f"Refund for return {order_return.return_number}"

    # Resolved here rather than as a dependency, because which permission
    # applies depends on the destination the user chose.
    from app.services.permissions import require

    decision = require(db, staff, *module_action)
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module=module_action[0], action=module_action[1],
        params=params,
        summary_en=summary,
        target_table="scd_order_return", target_row_id=return_id,
    )
    db.commit()
    return _back(return_id, result)


# ---------------------------------------------------------------------------


def _get(db: Session, return_id: int) -> OrderReturn:
    row = db.scalars(
        select(OrderReturn).where(
            OrderReturn.pk_order_return_id == return_id,
            OrderReturn.scd_active_flag.is_(True),
        )
    ).first()
    if row is None:
        raise NotFound("That return does not exist.")
    return row


def _boxes(db: Session) -> list[MoneyBox]:
    return list(
        db.scalars(
            select(MoneyBox).where(
                MoneyBox.scd_active_flag.is_(True), MoneyBox.is_open_flag.is_(True)
            )
        ).all()
    )


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid amount.") from exc


def _back(return_id: int, result: approvals.ActionResult) -> RedirectResponse:
    flash = "pending" if result.pending else "saved"
    return RedirectResponse(
        f"/admin/returns/{return_id}?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
