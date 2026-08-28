"""The maker-checker rules from Part I §2.2.1, as executable proof.

Every admin write will route through this engine, so the rules it enforces are
worth pinning down before the screens that depend on them are built.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import Conflict, PermissionDenied
from app.db.base import Base, utcnow
from app.models.access import (
    ApprovalChecker,
    ApprovalDecision,
    ApprovalRequest,
    Permission,
    PermissionGrant,
)
from app.models.enums import ApprovalMode, ApprovalStatus, GrantScope, RoleCode
from app.models.identity import Role, User
from app.services import approvals
from app.services.permissions import GrantDecision, resolve_grant

MODULE, ACTION = "orders", "apply_invoice_discount"
CODE = f"{MODULE}.{ACTION}"

#: Records what the replayed handler actually received, so the tests can assert
#: the action ran with the arguments the checker reviewed — not just that it ran.
CALLS: list[dict] = []


@pytest.fixture(autouse=True)
def _clean_registry():
    """Register the test handler fresh for each test."""
    CALLS.clear()
    approvals._REGISTRY.pop(CODE, None)

    @approvals.register(MODULE, ACTION)
    def _handler(db: Session, params: dict, actor_user_id: int | None):
        CALLS.append({"params": params, "actor": actor_user_id})
        return params.get("amount")

    yield
    approvals._REGISTRY.pop(CODE, None)


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db: Session) -> dict:
    """A manager (maker) and an admin (checker), with the action set to
    Maker-Checker for the manager's role."""
    now = utcnow()

    manager_role = Role(
        role_code=RoleCode.STORE_MANAGER, name_ar="مدير", name_en="Manager",
        is_staff_flag=True, scd_active_from=now,
    )
    admin_role = Role(
        role_code=RoleCode.ADMIN, name_ar="مدير النظام", name_en="Admin",
        is_staff_flag=True, scd_active_from=now,
    )
    db.add_all([manager_role, admin_role])
    db.flush()

    maker = User(
        fk_role_id=manager_role.pk_role_id, username="manager",
        email="m@example.com", password_hash="x", scd_active_from=now,
    )
    checker = User(
        fk_role_id=admin_role.pk_role_id, username="admin",
        email="a@example.com", password_hash="x", scd_active_from=now,
    )
    other_manager = User(
        fk_role_id=manager_role.pk_role_id, username="manager2",
        email="m2@example.com", password_hash="x", scd_active_from=now,
    )
    db.add_all([maker, checker, other_manager])
    db.flush()

    permission = Permission(
        module_code=MODULE, action_code=ACTION,
        name_ar="خصم", name_en="Apply invoice discount",
        scd_active_from=now,
    )
    db.add(permission)
    db.flush()

    # The manager holds it under Maker-Checker; the admin holds it outright.
    maker_grant = PermissionGrant(
        fk_permission_id=permission.pk_permission_id,
        grant_scope=GrantScope.ROLE, fk_role_id=manager_role.pk_role_id,
        granted_flag=True, approval_mode=ApprovalMode.MAKER_CHECKER,
        required_approvals=1, scd_active_from=now,
    )
    admin_grant = PermissionGrant(
        fk_permission_id=permission.pk_permission_id,
        grant_scope=GrantScope.ROLE, fk_role_id=admin_role.pk_role_id,
        granted_flag=True, approval_mode=ApprovalMode.SINGLE, scd_active_from=now,
    )
    db.add_all([maker_grant, admin_grant])
    db.flush()

    # Only the admin role may check the manager's requests.
    db.add(
        ApprovalChecker(
            fk_permission_grant_id=maker_grant.pk_permission_grant_id,
            checker_scope=GrantScope.ROLE, fk_role_id=admin_role.pk_role_id,
            scd_active_from=now,
        )
    )
    db.commit()

    return {
        "maker": maker, "checker": checker, "other_manager": other_manager,
        "permission": permission, "maker_grant": maker_grant, "admin_grant": admin_grant,
    }


def _submit(db: Session, world: dict, amount="5.000") -> ApprovalRequest:
    decision = resolve_grant(db, world["maker"], MODULE, ACTION)
    result = approvals.execute_or_submit(
        db, None, decision, world["maker"],
        module=MODULE, action=ACTION,
        params={"order_id": 12, "amount": Decimal(amount), "reason": "goodwill"},
        summary_en=f"{amount} JOD off order 12",
    )
    db.commit()
    assert result.pending
    return result.approval_request


# ---------------------------------------------------------------------------
# Routing: execute now vs. park
# ---------------------------------------------------------------------------


def test_single_approval_executes_immediately(db: Session, world: dict):
    decision = resolve_grant(db, world["checker"], MODULE, ACTION)
    assert decision.approval_mode == ApprovalMode.SINGLE

    result = approvals.execute_or_submit(
        db, None, decision, world["checker"],
        module=MODULE, action=ACTION, params={"order_id": 1, "amount": Decimal("2.000")},
    )
    db.commit()

    assert result.executed
    assert len(CALLS) == 1


def test_maker_checker_parks_instead_of_executing(db: Session, world: dict):
    """The same action, a different role — parked, not run (Part I §2.2.1)."""
    approval = _submit(db, world)

    assert approval.status == ApprovalStatus.PENDING
    assert CALLS == [], "the action must not run before approval"


def test_the_same_action_can_differ_by_role(db: Session, world: dict):
    """§2.2.1's worked example: Maker-Checker for one role, Single for another."""
    assert resolve_grant(db, world["maker"], MODULE, ACTION).approval_mode == (
        ApprovalMode.MAKER_CHECKER
    )
    assert resolve_grant(db, world["checker"], MODULE, ACTION).approval_mode == (
        ApprovalMode.SINGLE
    )


# ---------------------------------------------------------------------------
# The maker/checker separation
# ---------------------------------------------------------------------------


def test_maker_cannot_approve_their_own_request(db: Session, world: dict):
    approval = _submit(db, world)
    with pytest.raises(PermissionDenied):
        approvals.approve(db, None, approval, world["maker"])
    assert CALLS == []


def test_maker_cannot_approve_even_when_their_role_qualifies(db: Session, world: dict):
    """The sharp edge of §2.2.1: holding a checker role does not exempt you.

    Here the maker is *added* as an eligible checker for their own grant. The
    maker exclusion must still win.
    """
    approval = _submit(db, world)
    db.add(
        ApprovalChecker(
            fk_permission_grant_id=world["maker_grant"].pk_permission_grant_id,
            checker_scope=GrantScope.USER,
            fk_user_id=world["maker"].pk_user_id,
            scd_active_from=utcnow(),
        )
    )
    db.commit()

    assert approvals.is_eligible_checker(db, approval, world["maker"]) is False
    with pytest.raises(PermissionDenied):
        approvals.approve(db, None, approval, world["maker"])


def test_ineligible_checker_is_refused(db: Session, world: dict):
    """Another manager holds the permission but is not a configured checker."""
    approval = _submit(db, world)
    with pytest.raises(PermissionDenied):
        approvals.approve(db, None, approval, world["other_manager"])


def test_maker_does_not_see_their_own_request_in_the_queue(db: Session, world: dict):
    _submit(db, world)
    assert approvals.pending_queue(db, world["maker"]) == []
    assert len(approvals.pending_queue(db, world["checker"])) == 1


def test_maker_sees_their_own_request_under_my_requests(db: Session, world: dict):
    _submit(db, world)
    assert len(approvals.my_requests(db, world["maker"])) == 1


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def test_approval_replays_the_action_with_stored_parameters(db: Session, world: dict):
    """The checker approves what they reviewed — types survive the round trip."""
    approval = _submit(db, world, amount="7.500")

    result = approvals.approve(db, None, approval, world["checker"], note="fine")
    db.commit()

    assert result.executed
    assert len(CALLS) == 1
    params = CALLS[0]["params"]
    assert params["order_id"] == 12 and isinstance(params["order_id"], int)
    assert params["amount"] == Decimal("7.500") and isinstance(params["amount"], Decimal)
    assert params["reason"] == "goodwill"
    # The action is attributed to the maker, not the checker.
    assert CALLS[0]["actor"] == world["maker"].pk_user_id

    db.refresh(approval)
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.executed_dt is not None


def test_rejection_discards_the_action(db: Session, world: dict):
    approval = _submit(db, world)
    approvals.reject(db, None, approval, world["checker"], note="not justified")
    db.commit()

    assert approval.status == ApprovalStatus.REJECTED
    assert CALLS == [], "a rejected action must never run"


def test_a_decided_request_cannot_be_decided_again(db: Session, world: dict):
    approval = _submit(db, world)
    approvals.approve(db, None, approval, world["checker"])
    db.commit()

    with pytest.raises(Conflict):
        approvals.approve(db, None, approval, world["checker"])


def test_maker_can_withdraw_their_own_request(db: Session, world: dict):
    approval = _submit(db, world)
    approvals.cancel(db, None, approval, world["maker"])
    db.commit()

    assert approval.status == ApprovalStatus.CANCELLED
    assert CALLS == []


def test_only_the_maker_can_withdraw(db: Session, world: dict):
    approval = _submit(db, world)
    with pytest.raises(PermissionDenied):
        approvals.cancel(db, None, approval, world["checker"])


# ---------------------------------------------------------------------------
# Multiple approvals
# ---------------------------------------------------------------------------


def test_two_required_approvals_need_two_distinct_checkers(db: Session, world: dict):
    """Admin may configure more than one required approval (Part I §2.2.1)."""
    now = utcnow()
    world["maker_grant"].required_approvals = 2

    second_checker = User(
        fk_role_id=world["checker"].fk_role_id, username="admin2",
        email="a2@example.com", password_hash="x", scd_active_from=now,
    )
    db.add(second_checker)
    db.commit()

    approval = _submit(db, world)

    first = approvals.approve(db, None, approval, world["checker"])
    db.commit()
    assert first.pending, "one approval is not enough"
    assert CALLS == []

    # The same person cannot satisfy both.
    with pytest.raises(Conflict):
        approvals.approve(db, None, approval, world["checker"])

    second = approvals.approve(db, None, approval, second_checker)
    db.commit()
    assert second.executed
    assert len(CALLS) == 1


# ---------------------------------------------------------------------------
# Durability of history
# ---------------------------------------------------------------------------


def test_decisions_survive_the_checker_being_deactivated(db: Session, world: dict):
    """A revoked checker's past decisions remain valid history (§2.2.1)."""
    approval = _submit(db, world)
    approvals.approve(db, None, approval, world["checker"], note="approved before leaving")
    db.commit()

    # The checker leaves: account deactivated and the grant closed.
    world["checker"].is_active_flag = False
    world["admin_grant"].close(changed_by=None)
    db.commit()

    decision = db.scalars(
        select(ApprovalDecision).where(
            ApprovalDecision.fk_approval_request_id == approval.pk_approval_request_id
        )
    ).one()
    assert decision.decision == ApprovalStatus.APPROVED
    assert decision.note == "approved before leaving"

    db.refresh(approval)
    assert approval.status == ApprovalStatus.APPROVED, (
        "an executed approval must not retroactively invalidate"
    )


def test_execution_failure_is_recorded_not_swallowed(db: Session, world: dict):
    """An approved action that cannot be carried out records why (Part II §5)."""
    from app.core.errors import OutOfStock

    approvals._REGISTRY.pop(CODE)

    @approvals.register(MODULE, ACTION)
    def _failing(db_: Session, params: dict, actor: int | None):
        raise OutOfStock("nothing left")

    approval = _submit(db, world)
    result = approvals.approve(db, None, approval, world["checker"])
    db.commit()

    assert result.executed is True
    assert approval.execution_error is not None
    assert "out_of_stock" in approval.execution_error
    assert approval.executed_dt is None, "a failed replay is not an execution"
