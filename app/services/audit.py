"""Writing the audit log (Part I §2.2).

Who changed what, when — for price changes, discounts applied, permission
changes, stock adjustments, every maker-checker event, and every action taken
during an impersonated session.

Two deliberate choices carry the module:

* **The actor is denormalised.** ``actor_username`` is copied onto the row
  rather than joined at read time. Part I §2.2 requires that a deleted or
  deactivated staff account leaves audit entries intact as *immutable
  historical records*, not live foreign keys that break or cascade. Storing the
  name as it was at the time is what makes that true.
* **Field changes are rows, not a diff blob.** ``TRX_AUDIT_LOG_FIELD`` holds
  one row per changed column, so "show every price change over 20%" is a query
  rather than a scan-and-parse (Part II §1).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.context import get_context
from app.db.base import utcnow
from app.models.access import AuditLog, AuditLogField
from app.models.identity import User


def record_audit(
    db: Session,
    request: Request | None,
    *,
    actor: User | None,
    module: str,
    action: str,
    target_table: str | None = None,
    target_row_id: int | None = None,
    approval_request_id: int | None = None,
    note: str | None = None,
    changes: dict[str, tuple[Any, Any]] | None = None,
) -> AuditLog:
    """Append one audit entry.

    ``changes`` maps ``field_name -> (old, new)``. Unchanged fields should not
    be passed — an audit entry listing twenty fields where one moved is an
    entry nobody reads.
    """
    context = get_context(request) if request is not None else None

    entry = AuditLog(
        actor_user_id=actor.pk_user_id if actor else None,
        actor_username=actor.username if actor else None,
        # Set only during impersonation, so reporting never conflates an action
        # taken on someone's behalf with their own (Part I §2.2.2).
        impersonator_user_id=(
            context.impersonator.pk_user_id
            if context and context.impersonator
            else None
        ),
        module_code=module,
        action_code=action,
        target_table=target_table,
        target_row_id=target_row_id,
        fk_approval_request_id=approval_request_id,
        ip_address=_client_ip(request),
        session_key=context.session_key if context else None,
        note=note,
        created_dt=utcnow(),
        created_by=actor.pk_user_id if actor else None,
    )
    db.add(entry)
    db.flush()

    for field_name, (old, new) in (changes or {}).items():
        db.add(
            AuditLogField(
                fk_audit_log_id=entry.pk_audit_log_id,
                field_name=field_name,
                old_value=None if old is None else str(old),
                new_value=None if new is None else str(new),
                created_dt=entry.created_dt,
                created_by=entry.created_by,
            )
        )

    return entry


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Changed fields only, as ``{name: (old, new)}``.

    Compares stringified values, so ``Decimal("5.000")`` and ``"5.000"`` do not
    read as a change when nothing actually moved.
    """
    changes: dict[str, tuple[Any, Any]] = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if str(old_value) != str(new_value):
            changes[key] = (old_value, new_value)
    return changes


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None
