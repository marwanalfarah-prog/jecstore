"""Promocode actions registered with the maker-checker engine."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.services import approvals, promocode_admin


@approvals.register("content", "manage_promocodes")
def manage_promocodes(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    operation = params.get("operation")
    if operation == "close":
        return promocode_admin.close_promocode(
            db,
            promocode_id=int(params["promocode_id"]),
            actor_user_id=actor_user_id,
        )

    restrictions = _restrictions(params)
    payload = dict(
        code=params["code"],
        promocode_kind=params["promocode_kind"],
        name_ar=params.get("name_ar"),
        name_en=params.get("name_en"),
        percentage=_decimal(params.get("percentage")),
        max_discount_amt=_decimal(params.get("max_discount_amt")),
        fixed_amount_amt=_decimal(params.get("fixed_amount_amt")),
        minimum_order_amt=_decimal(params.get("minimum_order_amt")),
        starts_dt=params.get("starts_dt"),
        expires_dt=params.get("expires_dt"),
        single_use_globally=bool(params.get("single_use_globally", False)),
        max_total_uses=params.get("max_total_uses"),
        max_uses_per_customer=params.get("max_uses_per_customer"),
        stacks_with_item_discount=bool(params.get("stacks_with_item_discount", False)),
        applies_to_consigned=bool(params.get("applies_to_consigned", False)),
        note=params.get("note"),
        restrictions=restrictions,
        actor_user_id=actor_user_id,
    )
    if operation == "update":
        return promocode_admin.update_promocode(
            db,
            promocode_id=int(params["promocode_id"]),
            **payload,
        )
    return promocode_admin.create_promocode(db, **payload)


def _restrictions(params: dict[str, Any]) -> list[promocode_admin.RestrictionSpec]:
    count = int(params.get("restriction_count") or 0)
    restrictions: list[promocode_admin.RestrictionSpec] = []
    for idx in range(1, count + 1):
        target_type = params.get(f"restriction_{idx}_type")
        target_id = params.get(f"restriction_{idx}_id")
        if not target_type or target_id is None:
            continue
        restrictions.append(
            promocode_admin.RestrictionSpec(
                target_type=str(target_type),
                target_id=int(target_id),
                is_exclusion=bool(params.get(f"restriction_{idx}_exclusion", False)),
            )
        )
    return restrictions


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))
