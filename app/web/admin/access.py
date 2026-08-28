"""Access management admin UI (§2.2, §2.2.1, §2.2.2)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.access import PermissionGrant
from app.models.enums import ApprovalMode, GrantScope
from app.models.identity import User
from app.services import access_admin, approvals
from app.services import access_actions  # noqa: F401 - registers replay handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/access")


@router.get("")
def access_page(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("access", "view")),
    db: Session = Depends(get_db),
) -> Response:
    grants = list(
        db.scalars(
            select(PermissionGrant)
            .where(PermissionGrant.scd_active_flag.is_(True))
            .options(
                selectinload(PermissionGrant.permission),
                selectinload(PermissionGrant.checkers),
                selectinload(PermissionGrant.login_as_scopes),
            )
            .order_by(PermissionGrant.fk_permission_id, PermissionGrant.pk_permission_grant_id)
        ).all()
    )
    roles = access_admin.active_roles(db)
    users = access_admin.active_users(db)
    return templates.TemplateResponse(
        request,
        "admin/access/index.html",
        admin_context(
            db,
            staff,
            permissions=access_admin.active_permissions(db),
            grants=grants,
            roles=roles,
            staff_roles=[role for role in roles if role.is_staff_flag],
            users=users,
            role_map={role.pk_role_id: role for role in roles},
            user_map={user.pk_user_id: user for user in users},
            modes=list(ApprovalMode),
            scopes=list(GrantScope),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/grants")
async def grant_permission(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("access", "grant_permission")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _grant_params(form)
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="access",
        action="grant_permission",
        params=params,
        summary_en="Grant or update permission",
        target_table="scd_permission_grant",
    )
    db.commit()
    return _redirect("pending" if result.pending else "saved")


@router.post("/grants/mode")
async def set_approval_mode(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("access", "set_approval_mode")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _grant_params(form)
    params["granted"] = str(form.get("granted", "1")) != "0"
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="access",
        action="set_approval_mode",
        params=params,
        summary_en="Update approval mode",
        target_table="scd_permission_grant",
    )
    db.commit()
    return _redirect("pending" if result.pending else "saved")


@router.post("/grants/revoke")
def revoke_permission(
    request: Request,
    permission_id: int = Form(...),
    grant_scope: str = Form(...),
    role_id: str | None = Form(None),
    user_id: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("access", "revoke_permission")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="access",
        action="revoke_permission",
        params={
            "permission_id": permission_id,
            "grant_scope": grant_scope,
            "role_id": _int_or_none(role_id),
            "user_id": _int_or_none(user_id),
        },
        summary_en="Revoke permission",
        target_table="scd_permission_grant",
    )
    db.commit()
    return _redirect("pending" if result.pending else "saved")


def _grant_params(form) -> dict[str, object]:
    params: dict[str, object] = {
        "permission_id": int(form["permission_id"]),
        "grant_scope": str(form["grant_scope"]),
        "role_id": _int_or_none(form.get("role_id")),
        "user_id": _int_or_none(form.get("user_id")),
        "approval_mode": str(form.get("approval_mode") or ApprovalMode.SINGLE),
        "required_approvals": int(form.get("required_approvals") or 1),
        "checker_count": int(form.get("checker_count") or 0),
        "login_scope_count": int(form.get("login_scope_count") or 0),
    }
    for index in range(1, int(params["checker_count"]) + 1):
        params[f"checker_scope_{index}"] = form.get(f"checker_scope_{index}") or None
        params[f"checker_role_id_{index}"] = _int_or_none(form.get(f"checker_role_id_{index}"))
        params[f"checker_user_id_{index}"] = _int_or_none(form.get(f"checker_user_id_{index}"))
    for index in range(1, int(params["login_scope_count"]) + 1):
        params[f"login_scope_{index}"] = form.get(f"login_scope_{index}") or None
        params[f"login_scope_role_id_{index}"] = _int_or_none(form.get(f"login_scope_role_id_{index}"))
        params[f"login_scope_user_id_{index}"] = _int_or_none(form.get(f"login_scope_user_id_{index}"))
    return params


def _int_or_none(raw) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    return int(raw)


def _decimal(raw) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid number.") from exc


def _redirect(flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/access?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
