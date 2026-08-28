"""Server-side sessions (Part I §2.3, §2.8; Part II §7.4).

Server-held sessions rather than JWTs, because two required features are
awkward-to-impossible with stateless tokens: an admin force-terminating one
specific session, and the "currently logged in" dashboard. Both are trivial when
the server owns the record.

The session row is also where impersonation lives: an impersonated session
carries the *target* user in ``fk_user_id`` — so it inherits the target's
permissions, exactly as Part I §2.2.2 requires — with the real actor recorded
alongside in ``impersonator_user_id``.
"""

from __future__ import annotations

import datetime as dt

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import generate_token
from app.db.base import utcnow
from app.models.activity import UserSession
from app.models.enums import SessionEndReason
from app.models.identity import User

log = get_logger(__name__)


def _timeout_minutes(user: User) -> int:
    """Global default, overridable per role — never hardcoded (Part I §2.3)."""
    if user.role and user.role.session_timeout_minutes:
        return user.role.session_timeout_minutes
    return settings.session_idle_timeout_minutes


def create_session(
    db: Session,
    user: User,
    request: Request,
    *,
    impersonator_user_id: int | None = None,
    parent_session_key: str | None = None,
) -> UserSession:
    now = utcnow()
    session = UserSession(
        session_key=generate_token(),
        fk_user_id=user.pk_user_id,
        started_dt=now,
        last_seen_dt=now,
        expires_dt=now + dt.timedelta(minutes=_timeout_minutes(user)),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500] or None,
        device_label=_device_label(request.headers.get("user-agent", "")),
        impersonator_user_id=impersonator_user_id,
        parent_session_key=parent_session_key,
        scd_active_from=now,
    )
    db.add(session)
    db.flush()
    return session


def resolve_session(
    db: Session, request: Request
) -> tuple[User | None, User | None, str | None]:
    """Resolve the cookie to ``(acting_user, impersonator, session_key)``.

    Expiry is enforced here on every request, so an idle session dies on its
    next use rather than waiting for a sweep job. Cart contents are untouched —
    a timed-out shopper is not punished for stepping away (Part I §2.3).
    """
    session_key = request.cookies.get(settings.session_cookie_name)
    if not session_key:
        return None, None, None

    session = db.scalars(
        select(UserSession).where(
            UserSession.session_key == session_key,
            UserSession.scd_active_flag.is_(True),
        )
    ).first()
    if session is None:
        return None, None, None

    now = utcnow()
    if session.is_expired(now):
        end_session(db, session, SessionEndReason.IDLE_TIMEOUT)
        db.commit()
        return None, None, None

    user = db.scalars(
        select(User)
        .where(User.pk_user_id == session.fk_user_id, User.scd_active_flag.is_(True))
        .options(selectinload(User.role))
    ).first()
    if user is None or not user.is_active_flag:
        end_session(db, session, SessionEndReason.FORCED_BY_ADMIN)
        db.commit()
        return None, None, None

    # Sliding expiry, written at most once a minute: on a browsing session this
    # would otherwise be a write on every single page view.
    if (now - _aware_utc(session.last_seen_dt)).total_seconds() > 60:
        session.last_seen_dt = now
        session.expires_dt = now + dt.timedelta(minutes=_timeout_minutes(user))
        db.commit()

    impersonator: User | None = None
    if session.impersonator_user_id:
        impersonator = db.get(User, session.impersonator_user_id)

    return user, impersonator, session.session_key


def end_session(db: Session, session: UserSession, reason: str) -> None:
    """Close the row — ``scd_active_to`` is when it ended (Part II §1)."""
    session.end_reason = reason
    session.close()


def end_all_sessions_for_user(
    db: Session, user_id: int, reason: str, *, except_key: str | None = None
) -> int:
    """Terminate every live session for one account.

    Used by "force logout on password change" (Part I §2.3) and by an admin
    terminating a lost device. Returns how many were closed.
    """
    sessions = db.scalars(
        select(UserSession).where(
            UserSession.fk_user_id == user_id,
            UserSession.scd_active_flag.is_(True),
        )
    ).all()
    closed = 0
    for session in sessions:
        if except_key and session.session_key == except_key:
            continue
        end_session(db, session, reason)
        closed += 1
    if closed:
        log.info("sessions_terminated", extra={"user_id": user_id, "count": closed, "reason": reason})
    return closed


def set_session_cookie(response: Response, session: UserSession) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session.session_key,
        max_age=int((_aware_utc(session.expires_dt) - utcnow()).total_seconds()),
        httponly=True,          # never readable from JavaScript
        samesite="lax",         # survives a WhatsApp link, blocks cross-site POSTs
        secure=settings.session_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


def client_ip(request: Request) -> str | None:
    """The caller's IP, honouring one proxy hop.

    Captured on every logged event, not just login (Part I §2.8).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


def _device_label(user_agent: str) -> str | None:
    """A short, human-readable device summary for the customer's own sessions
    view — enough to recognise "my phone", not a fingerprint."""
    if not user_agent:
        return None
    ua = user_agent.lower()
    platform = next(
        (name for token, name in (
            ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
            ("windows", "Windows"), ("mac os", "macOS"), ("linux", "Linux"),
        ) if token in ua),
        "Unknown device",
    )
    browser = next(
        (name for token, name in (
            ("edg/", "Edge"), ("chrome", "Chrome"), ("safari", "Safari"),
            ("firefox", "Firefox"),
        ) if token in ua),
        None,
    )
    return f"{platform} · {browser}" if browser else platform


def _aware_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value
