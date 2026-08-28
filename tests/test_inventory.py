"""Inventory: costing, shipments, transfers, stock takes, write-offs (Part I §11).

The centrepiece is cost averaging. §11 defines item cost as a weighted average
over the units actually held, and every order line freezes that average at sale
— so if the blend is wrong, every margin figure the business ever reports is
wrong with it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, ValidationFailed
from app.db.base import utcnow
from app.models.enums import Currency, MovementKind, StockPoolKind, WriteOffReason
from app.models.inventory import (
    Shipment,
    StockLevel,
    StockMovement,
    StockPool,
    StockTake,
)
from app.services import barcodes, inventory
from app.services.pricing import q
from tests.test_checkout import _FakeRequest, _cart, db, store  # noqa: F401


@pytest.fixture
def warehouse(db: Session, store: dict) -> dict:
    """The seeded branch pool plus a second location to transfer to."""
    now = utcnow()
    central = StockPool(
        pool_kind=StockPoolKind.CENTRAL_STORAGE,
        name_ar="المستودع", name_en="Central",
        is_sellable_flag=True, is_owned_flag=True, scd_active_from=now,
    )
    db.add(central)
    db.commit()
    return {**store, "central": central}


def _variant_id(store: dict) -> int:
    return store["variant"].pk_product_variant_id


# ---------------------------------------------------------------------------
# Cost averaging (Part I §11)
# ---------------------------------------------------------------------------


def test_blend_from_empty_takes_the_incoming_cost():
    """Nothing on hand: there is no pool to blend into, and no division by zero."""
    assert inventory.blend_average_cost(
        current_quantity=0, current_average=Decimal("0"),
        incoming_quantity=10, incoming_unit_cost=Decimal("5.000"),
    ) == Decimal("5.000")


def test_blend_is_weighted_by_quantity():
    """10 @ 5 blended with 10 @ 7 is 6, not 6-ish."""
    assert inventory.blend_average_cost(
        current_quantity=10, current_average=Decimal("5.000"),
        incoming_quantity=10, incoming_unit_cost=Decimal("7.000"),
    ) == Decimal("6.000")


def test_blend_respects_unequal_quantities():
    """30 @ 2 with 10 @ 6 is 3.000 — weighted, not a naive midpoint of 4."""
    assert inventory.blend_average_cost(
        current_quantity=30, current_average=Decimal("2.000"),
        incoming_quantity=10, incoming_unit_cost=Decimal("6.000"),
    ) == Decimal("3.000")


def test_blend_at_zero_total_keeps_the_last_average():
    """§11: the last computed average survives as the next shipment's baseline."""
    assert inventory.blend_average_cost(
        current_quantity=0, current_average=Decimal("4.250"),
        incoming_quantity=0, incoming_unit_cost=Decimal("9.000"),
    ) == Decimal("4.250")


def test_usd_invoice_converts_to_jod():
    """Only JOD and USD are accepted for costing (Part I §1.1)."""
    assert inventory.to_jod(Decimal("14.100"), Currency.USD, Decimal("1.41")) == Decimal("10.000")
    assert inventory.to_jod(Decimal("10.000"), Currency.JOD, None) == Decimal("10.000")


def test_usd_invoice_without_a_rate_is_refused(db: Session):
    with pytest.raises(ValidationFailed):
        inventory.to_jod(Decimal("10.000"), Currency.USD, None)


def test_other_currencies_are_refused(db: Session):
    with pytest.raises(ValidationFailed):
        inventory.to_jod(Decimal("10.000"), "EUR", Decimal("1.0"))


# ---------------------------------------------------------------------------
# Receiving shipments
# ---------------------------------------------------------------------------


def test_shipment_raises_stock_and_reaverages_cost(db: Session, warehouse: dict):
    level = db.scalars(select(StockLevel)).one()
    assert (level.quantity_on_hand, level.average_cost_amt) == (3, Decimal("12.000"))

    inventory.create_shipment(
        db,
        reference="SHP-1",
        lines=[
            inventory.ShipmentLineInput(
                variant_id=_variant_id(warehouse),
                quantity=3,
                unit_cost_amt=Decimal("8.000"),
            )
        ],
        stock_pool_id=warehouse["pool"].pk_stock_pool_id,
        no_invoice_available=True,
    )
    db.commit()
    db.refresh(level)

    assert level.quantity_on_hand == 6
    # (3 x 12 + 3 x 8) / 6 = 10
    assert level.average_cost_amt == Decimal("10.000")


def test_shipment_spreads_landed_costs_by_value(db: Session, warehouse: dict):
    """Freight is apportioned by value, so a cheap item does not absorb the
    same freight as an expensive one."""
    inventory.create_shipment(
        db,
        reference="SHP-2",
        lines=[
            inventory.ShipmentLineInput(
                variant_id=_variant_id(warehouse), quantity=10,
                unit_cost_amt=Decimal("10.000"),
            )
        ],
        stock_pool_id=warehouse["pool"].pk_stock_pool_id,
        shipping_cost_amt=Decimal("20.000"),
        no_invoice_available=True,
    )
    db.commit()

    line = db.scalars(select(Shipment)).all()
    assert line  # shipment written
    # 100 JOD of goods + 20 freight over 10 units = 12.000 landed each.
    from app.models.inventory import ShipmentLine

    shipment_line = db.scalars(select(ShipmentLine)).one()
    assert shipment_line.unit_cost_jod_amt == Decimal("12.000")


def test_shipment_requires_an_invoice_or_an_explicit_none(db: Session, warehouse: dict):
    """§11 makes "no invoice available" a recorded state, not an omission."""
    with pytest.raises(ValidationFailed):
        inventory.create_shipment(
            db,
            reference="SHP-3",
            lines=[
                inventory.ShipmentLineInput(
                    variant_id=_variant_id(warehouse), quantity=1,
                    unit_cost_amt=Decimal("1.000"),
                )
            ],
            stock_pool_id=warehouse["pool"].pk_stock_pool_id,
        )


def test_shipment_line_can_override_the_invoice_pool(db: Session, warehouse: dict):
    """§11: the whole invoice, or each item individually."""
    inventory.create_shipment(
        db,
        reference="SHP-4",
        lines=[
            inventory.ShipmentLineInput(
                variant_id=_variant_id(warehouse), quantity=5,
                unit_cost_amt=Decimal("6.000"),
                stock_pool_id=warehouse["central"].pk_stock_pool_id,
            )
        ],
        stock_pool_id=warehouse["pool"].pk_stock_pool_id,
        no_invoice_available=True,
    )
    db.commit()

    central_level = db.scalars(
        select(StockLevel).where(
            StockLevel.fk_stock_pool_id == warehouse["central"].pk_stock_pool_id
        )
    ).one()
    assert central_level.quantity_on_hand == 5


def test_shipment_writes_a_ledger_row(db: Session, warehouse: dict):
    inventory.create_shipment(
        db, reference="SHP-5",
        lines=[
            inventory.ShipmentLineInput(
                variant_id=_variant_id(warehouse), quantity=4,
                unit_cost_amt=Decimal("9.000"),
            )
        ],
        stock_pool_id=warehouse["pool"].pk_stock_pool_id,
        no_invoice_available=True,
    )
    db.commit()

    movement = db.scalars(
        select(StockMovement).where(
            StockMovement.movement_kind == MovementKind.SHIPMENT_IN
        )
    ).one()
    assert movement.quantity_delta == 4
    assert movement.unit_cost_amt == Decimal("9.000")


# ---------------------------------------------------------------------------
# Transfers (Part I §11)
# ---------------------------------------------------------------------------


def test_transfer_removes_from_origin_on_dispatch(db: Session, warehouse: dict):
    transfer = inventory.create_transfer(
        db, reference="TRF-1",
        from_pool_id=warehouse["pool"].pk_stock_pool_id,
        to_pool_id=warehouse["central"].pk_stock_pool_id,
        lines=[(_variant_id(warehouse), 2)],
    )
    db.commit()

    origin = db.scalars(
        select(StockLevel).where(
            StockLevel.fk_stock_pool_id == warehouse["pool"].pk_stock_pool_id
        )
    ).one()
    assert origin.quantity_on_hand == 1, "units have left the origin"

    destination = db.scalars(
        select(StockLevel).where(
            StockLevel.fk_stock_pool_id == warehouse["central"].pk_stock_pool_id
        )
    ).first()
    assert destination is None, "and have not arrived yet — they are in transit"

    assert transfer.received_dt is None


def test_transfer_arrives_carrying_its_cost(db: Session, warehouse: dict):
    """Moving stock between branches must not change what it cost."""
    transfer = inventory.create_transfer(
        db, reference="TRF-2",
        from_pool_id=warehouse["pool"].pk_stock_pool_id,
        to_pool_id=warehouse["central"].pk_stock_pool_id,
        lines=[(_variant_id(warehouse), 2)],
    )
    db.commit()

    inventory.receive_transfer(db, transfer)
    db.commit()

    destination = db.scalars(
        select(StockLevel).where(
            StockLevel.fk_stock_pool_id == warehouse["central"].pk_stock_pool_id
        )
    ).one()
    assert destination.quantity_on_hand == 2
    assert destination.average_cost_amt == Decimal("12.000")


def test_transfer_refuses_to_move_more_than_is_sellable(db: Session, warehouse: dict):
    with pytest.raises(Conflict):
        inventory.create_transfer(
            db, reference="TRF-3",
            from_pool_id=warehouse["pool"].pk_stock_pool_id,
            to_pool_id=warehouse["central"].pk_stock_pool_id,
            lines=[(_variant_id(warehouse), 99)],
        )


def test_transfer_cannot_be_received_twice(db: Session, warehouse: dict):
    transfer = inventory.create_transfer(
        db, reference="TRF-4",
        from_pool_id=warehouse["pool"].pk_stock_pool_id,
        to_pool_id=warehouse["central"].pk_stock_pool_id,
        lines=[(_variant_id(warehouse), 1)],
    )
    inventory.receive_transfer(db, transfer)
    db.commit()

    with pytest.raises(Conflict):
        inventory.receive_transfer(db, transfer)


# ---------------------------------------------------------------------------
# Write-offs (Part I §11)
# ---------------------------------------------------------------------------


def test_write_off_reduces_stock_and_records_the_reason(db: Session, warehouse: dict):
    inventory.write_off(
        db,
        variant_id=_variant_id(warehouse),
        stock_pool_id=warehouse["pool"].pk_stock_pool_id,
        quantity=1,
        reason=WriteOffReason.DAMAGED,
        note="dropped in transit",
    )
    db.commit()

    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_on_hand == 2

    movement = db.scalars(
        select(StockMovement).where(
            StockMovement.movement_kind == MovementKind.WRITE_OFF
        )
    ).one()
    assert movement.quantity_delta == -1
    assert movement.write_off_reason == WriteOffReason.DAMAGED


def test_write_off_needs_a_valid_reason(db: Session, warehouse: dict):
    with pytest.raises(ValidationFailed):
        inventory.write_off(
            db, variant_id=_variant_id(warehouse),
            stock_pool_id=warehouse["pool"].pk_stock_pool_id,
            quantity=1, reason="just because",
        )


def test_write_off_cannot_exceed_sellable_stock(db: Session, warehouse: dict):
    with pytest.raises(Conflict):
        inventory.write_off(
            db, variant_id=_variant_id(warehouse),
            stock_pool_id=warehouse["pool"].pk_stock_pool_id,
            quantity=99, reason=WriteOffReason.LOST,
        )


# ---------------------------------------------------------------------------
# Stock takes (Part I §11)
# ---------------------------------------------------------------------------


def test_stock_take_freezes_the_system_quantity(db: Session, warehouse: dict):
    """Frozen so sales during the count cannot move the target."""
    stock_take = inventory.open_stock_take(
        db, reference="STK-1", stock_pool_id=warehouse["pool"].pk_stock_pool_id
    )
    db.commit()

    report = inventory.variance_report(db, stock_take)
    assert report[0]["line"].system_quantity == 3

    # Stock moves after the count opened.
    inventory.write_off(
        db, variant_id=_variant_id(warehouse),
        stock_pool_id=warehouse["pool"].pk_stock_pool_id,
        quantity=1, reason=WriteOffReason.LOST,
    )
    db.commit()

    report = inventory.variance_report(db, stock_take)
    assert report[0]["line"].system_quantity == 3, "the frozen figure does not move"


def test_open_count_does_not_touch_live_stock(db: Session, warehouse: dict):
    stock_take = inventory.open_stock_take(
        db, reference="STK-2", stock_pool_id=warehouse["pool"].pk_stock_pool_id
    )
    line = inventory.variance_report(db, stock_take)[0]["line"]
    inventory.record_count(db, stock_take, {line.pk_stock_take_line_id: 1})
    db.commit()

    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_on_hand == 3, "adjustments are only posted on close"


def test_closing_a_stock_take_posts_the_variance(db: Session, warehouse: dict):
    stock_take = inventory.open_stock_take(
        db, reference="STK-3", stock_pool_id=warehouse["pool"].pk_stock_pool_id
    )
    line = inventory.variance_report(db, stock_take)[0]["line"]
    inventory.record_count(db, stock_take, {line.pk_stock_take_line_id: 1})
    inventory.close_stock_take(db, stock_take)
    db.commit()

    level = db.scalars(select(StockLevel)).one()
    assert level.quantity_on_hand == 1, "system now agrees with the shelf"

    movement = db.scalars(
        select(StockMovement).where(
            StockMovement.movement_kind == MovementKind.STOCK_TAKE_ADJUSTMENT
        )
    ).one()
    assert movement.quantity_delta == -2


def test_a_closed_stock_take_cannot_be_reopened(db: Session, warehouse: dict):
    stock_take = inventory.open_stock_take(
        db, reference="STK-4", stock_pool_id=warehouse["pool"].pk_stock_pool_id
    )
    line = inventory.variance_report(db, stock_take)[0]["line"]
    inventory.record_count(db, stock_take, {line.pk_stock_take_line_id: 3})
    inventory.close_stock_take(db, stock_take)
    db.commit()

    with pytest.raises(Conflict):
        inventory.close_stock_take(db, stock_take)


# ---------------------------------------------------------------------------
# Ledger reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_reports_no_drift_after_normal_operations(
    db: Session, warehouse: dict
):
    """The projection must still agree with the ledger after real activity."""
    pool_id = warehouse["pool"].pk_stock_pool_id
    variant_id = _variant_id(warehouse)

    inventory.create_shipment(
        db, reference="SHP-R",
        lines=[
            inventory.ShipmentLineInput(
                variant_id=variant_id, quantity=5, unit_cost_amt=Decimal("7.000")
            )
        ],
        stock_pool_id=pool_id, no_invoice_available=True,
    )
    inventory.write_off(
        db, variant_id=variant_id, stock_pool_id=pool_id,
        quantity=2, reason=WriteOffReason.EXPIRED,
    )
    db.commit()

    # The fixture seeds 3 units without a movement, so that is the baseline.
    drifts = inventory.reconcile_stock(db, baseline_on_hand={(variant_id, pool_id): 3})
    assert drifts == []


def test_reconciliation_detects_injected_drift(db: Session, warehouse: dict):
    """A projection edited behind the ledger's back must be caught."""
    pool_id = warehouse["pool"].pk_stock_pool_id
    variant_id = _variant_id(warehouse)

    level = db.scalars(select(StockLevel)).one()
    level.quantity_on_hand += 7  # nothing wrote a movement for this
    db.commit()

    drifts = inventory.reconcile_stock(db, baseline_on_hand={(variant_id, pool_id): 3})
    assert len(drifts) == 1
    assert drifts[0].on_hand_drift == 7


# ---------------------------------------------------------------------------
# Positions and alerting
# ---------------------------------------------------------------------------


def test_low_stock_is_flagged_against_the_minimum(db: Session, warehouse: dict):
    warehouse["product"].min_stock_level = 5
    db.commit()

    rows = inventory.stock_positions(db)
    assert rows[0].is_low is True
    assert inventory.stock_positions(db, only_low=True)


def test_restock_suggestion_targets_the_optimal_level(db: Session, warehouse: dict):
    warehouse["product"].min_stock_level = 5
    warehouse["product"].optimal_stock_level = 20
    db.commit()

    row = inventory.stock_positions(db)[0]
    assert row.sellable == 3
    assert row.restock_to_optimal == 17


# ---------------------------------------------------------------------------
# Barcodes (Part I §11, §5.4)
# ---------------------------------------------------------------------------


def test_barcode_is_assigned_and_resolves_back(db: Session, warehouse: dict):
    variant = warehouse["variant"]
    value = barcodes.assign_barcode(db, variant)
    db.commit()

    assert value
    assert barcodes.resolve_scan(db, value).pk_product_variant_id == (
        variant.pk_product_variant_id
    )


def test_duplicate_barcodes_are_refused(db: Session, warehouse: dict):
    """A duplicate would resolve a scan to the wrong item."""
    from app.models.catalog import ProductVariant

    first = warehouse["variant"]
    barcodes.assign_barcode(db, first, "SHARED-1")
    second = ProductVariant(
        fk_product_id=first.fk_product_id, sku="SKU-OTHER", scd_active_from=utcnow()
    )
    db.add(second)
    db.commit()

    with pytest.raises(ValidationFailed):
        barcodes.assign_barcode(db, second, "SHARED-1")


def test_scanning_falls_back_to_sku(db: Session, warehouse: dict):
    """Staff type the SKU when a label is damaged; refusing is a dead end."""
    found = barcodes.resolve_scan(db, warehouse["variant"].sku)
    assert found.pk_product_variant_id == warehouse["variant"].pk_product_variant_id


def test_label_sheet_assigns_missing_barcodes(db: Session, warehouse: dict):
    labels = barcodes.labels_for_variants(db, [_variant_id(warehouse)], copies=3)
    db.commit()

    assert len(labels) == 3
    assert labels[0].svg.lstrip().startswith("<svg")
    assert warehouse["variant"].barcode is not None
