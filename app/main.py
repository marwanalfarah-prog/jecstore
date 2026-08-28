"""Application entry point.

Middleware order is load-bearing and reads bottom-up in Starlette: the last one
added is the outermost. So the request id wraps everything (every log line and
error envelope needs it), the context sits inside it, and activity logging is
innermost, where it can see the finished response.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.web.admin.deps import AdminRedirect
from app.web.middleware import (
    ActivityLogMiddleware,
    RequestContextMiddleware,
    RequestIdMiddleware,
)

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "startup",
        extra={
            "env": settings.app_env,
            "database": "sqlite" if settings.is_sqlite else "postgresql",
            "default_language": settings.default_language,
        },
    )
    settings.media_root.mkdir(parents=True, exist_ok=True)
    yield
    log.info("shutdown")


app = FastAPI(
    title="شبيبة ستور — JEC Store",
    description="The Complete Catholic Bookstore. Storefront, admin panel and API.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if not settings.is_production else None,
)

register_exception_handlers(app)


@app.exception_handler(AdminRedirect)
async def _admin_redirect(request, exc: AdminRedirect):
    """Bounce an anonymous visitor to login, carrying where they were going."""
    return RedirectResponse(exc.location, status_code=303)

app.add_middleware(ActivityLogMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(RequestIdMiddleware)

app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
app.mount("/media", StaticFiles(directory=str(settings.media_root)), name="media")


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    """Liveness probe. Deliberately does not touch the database — this answers
    "is the process up", which is a different question from "is the database
    reachable", and conflating them makes restarts thrash."""
    return JSONResponse({"status": "ok", "version": app.version})


def _register_routers() -> None:
    """Routers are registered here, in order.

    The legacy catch-all redirect must come last: a router matching
    ``/{path:path}`` registered earlier would swallow every route after it.
    """
    from app.web import (
        account,
        auth,
        checkout,
        commerce,
        content,
        customer_returns,
        invoices,
        seo,
        storefront,
    )
    from app.web.admin import build_router as build_admin_router

    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(customer_returns.router)
    app.include_router(invoices.account_router)
    app.include_router(commerce.router)
    app.include_router(checkout.router)
    app.include_router(build_admin_router())
    app.include_router(content.router)
    app.include_router(seo.router)
    app.include_router(storefront.router)
    app.include_router(storefront.legacy_router)  # keep last


_register_routers()
