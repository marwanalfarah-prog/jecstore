"""Admin promocode screens."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import PromocodeKind
from app.models.identity import User
from app.services import approvals, catalog_admin, promocode_admin
from app.services import promocode_actions  # noqa: F401 - registers replay handler
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/promocodes")


@router.get("")
def promocode_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_promocodes")),
    db: Session = Depends(get_db),
) -> Response:
    promocodes = promocode_admin.active_promocodes(db)
    return templates.TemplateResponse(
        request,
        "admin/promocodes/list.html",
        admin_context(
            db,
            staff,
            promocodes=promocodes,
            counts=promocode_admin.redemption_counts(db, promocodes),
            categories=catalog_admin.active_categories(db),
            products=catalog_admin.product_options(db),
            kinds=list(PromocodeKind),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("")
async def create_promocode(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("content", "manage_promocodes")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _promocode_params(form)
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="content",
        action="manage_promocodes",
        params=params,
        summary_en=f"Create promocode {params['code']}",
        target_table="scd_promocode",
    )
    db.commit()
    if result.pending:
        return _back("pending")
    return RedirectResponse(
        f"/admin/promocodes/{result.value.pk_promocode_id}?flash=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{promocode_id}")
def promocode_detail(
    request: Request,
    promocode_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_promocodes")),
    db: Session = Depends(get_db),
) -> Response:
    promocode = promocode_admin.get_active(db, promocode_id)
    return templates.TemplateResponse(
        request,
        "admin/promocodes/detail.html",
        admin_context(
            db,
            staff,
            promocode=promocode,
            restrictions=promocode_admin.active_restrictions(db, promocode_id),
            redemption_count=promocode_admin.redemption_counts(db, [promocode])[promocode_id],
            categories=catalog_admin.active_categories(db),
            products=catalog_admin.product_options(db),
            kinds=list(PromocodeKind),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/{promocode_id}")
async def update_promocode(
    request: Request,
    promocode_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("content", "manage_promocodes")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _promocode_params(form)
    params.update({"operation": "update", "promocode_id": promocode_id})
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="content",
        action="manage_promocodes",
        params=params,
        summary_en=f"Update promocode {params['code']}",
        target_table="scd_promocode",
        target_row_id=promocode_id,
    )
    db.commit()
    return RedirectResponse(
        f"/admin/promocodes/{promocode_id}?flash={'pending' if result.pending else 'saved'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{promocode_id}/close")
def close_promocode(
    request: Request,
    promocode_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("content", "manage_promocodes")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="content",
        action="manage_promocodes",
        params={"operation": "close", "promocode_id": promocode_id},
        summary_en=f"Close promocode {promocode_id}",
        target_table="scd_promocode",
        target_row_id=promocode_id,
    )
    db.commit()
    return _back("pending" if result.pending else "saved")


def _promocode_params(form) -> dict:
    params = {
        "code": str(form.get("code") or "").strip(),
        "promocode_kind": str(form.get("promocode_kind") or PromocodeKind.PERCENTAGE.value),
        "name_ar": _text(form.get("name_ar")),
        "name_en": _text(form.get("name_en")),
        "percentage": _decimal(form.get("percentage")),
        "max_discount_amt": _decimal(form.get("max_discount_amt")),
        "fixed_amount_amt": _decimal(form.get("fixed_amount_amt")),
        "minimum_order_amt": _decimal(form.get("minimum_order_amt")),
        "starts_dt": _datetime(form.get("starts_dt")),
        "expires_dt": _datetime(form.get("expires_dt")),
        "single_use_globally": form.get("single_use_globally") == "1",
        "max_total_uses": _optional_int(form.get("max_total_uses")),
        "max_uses_per_customer": _optional_int(form.get("max_uses_per_customer")),
        "stacks_with_item_discount": form.get("stacks_with_item_discount") == "1",
        "applies_to_consigned": form.get("applies_to_consigned") == "1",
        "note": _text(form.get("note")),
    }
    params.update(_restriction_params(form))
    return params


def _restriction_params(form) -> dict:
    packed: dict[str, object] = {}
    count = 0
    for idx in range(1, 4):
        target_type = ""
        target_id = None
        target = str(form.get(f"restriction_{idx}_target") or "").strip()
        if ":" in target:
            target_type, raw_id = target.split(":", 1)
            target_id = _optional_int(raw_id)
        else:
            target_type = str(form.get(f"restriction_{idx}_type") or "").strip()
            target_id = _optional_int(form.get(f"restriction_{idx}_id"))
        if not target_type or target_id is None:
            continue
        count += 1
        packed[f"restriction_{count}_type"] = target_type
        packed[f"restriction_{count}_id"] = target_id
        packed[f"restriction_{count}_exclusion"] = (
            form.get(f"restriction_{idx}_exclusion") == "1"
        )
    packed["restriction_count"] = count
    return packed


def _decimal(value) -> Decimal | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValidationFailed("Enter a valid amount.") from exc


def _optional_int(value) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None
    return int(value)


def _datetime(value) -> dt.datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def _text(value) -> str | None:
    value = str(value or "").strip()
    return value or None


def _back(flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/promocodes?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
