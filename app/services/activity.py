"""Writing to the activity log (Part I §2.8; Part II §6).

Everything here appends to insert-only ``TRX_`` tables. Nothing in this module
ever updates or deletes — which is what makes the suspicious-activity view and
the funnel dashboards trustworthy rather than merely indicative.
"""

from __future__ import annotations

import datetime as dt

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.db.base import utcnow
from app.models.activity import ActivityEventLog, LoginAttempt
from app.models.enums import ActivityEvent
from app.services.sessions import client_ip


def record_event(
    db: Session,
    event_code: str,
    *,
    request: Request | None = None,
    context: RequestContext | None = None,
    target_table: str | None = None,
    target_row_id: int | None = None,
    quantity: int | None = None,
    success: bool | None = None,
    detail: str | None = None,
) -> ActivityEventLog:
    user = context.user if context else None
    entry = ActivityEventLog(
        event_code=event_code,
        user_id=user.pk_user_id if user else None,
        username=user.username if user else None,
        session_key=context.session_key if context else None,
        impersonator_user_id=(
            context.impersonator.pk_user_id if context and context.impersonator else None
        ),
        ip_address=client_ip(request) if request else None,
        user_agent=(request.headers.get("user-agent", "")[:500] or None) if request else None,
        path=str(request.url.path)[:500] if request else None,
        referrer=(request.headers.get("referer", "")[:500] or None) if request else None,
        target_table=target_table,
        target_row_id=target_row_id,
        quantity=quantity,
        success_flag=success,
        detail=detail,
        created_dt=utcnow(),
        created_by=user.pk_user_id if user else None,
    )
    db.add(entry)
    return entry


def record_page_view(
    db: Session, request: Request, context: RequestContext | None
) -> ActivityEventLog:
    return record_event(db, ActivityEvent.PAGE_VIEW, request=request, context=context)


def record_login_attempt(
    db: Session,
    request: Request,
    identifier: str,
    *,
    success: bool,
    user_id: int | None = None,
    failure_reason: str | None = None,
    captcha_required: bool = False,
    lockout_triggered: bool = False,
) -> LoginAttempt:
    """Record the attempt whether it succeeded or not.

    Failures are the more valuable half: repeated attempts against one account
    is a pattern the admin dashboard needs to surface, and it only exists if
    every attempt is written (Part I §2.4, §2.8).
    """
    attempt = LoginAttempt(
        identifier=identifier[:255],
        user_id=user_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500] or None,
        success_flag=success,
        failure_reason=failure_reason,
        captcha_required_flag=captcha_required,
        lockout_triggered_flag=lockout_triggered,
        created_dt=utcnow(),
        created_by=user_id,
    )
    db.add(attempt)
    return attempt


def recent_failed_attempts(
    db: Session,
    *,
    identifier: str | None = None,
    ip_address: str | None = None,
    within_minutes: int = 15,
) -> int:
    """Count recent failures for an identifier or an IP.

    Drives the progressive CAPTCHA trigger and the lockout threshold. Counted in
    SQL — never by pulling rows back to count in Python (Part II §1).
    """
    since = utcnow() - dt.timedelta(minutes=within_minutes)
    stmt = select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.success_flag.is_(False),
        LoginAttempt.created_dt >= since,
    )
    if identifier:
        stmt = stmt.where(LoginAttempt.identifier == identifier)
    if ip_address:
        stmt = stmt.where(LoginAttempt.ip_address == ip_address)
    return db.scalar(stmt) or 0
