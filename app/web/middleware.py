"""Request middleware: request id, session resolution, context, activity log.

Order matters and is set in ``app/main.py``. Each layer here does one thing:

1. :class:`RequestIdMiddleware` stamps an id so every log line and error
   envelope for one request can be tied together.
2. :class:`RequestContextMiddleware` resolves the session, the acting user,
   language, currency and the live rate, then hangs them on ``request.state``.
3. :class:`ActivityLogMiddleware` writes the page view (Part I §2.8).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.context import (
    CURRENCY_COOKIE,
    GUEST_SESSION_COOKIE,
    LANGUAGE_COOKIE,
    PREFERENCE_COOKIE_MAX_AGE,
    RequestContext,
    resolve_currency,
    resolve_language,
)
from app.core.logging import bind_request_id, get_logger, new_request_id
from app.db.session import SessionLocal

log = get_logger(__name__)

Next = Callable[[Request], Awaitable[Response]]

#: Paths that never need a database session or an activity log entry.
_SKIP_PREFIXES = ("/static", "/media", "/favicon.ico", "/healthz")


def _is_skipped(path: str) -> bool:
    return path.startswith(_SKIP_PREFIXES)


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Next) -> Response:
        request_id = request.headers.get("x-request-id") or new_request_id()
        bind_request_id(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        response.headers["x-request-id"] = request_id
        if not _is_skipped(request.url.path):
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(elapsed_ms, 1),
                },
            )
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Build the per-request presentation context.

    Deliberately resilient: if the database is unreachable, the context falls
    back to configured defaults so an error page can still render. A failure to
    load the mega-menu must not turn a 404 into a 500.
    """

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        if _is_skipped(request.url.path):
            return await call_next(request)

        from app.services.chrome import load_chrome
        from app.services.pricing import current_usd_rate
        from app.services.sessions import resolve_session

        db = SessionLocal()
        try:
            user, impersonator, auth_session_key = resolve_session(db, request)
            session_key = (
                auth_session_key
                if user is not None
                else request.cookies.get(GUEST_SESSION_COOKIE)
            )
            context = RequestContext(
                language=resolve_language(request, user),
                currency=resolve_currency(request, user),
                usd_rate=current_usd_rate(db),
                user=user,
                impersonator=impersonator,
                session_key=session_key,
            )
            load_chrome(db, context)
            request.state.context = context
            request.state.db = db
            response = await call_next(request)
        finally:
            db.close()

        # Persist an explicit language/currency choice so it survives the next
        # navigation without the query parameter trailing along.
        if "lang" in request.query_params:
            response.set_cookie(
                LANGUAGE_COOKIE,
                context.language,
                max_age=PREFERENCE_COOKIE_MAX_AGE,
                httponly=False,
                samesite="lax",
                secure=settings.session_cookie_secure,
            )
        if "currency" in request.query_params:
            response.set_cookie(
                CURRENCY_COOKIE,
                context.currency,
                max_age=PREFERENCE_COOKIE_MAX_AGE,
                httponly=False,
                samesite="lax",
                secure=settings.session_cookie_secure,
            )
        return response


class ActivityLogMiddleware(BaseHTTPMiddleware):
    """Page views, into the insert-only activity log (Part I §2.8).

    Only successful GETs of HTML are recorded — logging a redirect or an asset
    fetch as a "page view" would make the funnel dashboards lie. Failures here
    are swallowed on purpose: analytics must never break a page.
    """

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        response = await call_next(request)

        if (
            request.method != "GET"
            or _is_skipped(request.url.path)
            or response.status_code >= 300
            or "text/html" not in response.headers.get("content-type", "")
        ):
            return response

        try:
            from app.services.activity import record_page_view

            context = getattr(request.state, "context", None)
            with SessionLocal() as db:
                record_page_view(db, request, context)
                db.commit()
        except Exception:  # noqa: BLE001 - analytics is never load-bearing
            log.exception("activity_log_failed", extra={"path": request.url.path})

        return response
