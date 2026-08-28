"""Money-box actions registered with the maker-checker engine (§10, §11).

Routes submit scalar parameters only; for split transactions the allocations are
stored as numbered fields so the action can be replayed without JSON payloads.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.models.enums import MoneyDirection, MoneyReason
from app.services import approvals, money


@approvals.register("money_boxes", "close_box")
def close_box(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return money.close_box(
        db,
        int(params["money_box_id"]),
        actor_user_id=actor_user_id,
    )


@approvals.register("money_boxes", "create_box")
def create_or_close_box(db: Session, params: dict[str, Any], actor_user_id: int | None):
    # `operation` is retained so approval requests queued before close_box
    # became its own action still replay correctly.
    if (params.get("operation") or "create_box") == "close_box":
        return close_box(db, params, actor_user_id)

    return money.create_box(
        db,
        box_code=params["box_code"],
        name_ar=params["name_ar"],
        name_en=params["name_en"],
        opening_balance_amt=Decimal(params.get("opening_balance_amt") or 0),
        branch_id=params.get("branch_id"),
        description=params.get("description"),
        actor_user_id=actor_user_id,
    )


@approvals.register("money_boxes", "create_transaction")
def create_transaction(db: Session, params: dict[str, Any], actor_user_id: int | None):
    operation = params.get("operation") or "transaction"
    if operation == "operating_cost":
        return money.record_operating_cost(
            db,
            name_ar=params["name_ar"],
            name_en=params["name_en"],
            category_code=params["category_code"],
            amount_amt=Decimal(params["amount_amt"]),
            incurred_date=params.get("incurred_date") or dt.date.today(),
            money_box_id=int(params["money_box_id"]),
            is_recurring_flag=bool(params.get("is_recurring_flag")),
            recurrence_months=params.get("recurrence_months"),
            branch_id=params.get("branch_id"),
            note=params.get("note"),
            actor_user_id=actor_user_id,
        )

    direction = params.get("direction") or MoneyDirection.IN
    allocations = _allocations(params, direction)
    occurred_date = params.get("occurred_date")
    occurred_dt = (
        dt.datetime.combine(occurred_date, dt.time.min, tzinfo=dt.timezone.utc)
        if occurred_date is not None
        else None
    )
    return money.record_transaction(
        db,
        direction=direction,
        reason_code=params.get("reason_code") or MoneyReason.OTHER,
        allocations=allocations,
        channel_id=params.get("channel_id"),
        description=params.get("description"),
        actor_user_id=actor_user_id,
        occurred_dt=occurred_dt,
    )


@approvals.register("money_boxes", "reconcile")
def reconcile_box(db: Session, params: dict[str, Any], actor_user_id: int | None):
    return money.reconcile_box(
        db,
        money_box_id=int(params["money_box_id"]),
        counted_amt=Decimal(params["counted_amt"]),
        note=params.get("note"),
        adjust=bool(params.get("adjust")),
        actor_user_id=actor_user_id,
    )


def _allocations(
    params: dict[str, Any],
    direction: str,
) -> list[tuple[int, Decimal]]:
    count = int(params.get("allocation_count") or 0)
    sign = Decimal("1") if direction == MoneyDirection.IN else Decimal("-1")
    allocations: list[tuple[int, Decimal]] = []

    for index in range(1, count + 1):
        box_id = params.get(f"allocation_box_id_{index}")
        amount_raw = params.get(f"allocation_amount_amt_{index}")
        if not box_id or amount_raw in (None, ""):
            continue

        amount = abs(Decimal(amount_raw))
        if amount == 0:
            continue
        allocations.append((int(box_id), sign * amount))

    if not allocations:
        raise ValidationFailed("Add at least one money-box allocation.")
    return allocations
