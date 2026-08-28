"""Admin branch management (Part I §6).

Branches carry their own data — coordinates for the embedded map, and a weekly
opening schedule — so they get their own screen rather than sharing the content
page. One-off closures are homepage announcements instead, per §4's decision.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.identity import User
from app.services import content_admin
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/branches")


@router.get("")
def branch_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_branches")),
    db: Session = Depends(get_db),
) -> Response:
    rows = content_admin.branches(db)
    return templates.TemplateResponse(
        request,
        "admin/branches/list.html",
        admin_context(
            db, staff,
            branches=rows,
            hours={b.pk_branch_id: content_admin.branch_hours(db, b.pk_branch_id) for b in rows},
            weekdays=content_admin.WEEKDAYS,
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("")
async def save_branch(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_branches")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    branch = content_admin.save_branch(
        db,
        branch_id=_int(form.get("branch_id")),
        name_ar=str(form.get("name_ar") or ""),
        name_en=str(form.get("name_en") or ""),
        phone_country_code=str(form.get("phone_country_code") or ""),
        phone_number=str(form.get("phone_number") or ""),
        address_ar=str(form.get("address_ar") or ""),
        address_en=str(form.get("address_en") or ""),
        latitude=_decimal(form.get("latitude")),
        longitude=_decimal(form.get("longitude")),
        is_pickup_point=form.get("is_pickup_point") == "1",
        sort_order=_int(form.get("sort_order")) or 0,
        actor_user_id=staff.pk_user_id,
    )

    # Weekly hours arrive as opens_<weekday> / closes_<weekday> / closed_<weekday>.
    hours: dict[int, tuple[str | None, str | None, bool]] = {}
    for weekday in content_admin.WEEKDAYS:
        if f"opens_{weekday}" in form or f"closed_{weekday}" in form:
            hours[weekday] = (
                str(form.get(f"opens_{weekday}") or "") or None,
                str(form.get(f"closes_{weekday}") or "") or None,
                form.get(f"closed_{weekday}") == "1",
            )
    if hours:
        content_admin.save_branch_hours(
            db, branch.pk_branch_id, hours, actor_user_id=staff.pk_user_id
        )

    db.commit()
    return _back("saved")


@router.post("/{branch_id}/remove")
def remove_branch(
    request: Request,
    branch_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_branches")),
    db: Session = Depends(get_db),
) -> Response:
    content_admin.remove_branch(db, branch_id, actor_user_id=staff.pk_user_id)
    db.commit()
    return _back("saved")


def _back(flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/branches?flash={flash}", status_code=status.HTTP_303_SEE_OTHER
    )


def _int(raw) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _decimal(raw) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid coordinate.") from exc
