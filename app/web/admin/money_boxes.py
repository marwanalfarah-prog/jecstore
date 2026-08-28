"""Admin money-box screens (Part I §10)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import MoneyDirection, MoneyReason
from app.models.identity import User
from app.models.inventory import Branch
from app.models.money import MoneyBox, MoneyBoxReconciliation
from app.models.orders import PaymentChannel
from app.services import approvals, money
from app.services import money_actions  # noqa: F401 - registers replay handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/money-boxes")


@router.get("")
def list_boxes(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("money_boxes", "view")),
    db: Session = Depends(get_db),
) -> Response:
    boxes = _boxes(db, open_only=request.query_params.get("open") == "1")
    balances = money.box_balances(db)
    branch_map = {b.pk_branch_id: b for b in _branches(db)}
    return templates.TemplateResponse(
        request,
        "admin/money_boxes/list.html",
        admin_context(
            db,
            staff,
            boxes=boxes,
            balances=balances,
            branch_map=branch_map,
            total_balance=sum(balances.values(), Decimal("0")),
            flash=request.query_params.get("flash"),
        ),
    )


@router.get("/new")
def new_box(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("money_boxes", "create_box")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/money_boxes/new.html",
        admin_context(db, staff, branches=_branches(db)),
    )


@router.post("/new")
def create_box(
    request: Request,
    box_code: str = Form(...),
    name_ar: str = Form(...),
    name_en: str = Form(...),
    opening_balance_amt: str | None = Form(None),
    branch_id: str | None = Form(None),
    description: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("money_boxes", "create_box")),
    db: Session = Depends(get_db),
) -> Response:
    params = {
        "operation": "create_box",
        "box_code": box_code,
        "name_ar": name_ar,
        "name_en": name_en,
        "opening_balance_amt": _decimal(opening_balance_amt) or Decimal("0"),
        "branch_id": _int_or_none(branch_id),
        "description": description,
    }
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="money_boxes",
        action="create_box",
        params=params,
        summary_en=f"Create money box {box_code}",
        target_table="scd_money_box",
    )
    db.commit()
    if result.pending:
        return _redirect("/admin/money-boxes", "pending")
    box = result.value
    return _redirect(f"/admin/money-boxes/{box.pk_money_box_id}", "saved")


@router.post("/transaction")
async def manual_transaction(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("money_boxes", "create_transaction")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    direction = str(form.get("direction") or MoneyDirection.IN)
    reason_code = str(form.get("reason_code") or MoneyReason.OTHER)
    allocation_count = int(form.get("allocation_count") or 0)
    params: dict[str, object] = {
        "operation": "transaction",
        "direction": direction,
        "reason_code": reason_code,
        "channel_id": _int_or_none(form.get("channel_id")),
        "description": form.get("description") or None,
        "occurred_date": _date(form.get("occurred_date")),
        "allocation_count": allocation_count,
    }
    first_box_id: int | None = None
    for index in range(1, allocation_count + 1):
        box_id = _int_or_none(form.get(f"allocation_box_id_{index}"))
        amount = _decimal(form.get(f"allocation_amount_amt_{index}"))
        if box_id is None or amount is None:
            continue
        first_box_id = first_box_id or box_id
        params[f"allocation_box_id_{index}"] = box_id
        params[f"allocation_amount_amt_{index}"] = amount

    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="money_boxes",
        action="create_transaction",
        params=params,
        summary_en=f"Record {reason_code.replace('_', ' ')} money movement",
        target_table="trx_money_transaction",
        target_row_id=first_box_id,
    )
    db.commit()
    target_box = _int_or_none(form.get("target_box_id")) or first_box_id
    location = f"/admin/money-boxes/{target_box}" if target_box else "/admin/money-boxes"
    return _redirect(location, "pending" if result.pending else "saved")


@router.get("/{box_id}")
def detail(
    request: Request,
    box_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("money_boxes", "view")),
    db: Session = Depends(get_db),
) -> Response:
    box = _get_box(db, box_id)
    start_date = _date(request.query_params.get("start"))
    end_date = _date(request.query_params.get("end"))
    reason_code = request.query_params.get("reason") or None
    return templates.TemplateResponse(
        request,
        "admin/money_boxes/detail.html",
        admin_context(
            db,
            staff,
            box=box,
            balance=money.box_balance(db, box_id),
            ledger=money.box_ledger(
                db,
                box_id,
                start_date=start_date,
                end_date=end_date,
                reason_code=reason_code,
            ),
            boxes=_boxes(db, open_only=True),
            channels=_channels(db),
            directions=list(MoneyDirection),
            reasons=list(MoneyReason),
            reconciliations=_reconciliations(db, box_id),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/{box_id}/reconcile")
def reconcile(
    request: Request,
    box_id: int,
    counted_amt: str = Form(...),
    note: str | None = Form(None),
    adjust: bool = Form(False),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("money_boxes", "reconcile")),
    db: Session = Depends(get_db),
) -> Response:
    box = _get_box(db, box_id)
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="money_boxes",
        action="reconcile",
        params={
            "money_box_id": box_id,
            "counted_amt": _decimal(counted_amt) or Decimal("0"),
            "note": note,
            "adjust": bool(adjust),
        },
        summary_en=f"Reconcile money box {box.box_code}",
        target_table="scd_money_box_reconciliation",
        target_row_id=box_id,
    )
    db.commit()
    return _redirect(f"/admin/money-boxes/{box_id}", "pending" if result.pending else "saved")


@router.post("/{box_id}/close")
def close_box(
    request: Request,
    box_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("money_boxes", "close_box")),
    db: Session = Depends(get_db),
) -> Response:
    box = _get_box(db, box_id)
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="money_boxes",
        action="close_box",
        params={"money_box_id": box_id},
        summary_en=f"Close money box {box.box_code}",
        target_table="scd_money_box",
        target_row_id=box_id,
    )
    db.commit()
    return _redirect(f"/admin/money-boxes/{box_id}", "pending" if result.pending else "saved")


def _get_box(db: Session, box_id: int) -> MoneyBox:
    box = db.get(MoneyBox, box_id)
    if box is None or not box.scd_active_flag:
        raise NotFound("That money box does not exist.")
    return box


def _boxes(db: Session, *, open_only: bool = False) -> list[MoneyBox]:
    stmt = (
        select(MoneyBox)
        .where(MoneyBox.scd_active_flag.is_(True))
        .order_by(MoneyBox.box_code)
    )
    if open_only:
        stmt = stmt.where(MoneyBox.is_open_flag.is_(True))
    return list(db.scalars(stmt).all())


def _branches(db: Session) -> list[Branch]:
    return list(
        db.scalars(
            select(Branch)
            .where(Branch.scd_active_flag.is_(True))
            .order_by(Branch.sort_order, Branch.pk_branch_id)
        ).all()
    )


def _channels(db: Session) -> list[PaymentChannel]:
    return list(
        db.scalars(
            select(PaymentChannel)
            .where(PaymentChannel.scd_active_flag.is_(True))
            .order_by(PaymentChannel.sort_order, PaymentChannel.pk_payment_channel_id)
        ).all()
    )


def _reconciliations(db: Session, box_id: int) -> list[MoneyBoxReconciliation]:
    return list(
        db.scalars(
            select(MoneyBoxReconciliation)
            .where(
                MoneyBoxReconciliation.fk_money_box_id == box_id,
                MoneyBoxReconciliation.scd_active_flag.is_(True),
            )
            .order_by(MoneyBoxReconciliation.counted_dt.desc())
        ).all()
    )


def _decimal(raw) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid number.") from exc


def _date(raw) -> dt.date | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValidationFailed("That is not a valid date.") from exc


def _int_or_none(raw) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    return int(raw)


def _redirect(location: str, flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"{location}?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
