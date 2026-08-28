"""Admin access-management operations (Part I §2.2, §2.2.1, §2.2.2)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.security import (
    hash_password,
    is_valid_username,
    normalize_email,
    normalize_username,
    password_problems,
)
from app.db.base import utcnow
from app.models.access import (
    ApprovalChecker,
    LoginAsScope,
    Permission,
    PermissionGrant,
)
from app.models.activity import UserSession
from app.models.enums import (
    ActivityEvent,
    ApprovalMode,
    GrantScope,
    Language,
    SessionEndReason,
)
from app.models.identity import Role, User
from app.services import permissions, sessions
from app.services.activity import record_event


@dataclass(frozen=True, slots=True)
class CheckerSpec:
    checker_scope: str
    role_id: int | None = None
    user_id: int | None = None


@dataclass(frozen=True, slots=True)
class LoginAsScopeSpec:
    target_scope: str
    role_id: int | None = None
    user_id: int | None = None


def active_permissions(db: Session) -> list[Permission]:
    return list(
        db.scalars(
            select(Permission)
            .where(Permission.scd_active_flag.is_(True))
            .order_by(Permission.module_code, Permission.action_code)
        ).all()
    )


def active_roles(db: Session, *, staff_only: bool | None = None) -> list[Role]:
    stmt = select(Role).where(Role.scd_active_flag.is_(True)).order_by(Role.role_code)
    if staff_only is not None:
        stmt = stmt.where(Role.is_staff_flag.is_(staff_only))
    return list(db.scalars(stmt).all())


def active_users(db: Session, *, staff_only: bool | None = None) -> list[User]:
    stmt = (
        select(User)
        .join(Role, Role.pk_role_id == User.fk_role_id)
        .where(User.scd_active_flag.is_(True))
        .order_by(User.username)
    )
    if staff_only is not None:
        stmt = stmt.where(Role.is_staff_flag.is_(staff_only))
    return list(db.scalars(stmt).all())


def active_grants(db: Session) -> list[PermissionGrant]:
    return list(
        db.scalars(
            select(PermissionGrant)
            .where(PermissionGrant.scd_active_flag.is_(True))
            .order_by(PermissionGrant.fk_permission_id, PermissionGrant.pk_permission_grant_id)
        ).all()
    )


def set_permission_grant(
    db: Session,
    *,
    permission_id: int,
    grant_scope: str,
    role_id: int | None = None,
    user_id: int | None = None,
    granted: bool = True,
    approval_mode: str = ApprovalMode.SINGLE,
    required_approvals: int = 1,
    checkers: list[CheckerSpec] | None = None,
    login_as_scopes: list[LoginAsScopeSpec] | None = None,
    actor_user_id: int | None = None,
) -> PermissionGrant:
    """Version one permission grant and attach checker/scope rules."""
    permission = db.get(Permission, permission_id)
    if permission is None or not permission.scd_active_flag:
        raise NotFound("That permission does not exist.")
    _validate_grant_target(db, grant_scope, role_id, user_id)
    if approval_mode not in set(ApprovalMode):
        raise ValidationFailed("Unknown approval mode.")
    if required_approvals < 1:
        raise ValidationFailed("At least one approval is required.")

    existing = _active_grant_for(
        db,
        permission_id=permission_id,
        grant_scope=grant_scope,
        role_id=role_id,
        user_id=user_id,
    )
    if existing is not None:
        _close_grant_tree(existing, changed_by=actor_user_id)

    now = utcnow()
    grant = PermissionGrant(
        fk_permission_id=permission_id,
        grant_scope=grant_scope,
        fk_role_id=role_id if grant_scope == GrantScope.ROLE else None,
        fk_user_id=user_id if grant_scope == GrantScope.USER else None,
        granted_flag=granted,
        approval_mode=approval_mode,
        required_approvals=max(1, required_approvals),
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(grant)
    db.flush()

    if granted and approval_mode == ApprovalMode.MAKER_CHECKER:
        for spec in checkers or []:
            _add_checker(db, grant, spec, now, actor_user_id)

    if granted and permission.module_code == "access" and permission.action_code == "login_as":
        for spec in login_as_scopes or []:
            _add_login_as_scope(db, grant, spec, now, actor_user_id)

    db.flush()
    return grant


def revoke_permission_grant(
    db: Session,
    *,
    permission_id: int,
    grant_scope: str,
    role_id: int | None = None,
    user_id: int | None = None,
    actor_user_id: int | None = None,
) -> PermissionGrant:
    """Insert an explicit denied grant version instead of deleting history."""
    return set_permission_grant(
        db,
        permission_id=permission_id,
        grant_scope=grant_scope,
        role_id=role_id,
        user_id=user_id,
        granted=False,
        approval_mode=ApprovalMode.SINGLE,
        required_approvals=1,
        actor_user_id=actor_user_id,
    )


def create_staff_user(
    db: Session,
    *,
    role_id: int,
    username: str,
    email: str,
    password: str,
    preferred_language: str = Language.AR,
    actor_user_id: int | None = None,
) -> User:
    role = db.get(Role, role_id)
    if role is None or not role.scd_active_flag or not role.is_staff_flag:
        raise ValidationFailed("Choose a staff role.")
    username = normalize_username(username)
    email = normalize_email(email)
    if not is_valid_username(username):
        raise ValidationFailed("That username is not valid.")
    if password_problems(password):
        raise ValidationFailed("That password does not meet the policy.")
    if _user_by_username(db, username) is not None:
        raise Conflict("That username is already in use.")
    if _user_by_email(db, email) is not None:
        raise Conflict("That email is already in use.")

    now = utcnow()
    user = User(
        fk_role_id=role_id,
        username=username,
        email=email,
        password_hash=hash_password(password),
        password_changed_dt=now,
        email_verified_flag=True,
        email_verified_dt=now,
        is_active_flag=True,
        preferred_language=preferred_language if preferred_language in set(Language) else Language.AR,
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(user)
    db.flush()
    return user


def deactivate_user(
    db: Session,
    *,
    user_id: int,
    actor_user_id: int | None = None,
) -> User:
    user = _user(db, user_id)
    permissions.assert_not_last_admin(db, user)
    if not user.is_active_flag:
        raise Conflict("That account is already inactive.")
    user.is_active_flag = False
    user.scd_changed_by = actor_user_id
    sessions.end_all_sessions_for_user(db, user_id, SessionEndReason.FORCED_BY_ADMIN)
    db.flush()
    return user


def terminate_session(
    db: Session,
    *,
    session_id: int,
    actor_user_id: int | None = None,
) -> UserSession:
    session = db.get(UserSession, session_id)
    if session is None or not session.scd_active_flag:
        raise NotFound("That session does not exist.")
    sessions.end_session(db, session, SessionEndReason.FORCED_BY_ADMIN)
    session.scd_changed_by = actor_user_id
    db.flush()
    return session


def assert_login_as_allowed(
    db: Session,
    *,
    grant_id: int | None,
    target_user: User,
) -> PermissionGrant:
    if grant_id is None:
        raise PermissionDenied("Login-As requires a concrete permission grant.")
    grant = db.get(PermissionGrant, grant_id)
    if grant is None or not grant.scd_active_flag or not grant.granted_flag:
        raise PermissionDenied("Login-As is not granted.")

    scopes = db.scalars(
        select(LoginAsScope).where(
            LoginAsScope.fk_permission_grant_id == grant.pk_permission_grant_id,
            LoginAsScope.scd_active_flag.is_(True),
        )
    ).all()
    if not scopes:
        raise PermissionDenied("Login-As target scopes are not configured.")
    for scope in scopes:
        if scope.target_scope == GrantScope.USER and scope.fk_target_user_id == target_user.pk_user_id:
            return grant
        if scope.target_scope == GrantScope.ROLE and scope.fk_target_role_id == target_user.fk_role_id:
            return grant
    raise PermissionDenied("That user is outside your Login-As scope.")


def start_impersonation(
    db: Session,
    request: Request,
    *,
    impersonator: User,
    target_user_id: int,
    grant_id: int | None,
    parent_session_key: str | None,
    context=None,
) -> UserSession:
    target = _user(db, target_user_id)
    if not target.is_active_flag:
        raise Conflict("That account is inactive.")
    if target.pk_user_id == impersonator.pk_user_id:
        raise ValidationFailed("Choose a different user to impersonate.")
    assert_login_as_allowed(db, grant_id=grant_id, target_user=target)
    session = sessions.create_session(
        db,
        target,
        request,
        impersonator_user_id=impersonator.pk_user_id,
        parent_session_key=parent_session_key,
    )
    record_event(
        db,
        ActivityEvent.IMPERSONATION_STARTED,
        request=request,
        context=context,
        target_table="scd_user",
        target_row_id=target.pk_user_id,
        success=True,
        detail=f"{impersonator.username} -> {target.username}",
    )
    return session


def _active_grant_for(
    db: Session,
    *,
    permission_id: int,
    grant_scope: str,
    role_id: int | None,
    user_id: int | None,
) -> PermissionGrant | None:
    stmt = select(PermissionGrant).where(
        PermissionGrant.fk_permission_id == permission_id,
        PermissionGrant.grant_scope == grant_scope,
        PermissionGrant.scd_active_flag.is_(True),
    )
    if grant_scope == GrantScope.ROLE:
        stmt = stmt.where(PermissionGrant.fk_role_id == role_id)
    else:
        stmt = stmt.where(PermissionGrant.fk_user_id == user_id)
    return db.scalars(stmt).first()


def _close_grant_tree(grant: PermissionGrant, *, changed_by: int | None) -> None:
    grant.close(changed_by=changed_by)
    for checker in grant.checkers:
        if checker.scd_active_flag:
            checker.close(changed_by=changed_by)
    for scope in grant.login_as_scopes:
        if scope.scd_active_flag:
            scope.close(changed_by=changed_by)


def _add_checker(
    db: Session,
    grant: PermissionGrant,
    spec: CheckerSpec,
    now: dt.datetime,
    actor_user_id: int | None,
) -> None:
    if spec.checker_scope == GrantScope.ROLE:
        if spec.role_id is None or db.get(Role, spec.role_id) is None:
            raise ValidationFailed("Choose a checker role.")
    elif spec.checker_scope == GrantScope.USER:
        if spec.user_id is None or db.get(User, spec.user_id) is None:
            raise ValidationFailed("Choose a checker user.")
    else:
        raise ValidationFailed("Unknown checker scope.")

    db.add(
        ApprovalChecker(
            fk_permission_grant_id=grant.pk_permission_grant_id,
            checker_scope=spec.checker_scope,
            fk_role_id=spec.role_id if spec.checker_scope == GrantScope.ROLE else None,
            fk_user_id=spec.user_id if spec.checker_scope == GrantScope.USER else None,
            scd_active_from=now,
            scd_changed_by=actor_user_id,
        )
    )


def _add_login_as_scope(
    db: Session,
    grant: PermissionGrant,
    spec: LoginAsScopeSpec,
    now: dt.datetime,
    actor_user_id: int | None,
) -> None:
    if spec.target_scope == GrantScope.ROLE:
        if spec.role_id is None or db.get(Role, spec.role_id) is None:
            raise ValidationFailed("Choose a target role.")
    elif spec.target_scope == GrantScope.USER:
        if spec.user_id is None or db.get(User, spec.user_id) is None:
            raise ValidationFailed("Choose a target user.")
    else:
        raise ValidationFailed("Unknown Login-As target scope.")

    db.add(
        LoginAsScope(
            fk_permission_grant_id=grant.pk_permission_grant_id,
            target_scope=spec.target_scope,
            fk_target_role_id=spec.role_id if spec.target_scope == GrantScope.ROLE else None,
            fk_target_user_id=spec.user_id if spec.target_scope == GrantScope.USER else None,
            scd_active_from=now,
            scd_changed_by=actor_user_id,
        )
    )


def _validate_grant_target(
    db: Session,
    grant_scope: str,
    role_id: int | None,
    user_id: int | None,
) -> None:
    if grant_scope == GrantScope.ROLE:
        if role_id is None or db.get(Role, role_id) is None:
            raise ValidationFailed("Choose a role.")
        return
    if grant_scope == GrantScope.USER:
        if user_id is None or db.get(User, user_id) is None:
            raise ValidationFailed("Choose a user.")
        return
    raise ValidationFailed("Unknown grant scope.")


def _user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or not user.scd_active_flag:
        raise NotFound("That user does not exist.")
    return user


def _user_by_username(db: Session, username: str) -> User | None:
    return db.scalars(
        select(User).where(
            User.username == username,
            User.scd_active_flag.is_(True),
        )
    ).first()


def _user_by_email(db: Session, email: str) -> User | None:
    return db.scalars(
        select(User).where(
            User.email == email,
            User.scd_active_flag.is_(True),
        )
    ).first()
