"""Access/user/session actions registered with maker-checker (§2.2, §2.8)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services import access_admin, approvals


@approvals.register("access", "grant_permission")
def grant_permission(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return access_admin.set_permission_grant(
        db,
        permission_id=int(params["permission_id"]),
        grant_scope=params["grant_scope"],
        role_id=params.get("role_id"),
        user_id=params.get("user_id"),
        granted=True,
        approval_mode=params.get("approval_mode"),
        required_approvals=int(params.get("required_approvals") or 1),
        checkers=_checker_specs(params),
        login_as_scopes=_login_as_scope_specs(params),
        actor_user_id=actor_user_id,
    )


@approvals.register("access", "revoke_permission")
def revoke_permission(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return access_admin.revoke_permission_grant(
        db,
        permission_id=int(params["permission_id"]),
        grant_scope=params["grant_scope"],
        role_id=params.get("role_id"),
        user_id=params.get("user_id"),
        actor_user_id=actor_user_id,
    )


@approvals.register("access", "set_approval_mode")
def set_approval_mode(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return access_admin.set_permission_grant(
        db,
        permission_id=int(params["permission_id"]),
        grant_scope=params["grant_scope"],
        role_id=params.get("role_id"),
        user_id=params.get("user_id"),
        granted=bool(params.get("granted", True)),
        approval_mode=params.get("approval_mode"),
        required_approvals=int(params.get("required_approvals") or 1),
        checkers=_checker_specs(params),
        login_as_scopes=_login_as_scope_specs(params),
        actor_user_id=actor_user_id,
    )


@approvals.register("access", "login_as")
def login_as_request(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Approval record for a Login-As request.

    The browser session switch itself needs a response cookie, so it is started
    by the route after a Single grant. Maker-Checker grants still create an
    auditable approval request instead of silently denying the attempt.
    """
    return params.get("target_user_id")


@approvals.register("users", "create_staff")
def create_staff(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return access_admin.create_staff_user(
        db,
        role_id=int(params["role_id"]),
        username=params["username"],
        email=params["email"],
        password=params["password"],
        preferred_language=params.get("preferred_language") or "ar",
        actor_user_id=actor_user_id,
    )


@approvals.register("users", "deactivate")
def deactivate_user(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return access_admin.deactivate_user(
        db,
        user_id=int(params["user_id"]),
        actor_user_id=actor_user_id,
    )


@approvals.register("users", "terminate_session")
def terminate_session(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return access_admin.terminate_session(
        db,
        session_id=int(params["session_id"]),
        actor_user_id=actor_user_id,
    )


def _checker_specs(params: dict[str, Any]) -> list[access_admin.CheckerSpec]:
    specs: list[access_admin.CheckerSpec] = []
    for index in range(1, int(params.get("checker_count") or 0) + 1):
        scope = params.get(f"checker_scope_{index}")
        if not scope:
            continue
        specs.append(
            access_admin.CheckerSpec(
                checker_scope=scope,
                role_id=params.get(f"checker_role_id_{index}"),
                user_id=params.get(f"checker_user_id_{index}"),
            )
        )
    return specs


def _login_as_scope_specs(params: dict[str, Any]) -> list[access_admin.LoginAsScopeSpec]:
    specs: list[access_admin.LoginAsScopeSpec] = []
    for index in range(1, int(params.get("login_scope_count") or 0) + 1):
        scope = params.get(f"login_scope_{index}")
        if not scope:
            continue
        specs.append(
            access_admin.LoginAsScopeSpec(
                target_scope=scope,
                role_id=params.get(f"login_scope_role_id_{index}"),
                user_id=params.get(f"login_scope_user_id_{index}"),
            )
        )
    return specs
