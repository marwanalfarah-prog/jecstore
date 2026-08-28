"""One error shape for the whole API, and never fail silently (Part II 5).

Every error the application raises is an `AppError`. The API surface renders it
as JSON; the storefront/admin HTML surface renders it through an error template.
Both carry the same machine-readable `code`, so the front end can branch on the
code rather than parsing prose.

Error envelope::

    {"error": {"code": "out_of_stock",
               "message": "...",            # already localised
               "details": {...},            # optional, machine-readable
               "request_id": "a1b2c3..."}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import current_request_id, get_logger

log = get_logger(__name__)


class AppError(Exception):
    """Base class for every expected failure in the system."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"
    message: str = "Something went wrong."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        if request_id := current_request_id():
            error["request_id"] = request_id
        return {"error": error}


# --- Generic ---------------------------------------------------------------


class ValidationFailed(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_failed"
    message = "Some of the submitted values are not valid."


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested item does not exist."


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "That action conflicts with the current state."


class RateLimited(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many attempts. Please wait and try again."


# --- Identity & access (Part I 2) -----------------------------------------


class NotAuthenticated(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "not_authenticated"
    message = "Please sign in to continue."


class PermissionDenied(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class EmailNotVerified(AppError):
    """Unverified accounts persist indefinitely but cannot check out (Part I 2.5)."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "email_not_verified"
    message = "Please verify your email address before completing an order."


class LastAdminLockout(AppError):
    """Blocked outright — never a warning-and-proceed (Part I 2.2)."""

    status_code = status.HTTP_409_CONFLICT
    code = "last_admin_lockout"
    message = "This would leave the system without an administrator."


class ApprovalRequired(AppError):
    """Not a failure: the action was parked in the maker-checker queue (Part I 2.2.1)."""

    status_code = status.HTTP_202_ACCEPTED
    code = "approval_required"
    message = "Your request was submitted and is awaiting approval."


# --- Commerce (Part I 8, 11, 12) ------------------------------------------


class OutOfStock(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "out_of_stock"
    message = "The requested quantity is no longer available."


class StockLocked(AppError):
    """Another checkout holds the reservation lock (Part I 8)."""

    status_code = status.HTTP_409_CONFLICT
    code = "stock_locked"
    message = "This item is being checked out by someone else. Please retry."


class CategoryNotEmpty(AppError):
    """Deletion is blocked until products are reassigned (Part I 5.1)."""

    status_code = status.HTTP_409_CONFLICT
    code = "category_not_empty"
    message = "Reassign every product in this category before deleting it."


class PromocodeInvalid(AppError):
    code = "promocode_invalid"
    message = "This promocode cannot be applied to your order."


# --- Handler registration --------------------------------------------------


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/v1/") or request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _html_error(request: Request, error: AppError) -> Response:
    # Imported lazily: templating imports i18n, which must not import errors at module load.
    from app.core.templating import templates

    return templates.TemplateResponse(
        request,
        "errors/error.html",
        {"error": error.to_envelope()["error"], "status_code": error.status_code},
        status_code=error.status_code,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> Response:
        log.warning(
            "app_error", extra={"code": exc.code, "path": request.url.path, "status": exc.status_code}
        )
        if _wants_json(request):
            return JSONResponse(exc.to_envelope(), status_code=exc.status_code)
        return _html_error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> Response:
        error = ValidationFailed(details={"fields": _field_errors(exc)})
        if _wants_json(request):
            return JSONResponse(error.to_envelope(), status_code=error.status_code)
        return _html_error(request, error)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        error = AppError(
            str(exc.detail),
            code=_CODE_BY_STATUS.get(exc.status_code, "http_error"),
            status_code=exc.status_code,
        )
        if _wants_json(request):
            return JSONResponse(error.to_envelope(), status_code=error.status_code)
        return _html_error(request, error)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        # Log the full trace, but never leak internals to the caller.
        log.exception("unhandled_error", extra={"path": request.url.path})
        error = AppError(
            "An unexpected error occurred. Our team has been notified.",
            code="internal_error",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        if _wants_json(request):
            return JSONResponse(error.to_envelope(), status_code=error.status_code)
        return _html_error(request, error)


_CODE_BY_STATUS = {
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "rate_limited",
}


def _field_errors(exc: RequestValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"][1:]) or "_"
        fields.setdefault(location, err["msg"])
    return fields
