"""Admin audit log viewer (§2.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.templating import templates
from app.db.session import get_db
from app.models.access import AuditLog
from app.models.identity import User
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/audit")

#: The newest N entries. The log is append-only and grows forever, so this read
#: is bounded (Part II §2) — and the screen says so under the table rather than
#: presenting a cut-off list as the whole log.
ENTRY_LIMIT = 200


@router.get("")
def audit_log(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("settings", "view_audit_log")),
    db: Session = Depends(get_db),
) -> Response:
    stmt = (
        select(AuditLog)
        .options(selectinload(AuditLog.fields))
        .order_by(AuditLog.created_dt.desc())
        .limit(ENTRY_LIMIT)
    )
    module = request.query_params.get("module") or None
    action = request.query_params.get("action") or None
    target_table = request.query_params.get("target_table") or None
    actor = request.query_params.get("actor") or None
    if module:
        stmt = stmt.where(AuditLog.module_code == module)
    if action:
        stmt = stmt.where(AuditLog.action_code == action)
    if target_table:
        stmt = stmt.where(AuditLog.target_table == target_table)
    if actor:
        stmt = stmt.where(AuditLog.actor_username.contains(actor))

    rows = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        request,
        "admin/audit/index.html",
        admin_context(
            db,
            staff,
            entries=rows,
            entry_limit=ENTRY_LIMIT,
            module_options=_distinct(db, AuditLog.module_code),
            action_options=_distinct(db, AuditLog.action_code),
            table_options=_distinct(db, AuditLog.target_table),
        ),
    )


def _distinct(db: Session, column) -> list[str]:
    return [
        row[0]
        for row in db.execute(
            select(column)
            .where(column.is_not(None))
            .distinct()
            .order_by(column)
        ).all()
        if row[0]
    ]
