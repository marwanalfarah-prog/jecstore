"""Access management and Login-As support (§2.2, §2.2.2, §2.8)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import LastAdminLockout, PermissionDenied
from app.db.base import utcnow
from app.models.access import ApprovalChecker, LoginAsScope, Permission
from app.models.activity import UserSession
from app.models.enums import ApprovalMode, GrantScope, RoleCode, SessionEndReason
from app.models.identity import Role, User
from app.services import access_admin, permissions
from tests.test_checkout import db  # noqa: F401 - fixture


def _role(db: Session, code: str, *, staff: bool = True) -> Role:
    role = Role(
        role_code=code,
        name_ar=code,
        name_en=code,
        is_staff_flag=staff,
        scd_active_from=utcnow(),
    )
    db.add(role)
    db.flush()
    return role


def _user(db: Session, role: Role, username: str) -> User:
    user = User(
        fk_role_id=role.pk_role_id,
        username=username,
        email=f"{username}@example.com",
        password_hash="x",
        email_verified_flag=True,
        is_active_flag=True,
        scd_active_from=utcnow(),
    )
    db.add(user)
    db.flush()
    return user


def _permission(
    db: Session,
    module: str,
    action: str,
    *,
    mode: str = ApprovalMode.SINGLE,
) -> Permission:
    permission = Permission(
        module_code=module,
        action_code=action,
        name_ar=f"{module}.{action}",
        name_en=f"{module}.{action}",
        default_approval_mode=mode,
        scd_active_from=utcnow(),
    )
    db.add(permission)
    db.flush()
    return permission


def test_grant_versions_checkers_and_login_as_scopes(db: Session):
    manager = _role(db, "manager")
    checker_role = _role(db, "checker")
    customer_role = _role(db, "customer", staff=False)
    maker = _user(db, manager, "manager")
    target = _user(db, customer_role, "shopper")
    permission = _permission(db, "access", "login_as", mode=ApprovalMode.MAKER_CHECKER)

    grant = access_admin.set_permission_grant(
        db,
        permission_id=permission.pk_permission_id,
        grant_scope=GrantScope.ROLE,
        role_id=manager.pk_role_id,
        approval_mode=ApprovalMode.MAKER_CHECKER,
        required_approvals=1,
        checkers=[
            access_admin.CheckerSpec(
                checker_scope=GrantScope.ROLE,
                role_id=checker_role.pk_role_id,
            )
        ],
        login_as_scopes=[
            access_admin.LoginAsScopeSpec(
                target_scope=GrantScope.ROLE,
                role_id=customer_role.pk_role_id,
            )
        ],
    )
    db.commit()

    decision = permissions.resolve_grant(db, maker, "access", "login_as")
    assert decision.allowed is True
    assert decision.needs_approval is True
    assert db.scalar(select(ApprovalChecker).where(ApprovalChecker.fk_permission_grant_id == grant.pk_permission_grant_id)) is not None
    assert db.scalar(select(LoginAsScope).where(LoginAsScope.fk_permission_grant_id == grant.pk_permission_grant_id)) is not None
    assert access_admin.assert_login_as_allowed(
        db,
        grant_id=grant.pk_permission_grant_id,
        target_user=target,
    ) == grant


def test_user_denied_grant_overrides_role_grant(db: Session):
    role = _role(db, "manager")
    user = _user(db, role, "manager")
    permission = _permission(db, "orders", "view")

    access_admin.set_permission_grant(
        db,
        permission_id=permission.pk_permission_id,
        grant_scope=GrantScope.ROLE,
        role_id=role.pk_role_id,
        granted=True,
    )
    access_admin.revoke_permission_grant(
        db,
        permission_id=permission.pk_permission_id,
        grant_scope=GrantScope.USER,
        user_id=user.pk_user_id,
    )
    db.commit()

    assert permissions.resolve_grant(db, user, "orders", "view").allowed is False


def test_login_as_requires_configured_target_scope(db: Session):
    manager = _role(db, "manager")
    customer_role = _role(db, "customer", staff=False)
    other_role = _role(db, "other", staff=False)
    target = _user(db, other_role, "outside")
    permission = _permission(db, "access", "login_as")
    grant = access_admin.set_permission_grant(
        db,
        permission_id=permission.pk_permission_id,
        grant_scope=GrantScope.ROLE,
        role_id=manager.pk_role_id,
        login_as_scopes=[
            access_admin.LoginAsScopeSpec(
                target_scope=GrantScope.ROLE,
                role_id=customer_role.pk_role_id,
            )
        ],
    )

    with pytest.raises(PermissionDenied):
        access_admin.assert_login_as_allowed(
            db,
            grant_id=grant.pk_permission_grant_id,
            target_user=target,
        )


def test_create_staff_user_and_deactivate_closes_sessions(db: Session):
    admin_role = _role(db, RoleCode.ADMIN)
    manager_role = _role(db, RoleCode.STORE_MANAGER)
    _user(db, admin_role, "admin")
    staff = access_admin.create_staff_user(
        db,
        role_id=manager_role.pk_role_id,
        username="new.manager",
        email="new.manager@example.com",
        password="ChangeMe2026",
        preferred_language="en",
    )
    session = UserSession(
        session_key="abc",
        fk_user_id=staff.pk_user_id,
        started_dt=utcnow(),
        last_seen_dt=utcnow(),
        expires_dt=utcnow(),
        scd_active_from=utcnow(),
    )
    db.add(session)
    db.commit()

    access_admin.deactivate_user(db, user_id=staff.pk_user_id)
    db.commit()

    assert staff.email_verified_flag is True
    assert staff.is_active_flag is False
    assert session.scd_active_flag is False
    assert session.end_reason == SessionEndReason.FORCED_BY_ADMIN


def test_deactivate_last_admin_is_blocked(db: Session):
    admin_role = _role(db, RoleCode.ADMIN)
    admin = _user(db, admin_role, "admin")

    with pytest.raises(LastAdminLockout):
        access_admin.deactivate_user(db, user_id=admin.pk_user_id)
