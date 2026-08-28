"""The maker-checker runtime (Part I §2.2.1).

The models have existed since the schema was laid down; this is the engine that
uses them. The central idea is that **a pending action is a row, not a
callback**. When an action needs a second pair of eyes, we do not hold a closure
in memory and hope the process survives — we record *what was asked for*, and
replay it from those rows when a checker approves. That is what makes the queue
survive a restart, and what makes it auditable afterwards.

Flow::

    decision = require(db, user, "orders", "apply_invoice_discount")
    result = execute_or_submit(
        db, request, decision, user,
        module="orders", action="apply_invoice_discount",
        params={"order_id": 12, "amount": Decimal("5.000")},
        summary_en="5.000 JOD off order JEC-260812-001",
        summary_ar="خصم ٥ دنانير على الطلب",
    )
    if result.pending:
        ...  # tell the maker it is awaiting approval

Rules enforced here, all from §2.2.1:

* A maker can **never** approve their own request, even if they hold a role that
  would otherwise qualify them as a checker.
* Any one eligible checker resolves it, unless the grant sets
  ``required_approvals`` higher.
* Decisions are immutable history. If a checker's access is later revoked or
  their account deactivated, their past decisions stand.
* Every event — created, approved, rejected, executed, failed — is written to
  the audit log.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, Conflict, NotFound, PermissionDenied
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.access import (
    ApprovalChecker,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalRequestParameter,
    Permission,
    PermissionGrant,
)
from app.models.enums import ApprovalStatus, GrantScope
from app.models.identity import User
from app.services.audit import record_audit
from app.services.permissions import GrantDecision

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

#: ``module.action`` → the function that performs it.
#:
#: The signature is ``(db, params, actor_user_id) -> Any``. Registering here is
#: what makes an action replayable after approval; an action that is only ever
#: Single Approval still benefits, because it keeps the "how do I do this" in
#: one place rather than inline in a route.
ActionHandler = Callable[[Session, dict[str, Any], int | None], Any]

_REGISTRY: dict[str, ActionHandler] = {}


def register(module: str, action: str) -> Callable[[ActionHandler], ActionHandler]:
    """Decorator registering the handler for one ``module.action``."""

    def wrap(handler: ActionHandler) -> ActionHandler:
        code = f"{module}.{action}"
        if code in _REGISTRY:
            raise RuntimeError(f"Action {code} is already registered.")
        _REGISTRY[code] = handler
        return handler

    return wrap


def registered_actions() -> dict[str, ActionHandler]:
    return dict(_REGISTRY)


def _handler_for(module: str, action: str) -> ActionHandler:
    code = f"{module}.{action}"
    handler = _REGISTRY.get(code)
    if handler is None:
        raise RuntimeError(
            f"No handler registered for '{code}'. A Maker-Checker action must be "
            f"registered with @approvals.register so it can be replayed after "
            f"approval."
        )
    return handler


# ---------------------------------------------------------------------------
# Parameter serialisation
# ---------------------------------------------------------------------------
#
# Parameters are stored as typed key/value *rows*, not a JSON blob: Part II §1
# rules out JSON for anything representable as rows, and it means the queue can
# be filtered on an amount without parsing a payload.


def _encode(value: Any) -> tuple[str | None, str]:
    if value is None:
        return None, "null"
    if isinstance(value, bool):
        return ("1" if value else "0"), "bool"
    if isinstance(value, int):
        return str(value), "int"
    if isinstance(value, Decimal):
        return str(value), "decimal"
    if isinstance(value, dt.datetime):
        return value.isoformat(), "datetime"
    if isinstance(value, dt.date):
        return value.isoformat(), "date"
    return str(value), "str"


def _decode(raw: str | None, param_type: str) -> Any:
    if param_type == "null" or raw is None:
        return None
    match param_type:
        case "bool":
            return raw == "1"
        case "int":
            return int(raw)
        case "decimal":
            return Decimal(raw)
        case "datetime":
            return dt.datetime.fromisoformat(raw)
        case "date":
            return dt.date.fromisoformat(raw)
        case _:
            return raw


def request_params(db: Session, request_row: ApprovalRequest) -> dict[str, Any]:
    """Rehydrate a pending action's arguments."""
    rows = db.scalars(
        select(ApprovalRequestParameter).where(
            ApprovalRequestParameter.fk_approval_request_id
            == request_row.pk_approval_request_id
        )
    ).all()
    return {row.param_name: _decode(row.param_value, row.param_type) for row in rows}


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ActionResult:
    """What happened: executed now, or parked for approval."""

    pending: bool
    approval_request: ApprovalRequest | None = None
    value: Any = None

    @property
    def executed(self) -> bool:
        return not self.pending


def execute_or_submit(
    db: Session,
    request: Request | None,
    decision: GrantDecision,
    actor: User,
    *,
    module: str,
    action: str,
    params: dict[str, Any],
    summary_en: str | None = None,
    summary_ar: str | None = None,
    target_table: str | None = None,
    target_row_id: int | None = None,
) -> ActionResult:
    """Run the action now, or park it — whichever the grant says.

    This is the single entry point every guarded admin write should use. Routes
    should not branch on the approval mode themselves; doing it here is what
    guarantees a Maker-Checker action can never accidentally execute because one
    route forgot to check.
    """
    handler = _handler_for(module, action)

    if not decision.needs_approval:
        value = handler(db, params, actor.pk_user_id)
        record_audit(
            db,
            request,
            actor=actor,
            module=module,
            action=action,
            target_table=target_table,
            target_row_id=target_row_id,
            note=summary_en,
        )
        return ActionResult(pending=False, value=value)

    approval = _submit(
        db,
        request,
        decision,
        actor,
        module=module,
        action=action,
        params=params,
        summary_en=summary_en,
        summary_ar=summary_ar,
        target_table=target_table,
        target_row_id=target_row_id,
    )
    return ActionResult(pending=True, approval_request=approval)


def _submit(
    db: Session,
    request: Request | None,
    decision: GrantDecision,
    actor: User,
    *,
    module: str,
    action: str,
    params: dict[str, Any],
    summary_en: str | None,
    summary_ar: str | None,
    target_table: str | None,
    target_row_id: int | None,
) -> ApprovalRequest:
    permission = db.scalars(
        select(Permission).where(
            Permission.module_code == module,
            Permission.action_code == action,
            Permission.scd_active_flag.is_(True),
        )
    ).first()
    if permission is None:
        raise NotFound(f"Unknown permission {module}.{action}.")

    now = utcnow()
    approval = ApprovalRequest(
        fk_permission_id=permission.pk_permission_id,
        fk_permission_grant_id=decision.grant_id,
        fk_maker_user_id=actor.pk_user_id,
        status=ApprovalStatus.PENDING,
        required_approvals=max(1, decision.required_approvals),
        target_table=target_table,
        target_row_id=target_row_id,
        summary_en=summary_en,
        summary_ar=summary_ar,
        requested_dt=now,
        scd_active_from=now,
    )
    db.add(approval)
    db.flush()

    for name, value in params.items():
        raw, param_type = _encode(value)
        db.add(
            ApprovalRequestParameter(
                fk_approval_request_id=approval.pk_approval_request_id,
                param_name=name,
                param_value=raw,
                param_type=param_type,
                created_dt=now,
                created_by=actor.pk_user_id,
            )
        )

    record_audit(
        db,
        request,
        actor=actor,
        module=module,
        action=f"{action}:requested",
        target_table="scd_approval_request",
        target_row_id=approval.pk_approval_request_id,
        approval_request_id=approval.pk_approval_request_id,
        note=summary_en,
    )
    log.info(
        "approval_requested",
        extra={
            # Not "module": that is a reserved LogRecord attribute and passing
            # it raises. The sanitiser in app/core/logging.py would rename it,
            # but naming it correctly here is clearer than relying on that.
            "permission_module": module,
            "permission_action": action,
            "maker": actor.pk_user_id,
            "request_id": approval.pk_approval_request_id,
        },
    )
    return approval


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def is_eligible_checker(db: Session, approval: ApprovalRequest, user: User) -> bool:
    """May ``user`` decide this request?

    The maker is excluded first and unconditionally — §2.2.1 says a maker can
    never approve their own action *even if they also hold a qualifying role*,
    so this check must come before any role matching, not after.
    """
    if approval.fk_maker_user_id == user.pk_user_id:
        return False

    checkers = db.scalars(
        select(ApprovalChecker).where(
            ApprovalChecker.fk_permission_grant_id == approval.fk_permission_grant_id,
            ApprovalChecker.scd_active_flag.is_(True),
        )
    ).all()

    if not checkers:
        # No explicit checkers configured: fall back to anyone else holding the
        # same permission. Without this a Maker-Checker grant with no checkers
        # would deadlock, which is worse than a slightly wide fallback.
        return _holds_permission(db, user, approval.fk_permission_id)

    for checker in checkers:
        if checker.checker_scope == GrantScope.USER and checker.fk_user_id == user.pk_user_id:
            return True
        if checker.checker_scope == GrantScope.ROLE and checker.fk_role_id == user.fk_role_id:
            return True
    return False


def _holds_permission(db: Session, user: User, permission_id: int) -> bool:
    grant = db.scalars(
        select(PermissionGrant).where(
            PermissionGrant.fk_permission_id == permission_id,
            PermissionGrant.scd_active_flag.is_(True),
            PermissionGrant.granted_flag.is_(True),
            (PermissionGrant.fk_user_id == user.pk_user_id)
            | (PermissionGrant.fk_role_id == user.fk_role_id),
        )
    ).first()
    return grant is not None


def pending_queue(db: Session, user: User) -> list[ApprovalRequest]:
    """Everything ``user`` is eligible to decide, oldest first.

    Oldest first on purpose: an approval queue is a work queue, and the thing
    that has been waiting longest is the thing blocking somebody.
    """
    pending = db.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.status == ApprovalStatus.PENDING,
            ApprovalRequest.scd_active_flag.is_(True),
        )
        .order_by(ApprovalRequest.requested_dt)
    ).all()
    return [a for a in pending if is_eligible_checker(db, a, user)]


def my_requests(db: Session, user: User, *, limit: int = 50) -> list[ApprovalRequest]:
    """What this person has submitted, so a maker can see their own outcomes."""
    return list(
        db.scalars(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.fk_maker_user_id == user.pk_user_id,
                ApprovalRequest.scd_active_flag.is_(True),
            )
            .order_by(ApprovalRequest.requested_dt.desc())
            .limit(limit)
        ).all()
    )


def approve(
    db: Session,
    request: Request | None,
    approval: ApprovalRequest,
    checker: User,
    *,
    note: str | None = None,
) -> ActionResult:
    """Record an approval and, once enough have landed, replay the action."""
    _assert_decidable(db, approval, checker)

    now = utcnow()
    db.add(
        ApprovalDecision(
            fk_approval_request_id=approval.pk_approval_request_id,
            decided_by_user_id=checker.pk_user_id,
            decision=ApprovalStatus.APPROVED,
            note=note,
            created_dt=now,
            created_by=checker.pk_user_id,
        )
    )
    db.flush()

    approvals_so_far = _approval_count(db, approval)
    permission = db.get(Permission, approval.fk_permission_id)

    record_audit(
        db,
        request,
        actor=checker,
        module=permission.module_code,
        action=f"{permission.action_code}:approved",
        target_table="scd_approval_request",
        target_row_id=approval.pk_approval_request_id,
        approval_request_id=approval.pk_approval_request_id,
        note=note,
    )

    if approvals_so_far < approval.required_approvals:
        log.info(
            "approval_partial",
            extra={
                "request_id": approval.pk_approval_request_id,
                "have": approvals_so_far,
                "need": approval.required_approvals,
            },
        )
        return ActionResult(pending=True, approval_request=approval)

    # Enough approvals — replay the action from its stored parameters.
    approval.status = ApprovalStatus.APPROVED
    approval.resolved_dt = now

    handler = _handler_for(permission.module_code, permission.action_code)
    params = request_params(db, approval)

    try:
        value = handler(db, params, approval.fk_maker_user_id)
    except AppError as exc:
        # The action was legitimately approved but could not be carried out —
        # stock ran out, the order was already cancelled. Record why on the row
        # rather than failing silently or pretending it succeeded (Part II §5).
        approval.execution_error = f"{exc.code}: {exc.message}"
        log.warning(
            "approval_execution_failed",
            extra={"request_id": approval.pk_approval_request_id, "code": exc.code},
        )
        record_audit(
            db,
            request,
            actor=checker,
            module=permission.module_code,
            action=f"{permission.action_code}:execution_failed",
            target_table="scd_approval_request",
            target_row_id=approval.pk_approval_request_id,
            approval_request_id=approval.pk_approval_request_id,
            note=approval.execution_error,
        )
        return ActionResult(pending=False, approval_request=approval, value=None)

    approval.executed_dt = now
    record_audit(
        db,
        request,
        actor=checker,
        module=permission.module_code,
        action=permission.action_code,
        target_table=approval.target_table,
        target_row_id=approval.target_row_id,
        approval_request_id=approval.pk_approval_request_id,
        note=approval.summary_en,
    )
    log.info(
        "approval_executed",
        extra={"request_id": approval.pk_approval_request_id, "checker": checker.pk_user_id},
    )
    return ActionResult(pending=False, approval_request=approval, value=value)


def reject(
    db: Session,
    request: Request | None,
    approval: ApprovalRequest,
    checker: User,
    *,
    note: str | None = None,
) -> ApprovalRequest:
    """Discard the request. The maker is notified of the outcome either way."""
    _assert_decidable(db, approval, checker)

    now = utcnow()
    db.add(
        ApprovalDecision(
            fk_approval_request_id=approval.pk_approval_request_id,
            decided_by_user_id=checker.pk_user_id,
            decision=ApprovalStatus.REJECTED,
            note=note,
            created_dt=now,
            created_by=checker.pk_user_id,
        )
    )
    approval.status = ApprovalStatus.REJECTED
    approval.resolved_dt = now

    permission = db.get(Permission, approval.fk_permission_id)
    record_audit(
        db,
        request,
        actor=checker,
        module=permission.module_code,
        action=f"{permission.action_code}:rejected",
        target_table="scd_approval_request",
        target_row_id=approval.pk_approval_request_id,
        approval_request_id=approval.pk_approval_request_id,
        note=note,
    )
    log.info(
        "approval_rejected",
        extra={"request_id": approval.pk_approval_request_id, "checker": checker.pk_user_id},
    )
    return approval


def cancel(
    db: Session, request: Request | None, approval: ApprovalRequest, maker: User
) -> ApprovalRequest:
    """A maker withdrawing their own pending request."""
    if approval.fk_maker_user_id != maker.pk_user_id:
        raise PermissionDenied("Only the person who raised a request can withdraw it.")
    if approval.status != ApprovalStatus.PENDING:
        raise Conflict("That request has already been decided.")

    approval.status = ApprovalStatus.CANCELLED
    approval.resolved_dt = utcnow()

    permission = db.get(Permission, approval.fk_permission_id)
    record_audit(
        db,
        request,
        actor=maker,
        module=permission.module_code,
        action=f"{permission.action_code}:cancelled",
        target_table="scd_approval_request",
        target_row_id=approval.pk_approval_request_id,
        approval_request_id=approval.pk_approval_request_id,
    )
    return approval


def _assert_decidable(db: Session, approval: ApprovalRequest, checker: User) -> None:
    if approval.status != ApprovalStatus.PENDING:
        raise Conflict("That request has already been decided.")
    if approval.fk_maker_user_id == checker.pk_user_id:
        raise PermissionDenied("You cannot approve your own request.")
    if not is_eligible_checker(db, approval, checker):
        raise PermissionDenied("You are not an eligible checker for this request.")
    if _has_already_decided(db, approval, checker):
        raise Conflict("You have already decided on this request.")


def _has_already_decided(db: Session, approval: ApprovalRequest, checker: User) -> bool:
    """One person cannot satisfy a two-approval requirement twice."""
    existing = db.scalars(
        select(ApprovalDecision).where(
            ApprovalDecision.fk_approval_request_id == approval.pk_approval_request_id,
            ApprovalDecision.decided_by_user_id == checker.pk_user_id,
        )
    ).first()
    return existing is not None


def _approval_count(db: Session, approval: ApprovalRequest) -> int:
    decisions = db.scalars(
        select(ApprovalDecision).where(
            ApprovalDecision.fk_approval_request_id == approval.pk_approval_request_id,
            ApprovalDecision.decision == ApprovalStatus.APPROVED,
        )
    ).all()
    return len(decisions)
