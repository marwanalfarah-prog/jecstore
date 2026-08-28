"""Admin financial reports and operating costs (Part I §11)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.identity import User
from app.models.inventory import Branch, OperatingCost
from app.models.money import MoneyBox
from app.services import approvals, money
from app.services import money_actions  # noqa: F401 - registers replay handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, has_permission, require_permission

router = APIRouter(prefix="/reports")


@router.get("")
def financial_reports(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("reports", "view_financials")),
    db: Session = Depends(get_db),
) -> Response:
    today = dt.date.today()
    start_date = _date(request.query_params.get("start")) or today.replace(day=1)
    end_date = _date(request.query_params.get("end")) or today
    category = request.query_params.get("category") or None

    statement = money.financial_statement(
        db,
        start_date=start_date,
        end_date=end_date,
    )
    costs = money.operating_costs(
        db,
        start_date=start_date,
        end_date=end_date,
        category_code=category,
    )
    return templates.TemplateResponse(
        request,
        "admin/reports/index.html",
        admin_context(
            db,
            staff,
            statement=statement,
            costs=costs,
            cost_categories=_cost_categories(db),
            selected_category=category,
            boxes=_boxes(db),
            branches=_branches(db),
            can_record_cost=has_permission(
                db, staff, "money_boxes", "create_transaction"
            ),
            # Every report the export layer serves, so the screen lists all of
            # them instead of the two that happened to be wired to buttons.
            reports=_directory(),
            presets=_presets(today),
            start_date=start_date,
            end_date=end_date,
            can_export=has_permission(db, staff, "reports", "export"),
            flash=request.query_params.get("flash"),
        ),
    )


#: Which reports take a date range, in the order they are worth reading.
#: Driven off ``REPORT_KEYS`` so a report added to the export layer cannot be
#: left off this screen — the previous version hard-coded two of the seven.
_DATED = {
    "financial_statement",
    "sales",
    "operating_costs",
    "promocodes",
    "returns",
    "staff_activity",
}


def _presets(today: dt.date) -> dict[str, tuple[str, str]]:
    """The three periods anyone actually asks for, as ready-made date pairs.

    Typing two dates to answer "how did last month go?" is the commonest thing
    done on this screen; these make it one press. Computed here rather than in
    the template because month arithmetic across a January boundary is not
    something Jinja should be doing.
    """
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - dt.timedelta(days=1)

    return {
        "this_month": (first_of_month.isoformat(), today.isoformat()),
        "last_month": (
            last_month_end.replace(day=1).isoformat(),
            last_month_end.isoformat(),
        ),
        "this_year": (today.replace(month=1, day=1).isoformat(), today.isoformat()),
    }


def _directory() -> list[dict[str, object]]:
    from app.services.report_datasets import REPORT_KEYS

    return [
        {
            "key": key,
            "dated": key in _DATED,
            "title_key": f"exports.{key}",
            "about_key": f"reports.about_{key}",
        }
        for key in REPORT_KEYS
    ]


@router.post("/operating-costs")
def record_operating_cost(
    request: Request,
    name_ar: str = Form(...),
    name_en: str = Form(...),
    category_code: str = Form(...),
    amount_amt: str = Form(...),
    incurred_date: str = Form(...),
    money_box_id: str = Form(...),
    is_recurring_flag: bool = Form(False),
    recurrence_months: str | None = Form(None),
    branch_id: str | None = Form(None),
    note: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("money_boxes", "create_transaction")),
    db: Session = Depends(get_db),
) -> Response:
    date_value = _date(incurred_date)
    if date_value is None:
        raise ValidationFailed("Incurred date is required.")
    box_id = _int_or_none(money_box_id)
    if box_id is None:
        raise ValidationFailed("Choose which money box paid this cost.")

    params = {
        "operation": "operating_cost",
        "name_ar": name_ar,
        "name_en": name_en,
        "category_code": category_code,
        "amount_amt": _decimal(amount_amt) or Decimal("0"),
        "incurred_date": date_value,
        "money_box_id": box_id,
        "is_recurring_flag": bool(is_recurring_flag),
        "recurrence_months": _int_or_none(recurrence_months),
        "branch_id": _int_or_none(branch_id),
        "note": note,
    }
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="money_boxes",
        action="create_transaction",
        params=params,
        summary_en=f"Record operating cost {name_en}",
        target_table="scd_operating_cost",
    )
    db.commit()
    return RedirectResponse(
        f"/admin/reports?flash={'pending' if result.pending else 'saved'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _boxes(db: Session) -> list[MoneyBox]:
    return list(
        db.scalars(
            select(MoneyBox)
            .where(MoneyBox.scd_active_flag.is_(True), MoneyBox.is_open_flag.is_(True))
            .order_by(MoneyBox.box_code)
        ).all()
    )


def _branches(db: Session) -> list[Branch]:
    return list(
        db.scalars(
            select(Branch)
            .where(Branch.scd_active_flag.is_(True))
            .order_by(Branch.sort_order, Branch.pk_branch_id)
        ).all()
    )


def _cost_categories(db: Session) -> list[str]:
    return [
        row[0]
        for row in db.execute(
            select(OperatingCost.category_code)
            .where(OperatingCost.scd_active_flag.is_(True))
            .distinct()
            .order_by(OperatingCost.category_code)
        ).all()
    ]


def _date(raw) -> dt.date | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValidationFailed("That is not a valid date.") from exc


def _decimal(raw) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid number.") from exc


def _int_or_none(raw) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    return int(raw)
