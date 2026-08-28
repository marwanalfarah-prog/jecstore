"""Admin session and suspicious-login monitoring (§2.8)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.templating import templates
from app.db.session import get_db
from app.models.activity import LoginAttempt, UserSession
from app.models.identity import User
from app.services import approvals
from app.services import access_actions  # noqa: F401 - registers replay handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/sessions")


@router.get("")
def session_dashboard(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("users", "view_sessions")),
    db: Session = Depends(get_db),
) -> Response:
    active_sessions = list(
        db.scalars(
            select(UserSession)
            .where(UserSession.scd_active_flag.is_(True))
            .order_by(UserSession.last_seen_dt.desc())
        ).all()
    )
    user_ids = {session.fk_user_id for session in active_sessions}
    users = {
        user.pk_user_id: user
        for user in db.scalars(
            select(User)
            .where(User.pk_user_id.in_(user_ids) if user_ids else User.pk_user_id == -1)
            .options(selectinload(User.role))
        ).all()
    }
    attempts = list(
        db.scalars(
            select(LoginAttempt)
            .order_by(LoginAttempt.created_dt.desc())
            .limit(100)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "admin/sessions/index.html",
        admin_context(
            db,
            staff,
            sessions=active_sessions,
            user_map=users,
            attempts=attempts,
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/{session_id}/terminate")
def terminate_session(
    request: Request,
    session_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("users", "terminate_session")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="users",
        action="terminate_session",
        params={"session_id": session_id},
        summary_en=f"Terminate session #{session_id}",
        target_table="scd_user_session",
        target_row_id=session_id,
    )
    db.commit()
    return RedirectResponse(
        f"/admin/sessions?flash={'pending' if result.pending else 'saved'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
