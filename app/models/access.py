"""Access management: permissions, maker-checker, impersonation, audit log.

Implements Part I §2.2, §2.2.1 and §2.2.2. Three ideas carry the whole module:

1. A **permission** is a ``(module, action)`` pair, not a page. That is what
   lets an Employee hold order-prep access without money-box access.
2. A **grant** attaches a permission to a *role* or a *single username*, and
   carries its own approval mode. The same action can therefore be Single
   Approval for one role and Maker-Checker for another.
3. A pending action is a **row, not a callback**. It records what was asked
   for; the executor replays it on approval. This is what makes an approval
   queue survive a restart and stay auditable.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SCDMixin, TrxBase, UtcDateTime, fk, pk
from app.models.enums import ApprovalMode, ApprovalStatus, GrantScope


class Permission(Base, SCDMixin):
    """One grantable ``(module, action)`` capability.

    Rows are seeded from ``app/services/permissions.py``'s registry, which is
    the single source of truth for what actions exist; the table exists so
    grants can reference them and so Admin sees a real list to tick.
    """

    __tablename__ = "lkp_permission"
    __grain__ = "One version of one grantable permission (module + action)."

    pk_permission_id: Mapped[int] = pk("permission")
    module_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    action_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name_ar: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Sensitive actions default to Maker-Checker when a grant does not say
    #: otherwise — safe by default, overridable per grant.
    default_approval_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ApprovalMode.SINGLE
    )

    __table_args__ = (
        UniqueConstraint("module_code", "action_code", name="permission_module_action"),
    )

    @property
    def code(self) -> str:
        return f"{self.module_code}.{self.action_code}"


class PermissionGrant(Base, SCDMixin):
    """A permission handed to a role or to one specific username (Part I §2.2).

    ``approval_mode`` lives on the grant, not the permission, because the spec
    is explicit that the same action may be Maker-Checker for a Store Manager
    and Single Approval for the General Secretariat (Part I §2.2.1).
    """

    __tablename__ = "scd_permission_grant"
    __grain__ = "One version of one permission granted to one role or one user."

    pk_permission_grant_id: Mapped[int] = pk("permission_grant")
    fk_permission_id: Mapped[int] = fk("permission", "lkp_permission.pk_permission_id")

    #: Exactly one of the two below is set, per ``grant_scope``.
    grant_scope: Mapped[str] = mapped_column(String(10), nullable=False)
    fk_role_id: Mapped[int | None] = fk(
        "role", "scd_role.pk_role_id", nullable=True
    )
    fk_user_id: Mapped[int | None] = fk(
        "user", "scd_user.pk_user_id", nullable=True
    )

    granted_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
        comment="A user-scoped row with granted_flag=0 revokes an inherited role grant.",
    )
    approval_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ApprovalMode.SINGLE
    )
    #: Maker-Checker only: how many distinct eligible checkers must approve.
    #: One is the norm — any single eligible checker resolves it (Part I §2.2.1).
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    permission: Mapped[Permission] = relationship()
    checkers: Mapped[list["ApprovalChecker"]] = relationship(back_populates="grant")
    login_as_scopes: Mapped[list["LoginAsScope"]] = relationship(back_populates="grant")

    __table_args__ = (
        Index("ix_scd_permission_grant_role_lookup", "fk_role_id", "scd_active_flag"),
        Index("ix_scd_permission_grant_user_lookup", "fk_user_id", "scd_active_flag"),
    )

    @property
    def is_maker_checker(self) -> bool:
        return self.approval_mode == ApprovalMode.MAKER_CHECKER


class ApprovalChecker(Base, SCDMixin):
    """Who may check a Maker-Checker grant — any combination of roles and
    usernames (Part I §2.2.1)."""

    __tablename__ = "scd_approval_checker"
    __grain__ = "One version of one eligible checker (role or user) for one grant."

    pk_approval_checker_id: Mapped[int] = pk("approval_checker")
    fk_permission_grant_id: Mapped[int] = fk(
        "permission_grant", "scd_permission_grant.pk_permission_grant_id"
    )
    checker_scope: Mapped[str] = mapped_column(String(10), nullable=False)
    fk_role_id: Mapped[int | None] = fk("role", "scd_role.pk_role_id", nullable=True)
    fk_user_id: Mapped[int | None] = fk("user", "scd_user.pk_user_id", nullable=True)

    grant: Mapped[PermissionGrant] = relationship(back_populates="checkers")


class LoginAsScope(Base, SCDMixin):
    """Whom a "Login As" grant may be used *on* (Part I §2.2.2).

    Scoping lives on the target side: a Store Manager can be allowed to
    impersonate any Customer without that extending to another Store Manager or
    an Admin.
    """

    __tablename__ = "scd_login_as_scope"
    __grain__ = "One version of one allowed impersonation target (role or user) for one grant."

    pk_login_as_scope_id: Mapped[int] = pk("login_as_scope")
    fk_permission_grant_id: Mapped[int] = fk(
        "permission_grant", "scd_permission_grant.pk_permission_grant_id"
    )
    target_scope: Mapped[str] = mapped_column(String(10), nullable=False)
    fk_target_role_id: Mapped[int | None] = fk(
        "target_role", "scd_role.pk_role_id", nullable=True
    )
    fk_target_user_id: Mapped[int | None] = fk(
        "target_user", "scd_user.pk_user_id", nullable=True
    )

    grant: Mapped[PermissionGrant] = relationship(back_populates="login_as_scopes")


class ApprovalRequest(Base, SCDMixin):
    """A pending action awaiting a checker (Part I §2.2.1).

    SCD rather than TRX because the row's status genuinely changes over its
    life; the individual decisions on it are insert-only facts recorded in
    :class:`ApprovalDecision`.
    """

    __tablename__ = "scd_approval_request"
    __grain__ = "One version of one action awaiting maker-checker approval."

    pk_approval_request_id: Mapped[int] = pk("approval_request")
    fk_permission_id: Mapped[int] = fk("permission", "lkp_permission.pk_permission_id")
    fk_permission_grant_id: Mapped[int] = fk(
        "permission_grant", "scd_permission_grant.pk_permission_grant_id"
    )
    #: The maker. Never eligible to check their own request, even if their role
    #: would otherwise qualify them (Part I §2.2.1).
    fk_maker_user_id: Mapped[int] = fk("maker_user", "scd_user.pk_user_id")

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ApprovalStatus.PENDING, index=True
    )
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: What the action targets, kept as plain columns so the queue is
    #: searchable without decoding a payload.
    target_table: Mapped[str | None] = mapped_column(String(80))
    target_row_id: Mapped[int | None] = mapped_column(Integer)
    summary_ar: Mapped[str | None] = mapped_column(Text)
    summary_en: Mapped[str | None] = mapped_column(Text)

    requested_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    resolved_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    executed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    execution_error: Mapped[str | None] = mapped_column(
        Text, comment="Set if the action failed when replayed after approval; never silent."
    )

    permission: Mapped[Permission] = relationship()
    parameters: Mapped[list["ApprovalRequestParameter"]] = relationship(back_populates="request")
    decisions: Mapped[list["ApprovalDecision"]] = relationship(back_populates="request")

    __table_args__ = (
        Index("ix_scd_approval_request_queue", "status", "requested_dt"),
    )


class ApprovalRequestParameter(TrxBase):
    """One argument of a pending action, as a row.

    Deliberately key/value rows rather than a JSON payload column: the arguments
    are tabular, and Part II §1 rules out JSON for anything representable as
    rows. It also means the queue can be filtered on, say, ``amount`` without
    parsing a blob.

    Insert-only: what the maker asked for is a fact. Changing the request means
    rejecting it and raising a new one, so a checker can never approve something
    other than what they reviewed.
    """

    __tablename__ = "trx_approval_request_parameter"
    __grain__ = "One argument of one pending approval request."

    pk_approval_request_parameter_id: Mapped[int] = pk("approval_request_parameter")
    fk_approval_request_id: Mapped[int] = fk(
        "approval_request", "scd_approval_request.pk_approval_request_id"
    )
    param_name: Mapped[str] = mapped_column(String(80), nullable=False)
    param_value: Mapped[str | None] = mapped_column(Text)
    param_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="str",
        comment="str | int | decimal | bool | date | datetime — used to rehydrate the value.",
    )

    request: Mapped[ApprovalRequest] = relationship(back_populates="parameters")

    __table_args__ = (
        UniqueConstraint(
            "fk_approval_request_id", "param_name", name="parameter_per_request"
        ),
    )


class ApprovalDecision(TrxBase):
    """A checker's approve/reject. Insert-only and permanent.

    If the checker's access is later revoked or their account deactivated, this
    row stands — decisions are immutable historical records and do not
    retroactively invalidate (Part I §2.2.1).
    """

    __tablename__ = "trx_approval_decision"
    __grain__ = "One approve/reject decision by one checker on one approval request."

    pk_approval_decision_id: Mapped[int] = pk("approval_decision")
    fk_approval_request_id: Mapped[int] = fk(
        "approval_request", "scd_approval_request.pk_approval_request_id"
    )
    #: Not a live FK — the decider is an immutable historical reference.
    decided_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    request: Mapped[ApprovalRequest] = relationship(back_populates="decisions")


class AuditLog(TrxBase):
    """Who changed what, when (Part I §2.2).

    Written for price changes, discounts applied, permission changes, stock
    adjustments, every maker-checker event, and every impersonated action.
    Before/after values are rows in :class:`AuditLogField`, not a JSON diff.
    """

    __tablename__ = "trx_audit_log"
    __grain__ = "One audited change to one row of one table."

    pk_audit_log_id: Mapped[int] = pk("audit_log")

    #: Immutable historical references, never live FKs: the acting staff account
    #: may later be deactivated and must not break or cascade (Part I §2.2).
    actor_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    actor_username: Mapped[str | None] = mapped_column(
        String(60), comment="Denormalised on purpose: preserves who acted, as named at the time."
    )
    #: Set only when the action was taken during an impersonated session, so
    #: reporting never conflates it with the target's own activity (Part I §2.2.2).
    impersonator_user_id: Mapped[int | None] = mapped_column(Integer, index=True)

    module_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    action_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target_table: Mapped[str | None] = mapped_column(String(80), index=True)
    target_row_id: Mapped[int | None] = mapped_column(Integer, index=True)

    fk_approval_request_id: Mapped[int | None] = fk(
        "approval_request",
        "scd_approval_request.pk_approval_request_id",
        nullable=True,
    )
    ip_address: Mapped[str | None] = mapped_column(String(45))
    session_key: Mapped[str | None] = mapped_column(String(64), index=True)
    note: Mapped[str | None] = mapped_column(Text)

    fields: Mapped[list["AuditLogField"]] = relationship(back_populates="entry")

    __table_args__ = (
        Index("ix_trx_audit_log_target", "target_table", "target_row_id", "created_dt"),
    )


class AuditLogField(TrxBase):
    """One changed column, before and after — tabular, so it is queryable
    ("show every price change over 20%") rather than a blob to eyeball."""

    __tablename__ = "trx_audit_log_field"
    __grain__ = "One column's before/after value within one audit-log entry."

    pk_audit_log_field_id: Mapped[int] = pk("audit_log_field")
    fk_audit_log_id: Mapped[int] = fk("audit_log", "trx_audit_log.pk_audit_log_id")
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)

    entry: Mapped[AuditLog] = relationship(back_populates="fields")
