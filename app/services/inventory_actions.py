"""Inventory actions registered with the maker-checker engine (§11, §2.2.1).

Write-off and stock adjustment default to Maker-Checker in the permission
registry: both make stock disappear without a sale, which is precisely the
shrinkage path an approval trail exists to protect.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.models.inventory import StockTake
from app.services import approvals, inventory


@approvals.register("inventory", "write_off")
def write_off(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Shrinkage, for owned stock or for units lost in custody (§11, §7).

    Both go through one permission and one handler because they are the same
    decision — stock disappearing without a sale — and splitting them would let
    a role write off consigned goods while barred from writing off our own.
    """
    if params.get("consignment_item_id"):
        from app.models.consignment import ConsignmentItem
        from app.services import consignment

        item = db.get(ConsignmentItem, params["consignment_item_id"])
        if item is None:
            raise NotFound("That consignment item does not exist.")
        consignment.mark_damaged_or_lost(
            db,
            item,
            quantity=int(params["quantity"]),
            reason=params["reason"],
            note=params.get("note"),
            actor_user_id=actor_user_id,
        )
        return params["quantity"]

    inventory.write_off(
        db,
        variant_id=params["variant_id"],
        stock_pool_id=params["stock_pool_id"],
        quantity=int(params["quantity"]),
        reason=params["reason"],
        note=params.get("note"),
        actor_user_id=actor_user_id,
    )
    return params["quantity"]


@approvals.register("inventory", "adjust_stock")
def close_stock_take(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Closing a stock take posts every variance as an adjustment movement."""
    stock_take = db.get(StockTake, params["stock_take_id"])
    if stock_take is None:
        raise NotFound("That stock take does not exist.")
    inventory.close_stock_take(db, stock_take, actor_user_id=actor_user_id)
    return stock_take.reference
