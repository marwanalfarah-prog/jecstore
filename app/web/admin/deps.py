"""Admin-side dependencies: the staff gate and the permission gate.

Two distinct checks, deliberately separate:

* :func:`require_staff` — may this person see the admin panel *at all*? Answered
  by the role's ``is_staff_flag``. Customers never reach here.
* :func:`require_permission` — may they perform *this* action, and does it need
  a second pair of eyes? Answered per ``(module, action)`` by
  ``app/services/permissions.py``, which also returns the approval mode.

Keeping them apart is what lets an Employee hold order-prep access without
money-box access (Part I §2.2): they pass the staff gate once, then each action
is judged on its own.

An impersonated session inherits the **target's** permissions, not the
impersonator's (Part I §2.2.2). That falls out for free, because the context's
``user`` is already the target — the impersonator is recorded alongside it purely
for the banner and the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.context import RequestContext, get_context
from app.core.errors import NotAuthenticated, PermissionDenied
from app.db.session import get_db
from app.models.identity import User
from app.services.permissions import GrantDecision, resolve_grant


class AdminRedirect(Exception):
    """Raised to bounce an anonymous visitor to the login page.

    Not an :class:`AppError`: an unauthenticated *browser* wants a redirect, not
    a 401 error page. Handled in ``app/main.py``.
    """

    def __init__(self, location: str) -> None:
        self.location = location
        super().__init__(location)


def current_staff(request: Request) -> User:
    """The signed-in staff member, or bounce/refuse.

    Anonymous → redirect to login carrying ``?next=``.
    Signed in but not staff → 403. Deliberately *not* a redirect: a customer who
    guessed an admin URL should be told no, not sent somewhere that implies
    signing in again would help.
    """
    ctx: RequestContext = get_context(request)

    if ctx.user is None:
        raise AdminRedirect(f"/auth/login?next={request.url.path}")

    role = ctx.user.role
    if role is None or not role.is_staff_flag:
        raise PermissionDenied("This area is for store staff.")

    if not ctx.user.is_active_flag:
        raise NotAuthenticated("This account is no longer active.")

    return ctx.user


def require_permission(module: str, action: str) -> Callable[..., GrantDecision]:
    """Dependency factory guarding one ``module.action``.

    Returns the :class:`GrantDecision`, so the route knows whether to execute
    immediately or park the action for approval::

        @router.post("/orders/{id}/discount")
        def apply_discount(
            decision: GrantDecision = Depends(
                require_permission("orders", "apply_invoice_discount")
            ),
        ):
            if decision.needs_approval:
                ...
    """

    def dependency(
        request: Request,
        staff: User = Depends(current_staff),
        db: Session = Depends(get_db),
    ) -> GrantDecision:
        decision = resolve_grant(db, staff, module, action)
        if not decision.allowed:
            raise PermissionDenied(
                f"You do not have permission to {action.replace('_', ' ')}."
            )
        return decision

    return dependency


def has_permission(db: Session, user: User | None, module: str, action: str) -> bool:
    """Non-raising check, for deciding whether to *render* a nav item or button.

    Hiding a control the user cannot use is a courtesy, not a security boundary
    — the route still enforces it. Never rely on this alone.
    """
    if user is None:
        return False
    return resolve_grant(db, user, module, action).allowed


def permission_map(db: Session, user: User | None, codes: list[str]) -> dict[str, bool]:
    """Resolve several permissions at once, for a nav render.

    Batched because the admin sidebar asks about a dozen modules on every page
    load, and one query per item is the N+1 Part II §2 rules out.
    """
    result: dict[str, bool] = {}
    for code in codes:
        module, _, action = code.partition(".")
        result[code] = has_permission(db, user, module, action)
    return result
