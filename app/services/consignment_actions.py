"""Consignment actions registered with the maker-checker engine (§7, §2.2.1).

Settlement defaults to Maker-Checker in the permission registry: it moves money
to or from a third party, which is exactly the class of action a second pair of
eyes exists for.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.models.consignment import Consignment, ConsignmentItem
from app.services import approvals, consignment


def _item(db: Session, item_id: int) -> ConsignmentItem:
    item = db.get(ConsignmentItem, item_id)
    if item is None or not item.scd_active_flag:
        raise NotFound("That consignment item does not exist.")
    return item


@approvals.register("consignment", "record_return")
def record_return(db: Session, params: dict[str, Any], actor_user_id: int | None):
    """Return unsold units. A partial quantity is §7's partial recall."""
    item = _item(db, params["item_id"])
    consignment.return_to_consignor(
        db, item, quantity=params.get("quantity"), actor_user_id=actor_user_id
    )
    return item.state


@approvals.register("consignment", "settle")
def settle(db: Session, params: dict[str, Any], actor_user_id: int | None):
    arrangement = db.get(Consignment, params["consignment_id"])
    if arrangement is None or not arrangement.scd_active_flag:
        raise NotFound("That consignment arrangement does not exist.")

    settlement = consignment.settle(
        db,
        arrangement,
        money_box_id=params.get("money_box_id"),
        note=params.get("note"),
        actor_user_id=actor_user_id,
    )
    return settlement.net_owed_amt
