"""Admin user management and Login-As start route (§2.1, §2.2.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.context import get_context
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import Language
from app.models.identity import Role, User
from app.services import access_admin, approvals
from app.services import access_actions  # noqa: F401 - registers replay handlers
from app.services.activity import record_event
from app.services.audit import record_audit
from app.services.permissions import GrantDecision
from app.services.sessions import set_session_cookie
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/users")


@router.get("")
def user_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("users", "view")),
    db: Session = Depends(get_db),
) -> Response:
    role_filter = request.query_params.get("role")
    q = (request.query_params.get("q") or "").strip().lower()
    stmt = (
        select(User)
        .where(User.scd_active_flag.is_(True))
        .options(selectinload(User.role))
        .order_by(User.username)
    )
    if role_filter:
        stmt = stmt.where(User.fk_role_id == int(role_filter))
    if q:
        stmt = stmt.where((User.username.contains(q)) | (User.email.contains(q)))
    users = list(db.scalars(stmt).all())
    roles = access_admin.active_roles(db)
    return templates.TemplateResponse(
        request,
        "admin/users/list.html",
        admin_context(
            db,
            staff,
            users=users,
            roles=roles,
            staff_roles=[role for role in roles if role.is_staff_flag],
            languages=list(Language),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/new")
def create_staff(
    request: Request,
    role_id: int = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    preferred_language: str = Form(Language.AR),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("users", "create_staff")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="users",
        action="create_staff",
        params={
            "role_id": role_id,
            "username": username,
            "email": email,
            "password": password,
            "preferred_language": preferred_language,
        },
        summary_en=f"Create staff account {username}",
        target_table="scd_user",
    )
    db.commit()
    return _redirect("pending" if result.pending else "saved")


@router.post("/{user_id}/deactivate")
def deactivate_user(
    request: Request,
    user_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("users", "deactivate")),
    db: Session = Depends(get_db),
) -> Response:
    target = db.get(User, user_id)
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="users",
        action="deactivate",
        params={"user_id": user_id},
        summary_en=f"Deactivate user {target.username if target else user_id}",
        target_table="scd_user",
        target_row_id=user_id,
    )
    db.commit()
    return _redirect("pending" if result.pending else "saved")


@router.post("/{user_id}/login-as")
def login_as(
    request: Request,
    user_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("access", "login_as")),
    db: Session = Depends(get_db),
) -> Response:
    if decision.needs_approval:
        result = approvals.execute_or_submit(
            db,
            request,
            decision,
            staff,
            module="access",
            action="login_as",
            params={"target_user_id": user_id},
            summary_en=f"Request Login-As for user #{user_id}",
            target_table="scd_user",
            target_row_id=user_id,
        )
        db.commit()
        return _redirect("pending" if result.pending else "saved")

    ctx = get_context(request)
    session = access_admin.start_impersonation(
        db,
        request,
        impersonator=staff,
        target_user_id=user_id,
        grant_id=decision.grant_id,
        parent_session_key=ctx.session_key,
        context=ctx,
    )
    record_audit(
        db,
        request,
        actor=staff,
        module="access",
        action="login_as",
        target_table="scd_user",
        target_row_id=user_id,
        note=f"Login-As started for user #{user_id}",
    )
    db.commit()
    response = RedirectResponse("/admin/", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, session)
    return response


def _redirect(flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/users?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
