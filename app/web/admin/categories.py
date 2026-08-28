"""Admin category screens."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import DiscountKind
from app.models.identity import User
from app.services import approvals, catalog_admin
from app.services import catalog_actions  # noqa: F401 - registers replay handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/categories")


@router.get("")
def category_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "view")),
    db: Session = Depends(get_db),
) -> Response:
    categories = catalog_admin.active_categories(db)
    return templates.TemplateResponse(
        request,
        "admin/categories/list.html",
        admin_context(
            db,
            staff,
            categories=categories,
            product_counts=catalog_admin.category_product_counts(db),
            discount_kinds=list(DiscountKind),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("")
async def create_category(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "create_category")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _category_params(form)
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="create_category",
        params=params,
        summary_en=f"Create category {params['name_en']}",
        target_table="scd_category",
    )
    db.commit()
    return _back("pending" if result.pending else "saved")


@router.post("/{category_id}")
async def update_category(
    request: Request,
    category_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "create_category")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _category_params(form)
    params.update({"operation": "update", "category_id": category_id})
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="create_category",
        params=params,
        summary_en=f"Update category {category_id}",
        target_table="scd_category",
        target_row_id=category_id,
    )
    db.commit()
    return _back("pending" if result.pending else "saved")


@router.post("/{category_id}/close")
def close_category(
    request: Request,
    category_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "delete_category")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="delete_category",
        params={"category_id": category_id},
        summary_en=f"Close category {category_id}",
        target_table="scd_category",
        target_row_id=category_id,
    )
    db.commit()
    return _back("pending" if result.pending else "saved")


@router.post("/{category_id}/discounts")
async def create_category_discount(
    request: Request,
    category_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "apply_discount")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = {
        "name_ar": str(form.get("discount_name_ar") or "").strip(),
        "name_en": str(form.get("discount_name_en") or "").strip(),
        "discount_scope": "category",
        "product_id": None,
        "category_id": category_id,
        "include_subcategories": form.get("include_subcategories") == "1",
        "discount_kind": str(form.get("discount_kind") or DiscountKind.PERCENTAGE.value),
        "percentage": _decimal(form.get("percentage")),
        "fixed_price_amt": _decimal(form.get("fixed_price_amt")),
        "starts_dt": _datetime(form.get("starts_dt")),
        "ends_dt": _datetime(form.get("ends_dt")),
        "priority": _optional_int(form.get("priority")) or 0,
    }
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="apply_discount",
        params=params,
        summary_en=f"Apply discount to category {category_id}",
        target_table="scd_discount",
    )
    db.commit()
    return _back("pending" if result.pending else "saved")


def _category_params(form) -> dict:
    return {
        "name_ar": str(form.get("name_ar") or "").strip(),
        "name_en": str(form.get("name_en") or "").strip(),
        "parent_category_id": _optional_int(form.get("parent_category_id")),
        "slug_ar": _text(form.get("slug_ar")),
        "slug_en": _text(form.get("slug_en")),
        "description_ar": _text(form.get("description_ar")),
        "description_en": _text(form.get("description_en")),
        "image_path": _text(form.get("image_path")),
        "sort_order": _optional_int(form.get("sort_order")) or 0,
        "is_visible": form.get("is_visible") == "1",
    }


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
        f"/admin/categories?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
