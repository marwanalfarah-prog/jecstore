"""Consignment workflows (Part I §7).

These tests cover the pieces that make consignment different from ordinary
owned stock: sellable-but-not-owned inbound goods, not-sellable-but-owned
outbound goods, configurable split maths, settlement signs, and customer return
reversals.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.models.consignment import Consignment, ConsignmentItem, ConsignmentSale
from app.models.enums import (
    ConsignmentDirection,
    ConsignmentItemState,
    ConsignmentSplitBasis,
    MovementKind,
    RefundDestination,
    WriteOffReason,
)
from app.models.inventory import StockLevel, StockMovement, StockPool
from app.services import consignment, inventory, money, orders, returns
from app.services.checkout import CheckoutRequest, place_order
from app.services.commerce import ShopperRef
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401
from tests.test_order_management import shop  # noqa: F401


def _arrangement(
    db: Session,
    *,
    direction: str = ConsignmentDirection.INBOUND,
    share: Decimal = Decimal("70"),
    basis: str = ConsignmentSplitBasis.DISCOUNTED_PRICE,
    reference: str = "CNS-TEST",
) -> Consignment:
    consignor = consignment.create_consignor(db, name=f"Consignor {reference}")
    return consignment.create_arrangement(
        db,
        reference=reference,
        consignor_id=consignor.pk_consignor_id,
        direction=direction,
        default_our_share_percentage=share,
        split_basis=basis,
    )


def _place_item(
    db: Session,
    arrangement: Consignment,
    variant_id: int,
    *,
    quantity: int = 1,
    share: Decimal | None = None,
) -> ConsignmentItem:
    return consignment.place_items(
        db,
        arrangement,
        variant_id=variant_id,
        quantity=quantity,
        our_share_percentage=share,
    )


def _level(db: Session, variant_id: int, pool_id: int) -> StockLevel:
    return db.scalars(
        select(StockLevel).where(
            StockLevel.fk_product_variant_id == variant_id,
            StockLevel.fk_stock_pool_id == pool_id,
            StockLevel.scd_active_flag.is_(True),
        )
    ).one()


def test_per_item_split_override_beats_arrangement_default(db: Session, store: dict):
    arrangement = _arrangement(db, share=Decimal("70"))
    item = _place_item(
        db,
        arrangement,
        store["variant"].pk_product_variant_id,
        share=Decimal("85"),
    )

    sale = consignment.record_sale(
        db,
        item,
        quantity=1,
        list_price_amt=Decimal("100.000"),
        sold_price_amt=Decimal("100.000"),
    )

    assert sale.our_share_percentage == Decimal("85.000")
    assert sale.our_share_amt == Decimal("85.000")
    assert sale.their_share_amt == Decimal("15.000")


def test_split_basis_changes_who_absorbs_a_discount(db: Session, store: dict):
    discounted = _arrangement(
        db,
        share=Decimal("20"),
        basis=ConsignmentSplitBasis.DISCOUNTED_PRICE,
        reference="CNS-DISC",
    )
    original = _arrangement(
        db,
        share=Decimal("20"),
        basis=ConsignmentSplitBasis.ORIGINAL_PRICE,
        reference="CNS-ORIG",
    )

    discount_item = _place_item(db, discounted, store["variant"].pk_product_variant_id)
    original_item = _place_item(db, original, store["variant"].pk_product_variant_id)

    discount_sale = consignment.record_sale(
        db,
        discount_item,
        quantity=1,
        list_price_amt=Decimal("100.000"),
        sold_price_amt=Decimal("80.000"),
    )
    original_sale = consignment.record_sale(
        db,
        original_item,
        quantity=1,
        list_price_amt=Decimal("100.000"),
        sold_price_amt=Decimal("80.000"),
    )

    assert discount_sale.their_share_amt == Decimal("64.000")
    assert original_sale.their_share_amt == Decimal("80.000")
    assert original_sale.our_share_amt == Decimal("0.000")
    assert discount_sale.their_share_amt != original_sale.their_share_amt


def test_inbound_stock_is_sellable_but_not_owned(db: Session, store: dict):
    arrangement = _arrangement(db, direction=ConsignmentDirection.INBOUND)
    _place_item(db, arrangement, store["variant"].pk_product_variant_id, quantity=5)

    pool = db.get(StockPool, arrangement.fk_stock_pool_id)
    assert pool.is_sellable_flag is True
    assert pool.is_owned_flag is False

    row = next(
        row
        for row in inventory.stock_positions(db)
        if row.variant.pk_product_variant_id == store["variant"].pk_product_variant_id
    )
    assert row.on_hand == 3
    assert row.average_cost_amt == Decimal("12.000")


def test_outbound_leaves_sellable_pool_but_stays_owned(db: Session, store: dict):
    arrangement = _arrangement(
        db, direction=ConsignmentDirection.OUTBOUND, reference="CNS-OUT"
    )
    _place_item(db, arrangement, store["variant"].pk_product_variant_id, quantity=2)

    pool = db.get(StockPool, arrangement.fk_stock_pool_id)
    assert pool.is_sellable_flag is False
    assert pool.is_owned_flag is True

    branch = _level(
        db, store["variant"].pk_product_variant_id, store["pool"].pk_stock_pool_id
    )
    consigned_out = _level(
        db, store["variant"].pk_product_variant_id, arrangement.fk_stock_pool_id
    )
    assert branch.quantity_sellable == 1
    assert consigned_out.quantity_on_hand == 2

    row = next(
        row
        for row in inventory.stock_positions(db)
        if row.variant.pk_product_variant_id == store["variant"].pk_product_variant_id
    )
    assert row.on_hand == 3


def test_partial_recall_keeps_item_held_and_reduces_outstanding(db: Session, store: dict):
    arrangement = _arrangement(db)
    item = _place_item(db, arrangement, store["variant"].pk_product_variant_id, quantity=5)

    consignment.return_to_consignor(db, item, quantity=2)

    assert item.quantity_returned == 2
    assert item.state == ConsignmentItemState.HELD
    assert item.quantity_outstanding == 3
    assert _level(
        db, store["variant"].pk_product_variant_id, arrangement.fk_stock_pool_id
    ).quantity_on_hand == 3


def test_damage_in_custody_writes_costed_write_off(db: Session, store: dict):
    arrangement = _arrangement(
        db, direction=ConsignmentDirection.OUTBOUND, reference="CNS-DMG"
    )
    item = _place_item(db, arrangement, store["variant"].pk_product_variant_id, quantity=2)

    consignment.mark_damaged_or_lost(
        db, item, quantity=1, reason=WriteOffReason.DAMAGED
    )

    assert item.quantity_damaged_or_lost == 1
    assert _level(
        db, store["variant"].pk_product_variant_id, arrangement.fk_stock_pool_id
    ).quantity_on_hand == 1

    movement = db.scalars(
        select(StockMovement).where(
            StockMovement.fk_consignment_id == arrangement.pk_consignment_id,
            StockMovement.movement_kind == MovementKind.WRITE_OFF,
        )
    ).one()
    assert movement.quantity_delta == -1
    assert movement.unit_cost_amt == Decimal("12.000")


def test_settlement_signs_and_money_box_movements(db: Session, shop: dict):
    inbound = _arrangement(
        db,
        direction=ConsignmentDirection.INBOUND,
        share=Decimal("10"),
        reference="CNS-IN",
    )
    outbound = _arrangement(
        db,
        direction=ConsignmentDirection.OUTBOUND,
        share=Decimal("80"),
        reference="CNS-OUT",
    )
    inbound_item = _place_item(db, inbound, shop["variant"].pk_product_variant_id)
    outbound_item = _place_item(db, outbound, shop["variant"].pk_product_variant_id)

    consignment.record_sale(
        db,
        inbound_item,
        quantity=1,
        list_price_amt=Decimal("100.000"),
        sold_price_amt=Decimal("100.000"),
    )
    consignment.record_sale(
        db,
        outbound_item,
        quantity=1,
        list_price_amt=Decimal("100.000"),
        sold_price_amt=Decimal("100.000"),
    )

    inbound_position = consignment.settlement_position(db, inbound)
    outbound_position = consignment.settlement_position(db, outbound)
    assert inbound_position.net_owed_amt > 0
    assert inbound_position.we_owe is True
    assert outbound_position.net_owed_amt < 0
    assert outbound_position.we_owe is False

    consignment.settle(
        db, inbound, money_box_id=shop["box"].pk_money_box_id
    )
    assert money.box_balance(db, shop["box"].pk_money_box_id) == Decimal("-90.000")

    consignment.settle(
        db, outbound, money_box_id=shop["box"].pk_money_box_id
    )
    assert money.box_balance(db, shop["box"].pk_money_box_id) == Decimal("-10.000")


def test_double_settlement_is_refused(db: Session, shop: dict):
    arrangement = _arrangement(db, share=Decimal("50"))
    item = _place_item(db, arrangement, shop["variant"].pk_product_variant_id)
    sale = consignment.record_sale(
        db,
        item,
        quantity=1,
        list_price_amt=Decimal("20.000"),
        sold_price_amt=Decimal("20.000"),
    )

    settlement = consignment.settle(
        db, arrangement, money_box_id=shop["box"].pk_money_box_id
    )

    db.refresh(sale)
    assert sale.fk_consignment_settlement_id == settlement.pk_consignment_settlement_id
    with pytest.raises(Conflict, match="nothing outstanding"):
        consignment.settle(
            db, arrangement, money_box_id=shop["box"].pk_money_box_id
        )


def test_customer_return_unwinds_consignment_split(db: Session, shop: dict):
    owned_level = _level(
        db, shop["variant"].pk_product_variant_id, shop["pool"].pk_stock_pool_id
    )
    owned_level.quantity_on_hand = 0

    arrangement = _arrangement(db, share=Decimal("70"), reference="CNS-RETURN")
    item = _place_item(db, arrangement, shop["variant"].pk_product_variant_id)

    _cart(db, shop, user=shop["user"], quantity=1)
    order = place_order(
        db,
        _FakeRequest(),
        ShopperRef(user_id=shop["user"].pk_user_id, session_key=None),
        CheckoutRequest(),
    )
    db.commit()

    line = orders.active_lines(db, order)[0]
    assert line.fk_consignment_item_id == item.pk_consignment_item_id

    money.record_order_payment(
        db,
        order,
        [
            money.Split(
                channel_id=shop["cash"].pk_payment_channel_id,
                amount_amt=order.total_amt,
                money_box_id=shop["box"].pk_money_box_id,
            )
        ],
    )
    orders.mark_delivered(db, order, shop["user"])
    db.commit()

    assert consignment.settlement_position(db, arrangement).net_owed_amt > 0

    order_return = returns.request_return(
        db,
        order,
        [
            returns.ReturnLineRequest(
                order_line_id=line.pk_order_line_id, quantity=1
            )
        ],
        reason_code="changed_mind",
    )
    returns.record_inspection(db, order_return, shop["user"], condition_acceptable=True)
    returns.finalise_refund(
        db,
        order_return,
        destination=RefundDestination.MONEY_BOX,
        money_box_id=shop["box"].pk_money_box_id,
    )
    db.commit()

    sales = db.scalars(
        select(ConsignmentSale)
        .where(ConsignmentSale.fk_consignment_item_id == item.pk_consignment_item_id)
        .order_by(ConsignmentSale.pk_consignment_sale_id)
    ).all()
    assert [sale.quantity for sale in sales] == [1, -1]
    assert consignment.settlement_position(db, arrangement).net_owed_amt == Decimal("0.000")


def test_close_arrangement_refuses_held_units_or_unsettled_sales(
    db: Session, shop: dict
):
    held_arrangement = _arrangement(db, reference="CNS-HELD")
    _place_item(db, held_arrangement, shop["variant"].pk_product_variant_id)
    with pytest.raises(Conflict, match="remaining units"):
        consignment.close_arrangement(db, held_arrangement)

    sale_arrangement = _arrangement(db, reference="CNS-SALE")
    item = _place_item(db, sale_arrangement, shop["variant"].pk_product_variant_id)
    consignment.record_sale(
        db,
        item,
        quantity=1,
        list_price_amt=Decimal("20.000"),
        sold_price_amt=Decimal("20.000"),
    )
    with pytest.raises(Conflict, match="outstanding sales"):
        consignment.close_arrangement(db, sale_arrangement)
