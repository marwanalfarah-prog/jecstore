"""Inventory: shipments, costing, transfers, stock takes, write-offs (Part I §11).

**Cost averaging is the heart of this module.** §11 defines item cost as

    (cost of all historical + currently available units − cost of already-sold units)
    ÷ (number of currently available units)

which is a running weighted average: each shipment-in blends its unit cost into
the pool at the quantity received, and each sale removes units at the average
that applied when they left. That is why ``TRX_STOCK_MOVEMENT.unit_cost_amt`` is
frozen on every movement, and why order lines freeze ``unit_cost_amt`` at sale
(§11) — margin history must not shift when a later shipment arrives at a
different price.

The division-by-zero case §11 raises never reaches the customer: an item at zero
stock is not available at all (§5.4). Here it is handled explicitly anyway — the
last computed average survives as the baseline for the next shipment, which is
exactly what §11 says to do.

Everything that moves stock writes to the insert-only ledger, and
:func:`reconcile_stock` proves the ``SCD_STOCK_LEVEL`` projection still agrees
with it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import Product, ProductVariant
from app.models.enums import (
    ON_HAND_MOVEMENT_KINDS,
    RESERVATION_MOVEMENT_KINDS,
    Currency,
    EmailTemplateCode,
    MovementKind,
    StockPoolKind,
    WriteOffReason,
)
from app.models.identity import User
from app.models.inventory import (
    Shipment,
    ShipmentLine,
    StockLevel,
    StockMovement,
    StockPool,
    StockTake,
    StockTakeLine,
    StockTransfer,
    StockTransferLine,
)
from app.services import locking
from app.services.pricing import q

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Cost averaging (Part I §11)
# ---------------------------------------------------------------------------


def blend_average_cost(
    *,
    current_quantity: int,
    current_average: Decimal,
    incoming_quantity: int,
    incoming_unit_cost: Decimal,
) -> Decimal:
    """Weighted-average cost after receiving ``incoming_quantity`` units.

    The zero-stock case is the one §11 calls out: with nothing on hand there is
    no pool to blend into, so the incoming cost simply becomes the new average
    rather than dividing by zero.
    """
    current_quantity = max(current_quantity, 0)
    incoming_quantity = max(incoming_quantity, 0)
    total = current_quantity + incoming_quantity

    if total == 0:
        # Nothing in, nothing held: keep the last known average as the baseline
        # for the next shipment (§11).
        return q(Decimal(current_average))

    if current_quantity == 0:
        return q(Decimal(incoming_unit_cost))

    blended = (
        Decimal(current_quantity) * Decimal(current_average)
        + Decimal(incoming_quantity) * Decimal(incoming_unit_cost)
    ) / Decimal(total)
    return q(blended)


def to_jod(amount: Decimal, currency: str, usd_rate: Decimal | None) -> Decimal:
    """Convert an invoice amount to stored JOD.

    Only JOD and USD are accepted for shipment costing (§1.1, §11) — no other
    currency conversion is needed, and accepting one silently would put the
    cost base wrong.
    """
    if currency == Currency.JOD:
        return q(Decimal(amount))
    if currency == Currency.USD:
        if not usd_rate or Decimal(usd_rate) <= 0:
            raise ValidationFailed("A USD invoice needs the rate used at the time.")
        return q(Decimal(amount) / Decimal(usd_rate))
    raise ValidationFailed("Shipments may only be costed in JOD or USD.")


# ---------------------------------------------------------------------------
# Receiving a shipment (Part I §11)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ShipmentLineInput:
    variant_id: int
    quantity: int
    unit_cost_amt: Decimal
    #: Per-line pool override. §11 lets Admin assign the whole invoice to one
    #: pool, or each item individually — this is the "individually" half.
    stock_pool_id: int | None = None


def create_shipment(
    db: Session,
    *,
    reference: str,
    lines: list[ShipmentLineInput],
    supplier_name: str | None = None,
    invoice_date: dt.date | None = None,
    currency: str = Currency.JOD,
    usd_rate_used: Decimal | None = None,
    stock_pool_id: int | None = None,
    invoice_file_path: str | None = None,
    no_invoice_available: bool = False,
    shipping_cost_amt: Decimal = Decimal("0"),
    customs_cost_amt: Decimal = Decimal("0"),
    note: str | None = None,
    actor_user_id: int | None = None,
) -> Shipment:
    """Record an incoming invoice, update stock, and re-average cost.

    Landed costs (shipping, customs) are spread across the received units in
    proportion to their value, because a book that cost 2 JOD should not absorb
    the same freight as one that cost 40.
    """
    if not lines:
        raise ValidationFailed("A shipment needs at least one line.")
    if not invoice_file_path and not no_invoice_available:
        # §11 makes "no invoice available" an explicit recorded state rather
        # than an omission, so one of the two must be true.
        raise ValidationFailed(
            "Attach the invoice, or mark it as 'no invoice available'."
        )

    now = utcnow()
    shipment = Shipment(
        reference=reference,
        supplier_name=supplier_name,
        invoice_date=invoice_date,
        currency=currency,
        usd_rate_used=usd_rate_used,
        fk_stock_pool_id=stock_pool_id,
        invoice_file_path=invoice_file_path,
        no_invoice_available_flag=no_invoice_available,
        shipping_cost_amt=q(shipping_cost_amt),
        customs_cost_amt=q(customs_cost_amt),
        note=note,
        received_dt=now,
        scd_active_from=now,
    )
    db.add(shipment)
    db.flush()

    goods_value = sum(
        (to_jod(line.unit_cost_amt, currency, usd_rate_used) * line.quantity for line in lines),
        Decimal("0"),
    )
    landed = q(Decimal(shipping_cost_amt) + Decimal(customs_cost_amt))

    variant_ids = [line.variant_id for line in lines]
    levels = {
        (level.fk_product_variant_id, level.fk_stock_pool_id): level
        for level in locking.lock_stock_levels(db, variant_ids)
    }

    for line in lines:
        if line.quantity <= 0:
            raise ValidationFailed("Shipment quantities must be positive.")

        pool_id = line.stock_pool_id or stock_pool_id
        if pool_id is None:
            raise ValidationFailed(
                "Choose a stock pool for the invoice or for each item."
            )

        unit_jod = to_jod(line.unit_cost_amt, currency, usd_rate_used)

        # Spread landed costs by value share.
        if goods_value > 0 and landed > 0:
            share = (unit_jod * line.quantity) / goods_value
            unit_jod = q(unit_jod + (landed * share) / Decimal(line.quantity))

        db.add(
            ShipmentLine(
                fk_shipment_id=shipment.pk_shipment_id,
                fk_product_variant_id=line.variant_id,
                fk_stock_pool_id=pool_id,
                quantity=line.quantity,
                unit_cost_amt=q(line.unit_cost_amt),
                unit_cost_jod_amt=unit_jod,
                scd_active_from=now,
            )
        )

        level = levels.get((line.variant_id, pool_id)) or _ensure_level(
            db, line.variant_id, pool_id, now
        )
        level.average_cost_amt = blend_average_cost(
            current_quantity=level.quantity_on_hand,
            current_average=Decimal(level.average_cost_amt or 0),
            incoming_quantity=line.quantity,
            incoming_unit_cost=unit_jod,
        )
        level.quantity_on_hand += line.quantity
        level.last_movement_dt = now
        levels[(line.variant_id, pool_id)] = level

        db.add(
            StockMovement(
                fk_product_variant_id=line.variant_id,
                fk_stock_pool_id=pool_id,
                movement_kind=MovementKind.SHIPMENT_IN,
                quantity_delta=line.quantity,
                unit_cost_amt=unit_jod,
                fk_shipment_id=shipment.pk_shipment_id,
                note=f"Shipment {reference}",
                created_dt=now,
                created_by=actor_user_id,
            )
        )

    log.info(
        "shipment_received",
        extra={"reference": reference, "lines": len(lines), "currency": currency},
    )
    return shipment


def _ensure_level(
    db: Session, variant_id: int, pool_id: int, now: dt.datetime
) -> StockLevel:
    """Get or create the stock row for a variant in a pool."""
    existing = db.scalars(
        select(StockLevel).where(
            StockLevel.fk_product_variant_id == variant_id,
            StockLevel.fk_stock_pool_id == pool_id,
            StockLevel.scd_active_flag.is_(True),
        )
    ).first()
    if existing is not None:
        return existing

    level = StockLevel(
        fk_product_variant_id=variant_id,
        fk_stock_pool_id=pool_id,
        quantity_on_hand=0,
        quantity_reserved=0,
        average_cost_amt=Decimal("0"),
        last_movement_dt=now,
        scd_active_from=now,
    )
    db.add(level)
    db.flush()
    return level


# ---------------------------------------------------------------------------
# Transfers between branches (Part I §11)
# ---------------------------------------------------------------------------


def create_transfer(
    db: Session,
    *,
    reference: str,
    from_pool_id: int,
    to_pool_id: int,
    lines: list[tuple[int, int]],
    note: str | None = None,
    actor_user_id: int | None = None,
) -> StockTransfer:
    """Dispatch stock from one pool to another.

    Its own movement type — neither a sale nor a shipment-in (§11). Units leave
    the origin immediately and are *in transit*: they only arrive at the
    destination when :func:`receive_transfer` is called, so stock in a van is
    never double-counted as available at both ends.
    """
    if from_pool_id == to_pool_id:
        raise ValidationFailed("Choose two different locations.")
    if not lines:
        raise ValidationFailed("A transfer needs at least one item.")

    now = utcnow()
    transfer = StockTransfer(
        reference=reference,
        fk_from_stock_pool_id=from_pool_id,
        fk_to_stock_pool_id=to_pool_id,
        dispatched_dt=now,
        note=note,
        scd_active_from=now,
    )
    db.add(transfer)
    db.flush()

    levels = {
        (lvl.fk_product_variant_id, lvl.fk_stock_pool_id): lvl
        for lvl in locking.lock_stock_levels(db, [v for v, _ in lines])
    }

    for variant_id, quantity in lines:
        if quantity <= 0:
            raise ValidationFailed("Transfer quantities must be positive.")

        source = levels.get((variant_id, from_pool_id))
        if source is None or source.quantity_sellable < quantity:
            raise Conflict(
                "Not enough sellable stock at the origin to transfer.",
                details={"variant_id": variant_id, "requested": quantity},
            )

        source.quantity_on_hand -= quantity
        source.last_movement_dt = now

        db.add(
            StockTransferLine(
                fk_stock_transfer_id=transfer.pk_stock_transfer_id,
                fk_product_variant_id=variant_id,
                quantity=quantity,
                scd_active_from=now,
            )
        )
        db.add(
            StockMovement(
                fk_product_variant_id=variant_id,
                fk_stock_pool_id=from_pool_id,
                movement_kind=MovementKind.TRANSFER_OUT,
                quantity_delta=-quantity,
                unit_cost_amt=source.average_cost_amt,
                fk_stock_transfer_id=transfer.pk_stock_transfer_id,
                note=f"Transfer {reference}",
                created_dt=now,
                created_by=actor_user_id,
            )
        )

    log.info("transfer_dispatched", extra={"reference": reference})
    return transfer


def receive_transfer(
    db: Session, transfer: StockTransfer, *, actor_user_id: int | None = None
) -> StockTransfer:
    """Book stock in at the destination, carrying its cost with it.

    Cost travels with the goods: moving stock between branches must not change
    what it cost the business.
    """
    if transfer.received_dt is not None:
        raise Conflict("This transfer has already been received.")

    now = utcnow()
    lines = db.scalars(
        select(StockTransferLine).where(
            StockTransferLine.fk_stock_transfer_id == transfer.pk_stock_transfer_id,
            StockTransferLine.scd_active_flag.is_(True),
        )
    ).all()

    for line in lines:
        received = line.quantity_received if line.quantity_received is not None else line.quantity
        if received <= 0:
            continue

        source = db.scalars(
            select(StockLevel).where(
                StockLevel.fk_product_variant_id == line.fk_product_variant_id,
                StockLevel.fk_stock_pool_id == transfer.fk_from_stock_pool_id,
                StockLevel.scd_active_flag.is_(True),
            )
        ).first()
        unit_cost = Decimal(source.average_cost_amt or 0) if source else Decimal("0")

        destination = _ensure_level(
            db, line.fk_product_variant_id, transfer.fk_to_stock_pool_id, now
        )
        destination.average_cost_amt = blend_average_cost(
            current_quantity=destination.quantity_on_hand,
            current_average=Decimal(destination.average_cost_amt or 0),
            incoming_quantity=received,
            incoming_unit_cost=unit_cost,
        )
        destination.quantity_on_hand += received
        destination.last_movement_dt = now
        line.quantity_received = received

        db.add(
            StockMovement(
                fk_product_variant_id=line.fk_product_variant_id,
                fk_stock_pool_id=transfer.fk_to_stock_pool_id,
                movement_kind=MovementKind.TRANSFER_IN,
                quantity_delta=received,
                unit_cost_amt=unit_cost,
                fk_stock_transfer_id=transfer.pk_stock_transfer_id,
                note=f"Transfer {transfer.reference}",
                created_dt=now,
                created_by=actor_user_id,
            )
        )

    transfer.received_dt = now
    log.info("transfer_received", extra={"reference": transfer.reference})
    return transfer


# ---------------------------------------------------------------------------
# Write-offs (Part I §11)
# ---------------------------------------------------------------------------


def write_off(
    db: Session,
    *,
    variant_id: int,
    stock_pool_id: int,
    quantity: int,
    reason: str,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> StockMovement:
    """Shrinkage: damaged, lost or expired stock.

    Its own movement type so this stock has a defined home rather than nowhere
    to go (§11), and so it never silently disappears from a variance report.
    """
    if quantity <= 0:
        raise ValidationFailed("Write-off quantity must be positive.")
    if reason not in set(WriteOffReason):
        raise ValidationFailed("Choose a write-off reason.")

    levels = locking.lock_stock_levels(db, [variant_id])
    level = next(
        (l for l in levels if l.fk_stock_pool_id == stock_pool_id), None
    )
    if level is None or level.quantity_sellable < quantity:
        raise Conflict("Not enough sellable stock to write off that quantity.")

    now = utcnow()
    level.quantity_on_hand -= quantity
    level.last_movement_dt = now

    movement = StockMovement(
        fk_product_variant_id=variant_id,
        fk_stock_pool_id=stock_pool_id,
        movement_kind=MovementKind.WRITE_OFF,
        quantity_delta=-quantity,
        unit_cost_amt=level.average_cost_amt,
        write_off_reason=reason,
        note=note,
        created_dt=now,
        created_by=actor_user_id,
    )
    db.add(movement)

    log.info(
        "stock_written_off",
        extra={"variant": variant_id, "quantity": quantity, "reason": reason},
    )
    return movement


# ---------------------------------------------------------------------------
# Stock takes (Part I §11)
# ---------------------------------------------------------------------------


def open_stock_take(
    db: Session,
    *,
    reference: str,
    stock_pool_id: int,
    variant_ids: list[int] | None = None,
    note: str | None = None,
) -> StockTake:
    """Start a physical count, freezing the system quantity per line.

    Frozen on purpose: sales during the count would otherwise move the target,
    and a variance you cannot reproduce is not a finding.
    """
    now = utcnow()
    stock_take = StockTake(
        reference=reference,
        fk_stock_pool_id=stock_pool_id,
        started_dt=now,
        note=note,
        scd_active_from=now,
    )
    db.add(stock_take)
    db.flush()

    stmt = select(StockLevel).where(
        StockLevel.fk_stock_pool_id == stock_pool_id,
        StockLevel.scd_active_flag.is_(True),
    )
    if variant_ids:
        stmt = stmt.where(StockLevel.fk_product_variant_id.in_(variant_ids))

    for level in db.scalars(stmt).all():
        db.add(
            StockTakeLine(
                fk_stock_take_id=stock_take.pk_stock_take_id,
                fk_product_variant_id=level.fk_product_variant_id,
                system_quantity=level.quantity_on_hand,
                scd_active_from=now,
            )
        )

    # Flush so the count sheet is queryable immediately, without the caller
    # having to commit first. Sessions here run with autoflush off, so a
    # service that leaves rows pending is a service whose results vanish.
    db.flush()

    log.info("stock_take_opened", extra={"reference": reference})
    return stock_take


def record_count(
    db: Session, stock_take: StockTake, counts: dict[int, int]
) -> StockTake:
    """Enter counted quantities. ``counts`` maps stock-take-line id → count."""
    if stock_take.completed_dt is not None:
        raise Conflict("This stock take is already closed.")

    for line in _take_lines(db, stock_take):
        if line.pk_stock_take_line_id in counts:
            counted = counts[line.pk_stock_take_line_id]
            if counted < 0:
                raise ValidationFailed("A counted quantity cannot be negative.")
            line.counted_quantity = counted
    return stock_take


def close_stock_take(
    db: Session, stock_take: StockTake, *, actor_user_id: int | None = None
) -> StockTake:
    """Close the count and write adjustment movements for every variance.

    Adjustments are only written here — a half-finished count never touches live
    stock (§11).
    """
    if stock_take.completed_dt is not None:
        raise Conflict("This stock take is already closed.")

    now = utcnow()
    lines = _take_lines(db, stock_take)
    counted_lines = [line for line in lines if line.counted_quantity is not None]
    if not counted_lines:
        raise ValidationFailed("Record at least one count before closing.")

    levels = {
        level.fk_product_variant_id: level
        for level in locking.lock_stock_levels(
            db, [line.fk_product_variant_id for line in counted_lines]
        )
        if level.fk_stock_pool_id == stock_take.fk_stock_pool_id
    }

    for line in counted_lines:
        variance = line.variance
        if not variance:
            continue

        level = levels.get(line.fk_product_variant_id)
        if level is None:
            continue

        level.quantity_on_hand += variance
        level.last_movement_dt = now

        db.add(
            StockMovement(
                fk_product_variant_id=line.fk_product_variant_id,
                fk_stock_pool_id=stock_take.fk_stock_pool_id,
                movement_kind=MovementKind.STOCK_TAKE_ADJUSTMENT,
                quantity_delta=variance,
                unit_cost_amt=level.average_cost_amt,
                fk_stock_take_id=stock_take.pk_stock_take_id,
                note=f"Stock take {stock_take.reference}",
                created_dt=now,
                created_by=actor_user_id,
            )
        )

    stock_take.completed_dt = now
    log.info(
        "stock_take_closed",
        extra={"reference": stock_take.reference, "lines": len(counted_lines)},
    )
    return stock_take


def variance_report(db: Session, stock_take: StockTake) -> list[dict]:
    """Counted vs. system, per line — the report §11 asks for."""
    report = []
    for line in _take_lines(db, stock_take):
        variant = db.get(ProductVariant, line.fk_product_variant_id)
        product = db.get(Product, variant.fk_product_id) if variant else None
        report.append(
            {
                "line": line,
                "variant": variant,
                "product": product,
                "variance": line.variance,
            }
        )
    return report


def _take_lines(db: Session, stock_take: StockTake) -> list[StockTakeLine]:
    return list(
        db.scalars(
            select(StockTakeLine)
            .where(
                StockTakeLine.fk_stock_take_id == stock_take.pk_stock_take_id,
                StockTakeLine.scd_active_flag.is_(True),
            )
            .order_by(StockTakeLine.pk_stock_take_line_id)
        ).all()
    )


# ---------------------------------------------------------------------------
# Reconciliation: ledger vs. projection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Drift:
    variant_id: int
    stock_pool_id: int
    projected_on_hand: int
    ledger_on_hand: int
    projected_reserved: int
    ledger_reserved: int

    @property
    def on_hand_drift(self) -> int:
        return self.projected_on_hand - self.ledger_on_hand

    @property
    def reserved_drift(self) -> int:
        return self.projected_reserved - self.ledger_reserved


def reconcile_stock(db: Session, *, baseline_on_hand: dict | None = None) -> list[Drift]:
    """Prove ``SCD_STOCK_LEVEL`` still agrees with ``TRX_STOCK_MOVEMENT``.

    The projection exists because checkout needs a lockable row (see
    ``app/services/locking.py``); this is what keeps that redundancy honest
    rather than letting it become a second, competing truth.

    ``baseline_on_hand`` accounts for stock seeded without a movement (a data
    migration, or a test fixture). Anything returned by this function is real
    drift and should be investigated.
    """
    baseline_on_hand = baseline_on_hand or {}

    def _sum_by_kind(kinds) -> dict[tuple[int, int], int]:
        """SUM the ledger for one movement-kind set, grouped by variant+pool.

        Two straightforward grouped queries rather than one clever conditional
        aggregate: each projection is summed from its own slice of the ledger,
        which is exactly how the two movement-kind sets are defined.
        """
        return {
            (variant_id, pool_id): int(total or 0)
            for variant_id, pool_id, total in db.execute(
                select(
                    StockMovement.fk_product_variant_id,
                    StockMovement.fk_stock_pool_id,
                    func.coalesce(func.sum(StockMovement.quantity_delta), 0),
                )
                .where(StockMovement.movement_kind.in_(kinds))
                .group_by(
                    StockMovement.fk_product_variant_id,
                    StockMovement.fk_stock_pool_id,
                )
            ).all()
        }

    on_hand_ledger = _sum_by_kind(ON_HAND_MOVEMENT_KINDS)
    reserved_ledger = _sum_by_kind(RESERVATION_MOVEMENT_KINDS)

    drifts: list[Drift] = []
    for level in db.scalars(
        select(StockLevel).where(StockLevel.scd_active_flag.is_(True))
    ).all():
        key = (level.fk_product_variant_id, level.fk_stock_pool_id)
        ledger_on_hand = on_hand_ledger.get(key, 0) + baseline_on_hand.get(key, 0)
        ledger_reserved = reserved_ledger.get(key, 0)

        drift = Drift(
            variant_id=level.fk_product_variant_id,
            stock_pool_id=level.fk_stock_pool_id,
            projected_on_hand=level.quantity_on_hand,
            ledger_on_hand=int(ledger_on_hand),
            projected_reserved=level.quantity_reserved,
            ledger_reserved=int(ledger_reserved),
        )
        if drift.on_hand_drift or drift.reserved_drift:
            drifts.append(drift)

    if drifts:
        log.warning("stock_ledger_drift", extra={"rows": len(drifts)})
    return drifts


# ---------------------------------------------------------------------------
# Queries and alerting (Part I §11)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StockRow:
    """One variant's position, for the inventory list."""

    variant: ProductVariant
    product: Product
    on_hand: int
    reserved: int
    average_cost_amt: Decimal
    minimum: int | None
    optimal: int | None

    @property
    def sellable(self) -> int:
        return max(self.on_hand - self.reserved, 0)

    @property
    def is_low(self) -> bool:
        return self.minimum is not None and self.sellable <= self.minimum

    @property
    def is_out(self) -> bool:
        return self.sellable <= 0

    @property
    def restock_to_optimal(self) -> int:
        """How many to order to reach the optimal level."""
        if self.optimal is None:
            return 0
        return max(self.optimal - self.sellable, 0)


def stock_positions(
    db: Session, *, only_low: bool = False, query: str | None = None, limit: int = 200
) -> list[StockRow]:
    """Current owned stock across all pools, aggregated per variant."""
    owned_pool_ids = select(StockPool.pk_stock_pool_id).where(
        StockPool.scd_active_flag.is_(True),
        StockPool.is_owned_flag.is_(True),
    )
    # Inbound consignment is sellable but not owned, so it must stay out of
    # cost and owned-stock reporting (Part I §7, §11).
    owned_stock_join = (
        (StockLevel.fk_product_variant_id == ProductVariant.pk_product_variant_id)
        & (StockLevel.scd_active_flag.is_(True))
        & (StockLevel.fk_stock_pool_id.in_(owned_pool_ids))
    )
    stmt = (
        select(
            ProductVariant,
            Product,
            func.coalesce(func.sum(StockLevel.quantity_on_hand), 0),
            func.coalesce(func.sum(StockLevel.quantity_reserved), 0),
            func.coalesce(func.avg(StockLevel.average_cost_amt), 0),
        )
        .join(Product, Product.pk_product_id == ProductVariant.fk_product_id)
        .outerjoin(StockLevel, owned_stock_join)
        .where(
            ProductVariant.scd_active_flag.is_(True),
            Product.scd_active_flag.is_(True),
        )
        .group_by(ProductVariant.pk_product_variant_id, Product.pk_product_id)
        .order_by(Product.name_en)
        .limit(limit)
    )
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(
            Product.name_en.ilike(like)
            | Product.name_ar.ilike(like)
            | ProductVariant.sku.ilike(like)
        )

    rows = [
        StockRow(
            variant=variant,
            product=product,
            on_hand=int(on_hand),
            reserved=int(reserved),
            average_cost_amt=q(Decimal(avg_cost or 0)),
            minimum=variant.min_stock_level or product.min_stock_level,
            optimal=variant.optimal_stock_level or product.optimal_stock_level,
        )
        for variant, product, on_hand, reserved, avg_cost in db.execute(stmt).all()
    ]

    return [row for row in rows if row.is_low] if only_low else rows


def movement_history(
    db: Session, variant_id: int, *, limit: int = 200
) -> list[StockMovement]:
    """Every in/out movement for one item — the per-item dashboard in §11."""
    return list(
        db.scalars(
            select(StockMovement)
            .where(StockMovement.fk_product_variant_id == variant_id)
            .order_by(StockMovement.pk_stock_movement_id.desc())
            .limit(limit)
        ).all()
    )


def shipments_for_variant(db: Session, variant_id: int) -> list[ShipmentLine]:
    """Every historical invoice this item arrived on (§11)."""
    return list(
        db.scalars(
            select(ShipmentLine)
            .where(
                ShipmentLine.fk_product_variant_id == variant_id,
                ShipmentLine.scd_active_flag.is_(True),
            )
            .order_by(ShipmentLine.pk_shipment_line_id.desc())
        ).all()
    )


def queue_low_stock_alerts(db: Session) -> int:
    """Email staff about items at or below their minimum (§11, §2.7)."""
    from app.services.email import queue_template_email

    low = stock_positions(db, only_low=True)
    sent = 0
    today = utcnow().date().isoformat()

    for row in low:
        queue_template_email(
            db,
            EmailTemplateCode.LOW_STOCK_ALERT,
            recipient="inventory@jecjordan.com",
            language="en",
            # One alert per item per day, so a slow-moving item does not email
            # every time the job runs.
            idempotency_key=f"low_stock:{row.variant.pk_product_variant_id}:{today}",
            params={
                "product_name": row.product.name_en,
                "quantity": row.sellable,
            },
        )
        sent += 1
    return sent


def next_reference(db: Session, model, prefix: str, column: str) -> str:
    """``PREFIX-YYMMDD-NNN`` for shipments, transfers and stock takes."""
    now = utcnow()
    stem = f"{prefix}-{now:%y%m%d}"
    count = db.scalar(
        select(func.count())
        .select_from(model)
        .where(getattr(model, column).like(f"{stem}-%"))
    ) or 0
    return f"{stem}-{count + 1:03d}"
