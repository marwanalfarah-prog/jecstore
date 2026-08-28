"""Stock reservation locking (Part I §8; Part II §6).

The spec is precise about why this module exists. Re-validating stock before
checkout completes is **not enough**: two customers can both pass validation for
the last unit inside the same window, and both orders succeed. §8 therefore
requires the stock to be *locked* at the moment of validation, not merely
re-read.

That is the one place SQLite and PostgreSQL genuinely differ (Part II §7.4), so
the difference is handled here explicitly rather than papered over:

* **PostgreSQL** — ``SELECT ... FOR UPDATE`` takes row locks on exactly the
  ``scd_stock_level`` rows being checked. Other transactions touching those rows
  block; unrelated checkouts proceed in parallel.
* **SQLite** — there are no row locks. A write transaction takes a
  database-wide ``RESERVED`` lock instead, so the way to serialise is to *write*
  before reading. A no-op ``UPDATE`` on the target rows acquires that lock and
  holds it until commit, which makes the read-then-write sequence atomic against
  other writers. ``PRAGMA busy_timeout=30000`` (set in ``app/db/session.py``)
  makes a contending writer wait rather than fail instantly.

Both paths give the same guarantee: **between reading a quantity and writing the
reservation, nobody else can change that quantity.** SQLite gets it by
serialising all writers, PostgreSQL by locking only the rows in play.

Everything here must run inside a transaction the caller owns. The locks release
on commit or rollback, so the caller decides how long they are held — and should
hold them for as little work as possible.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import OutOfStock, StockLocked
from app.core.logging import get_logger
from app.models.enums import MovementKind
from app.models.inventory import StockLevel, StockPool

log = get_logger(__name__)


def _is_sqlite(db: Session) -> bool:
    return db.get_bind().dialect.name == "sqlite"


def lock_stock_levels(db: Session, variant_ids: Sequence[int]) -> list[StockLevel]:
    """Lock and return the sellable stock rows for ``variant_ids``.

    Returns every sellable ``StockLevel`` row across all sellable, owned pools
    for those variants, locked for the remainder of the caller's transaction.
    Consignment-out pools are excluded — that stock is not ours to sell from
    here (Part I §7).

    The returned rows are safe to read *and* to decrement: no other transaction
    can change them until the caller commits.
    """
    if not variant_ids:
        return []

    unique_ids = sorted(set(variant_ids))

    if _is_sqlite(db):
        # Acquire SQLite's database-wide write lock before reading. The UPDATE
        # is deliberately a no-op on the data — its only job is to take the
        # lock, which is what makes the subsequent read trustworthy.
        #
        # Ordered by primary key so concurrent checkouts always take rows in
        # the same order; on PostgreSQL that is what prevents deadlock between
        # two carts holding overlapping items, and it costs nothing here.
        db.execute(
            update(StockLevel)
            .where(
                StockLevel.fk_product_variant_id.in_(unique_ids),
                StockLevel.scd_active_flag.is_(True),
            )
            .values(quantity_reserved=StockLevel.quantity_reserved)
            .execution_options(synchronize_session=False)
        )
        locked_stmt = _sellable_levels_stmt(unique_ids)
    else:
        locked_stmt = _sellable_levels_stmt(unique_ids).with_for_update(of=StockLevel)

    rows = list(db.scalars(locked_stmt).all())
    log.debug(
        "stock_locked",
        extra={"variants": len(unique_ids), "levels": len(rows), "engine": db.get_bind().dialect.name},
    )
    return rows


def _sellable_levels_stmt(variant_ids: Sequence[int]):
    return (
        select(StockLevel)
        .join(StockPool, StockPool.pk_stock_pool_id == StockLevel.fk_stock_pool_id)
        .where(
            StockLevel.fk_product_variant_id.in_(variant_ids),
            StockLevel.scd_active_flag.is_(True),
            StockPool.scd_active_flag.is_(True),
            StockPool.is_sellable_flag.is_(True),
        )
        # Deterministic lock order across concurrent checkouts.
        .order_by(StockLevel.pk_stock_level_id)
    )


def sellable_by_variant(levels: Sequence[StockLevel]) -> dict[int, int]:
    """Total sellable units per variant across the locked rows.

    Sellable is ``on_hand - reserved``: units already promised to a placed order
    are physically present but not available to the next shopper (Part I §8).
    """
    totals: dict[int, int] = {}
    for level in levels:
        totals[level.fk_product_variant_id] = (
            totals.get(level.fk_product_variant_id, 0) + level.quantity_sellable
        )
    return totals


def reserve(
    db: Session,
    levels: Sequence[StockLevel],
    *,
    variant_id: int,
    quantity: int,
) -> list[tuple[StockLevel, int]]:
    """Place ``quantity`` units of ``variant_id`` on hold against locked rows.

    Reserves rather than deducts: on checkout, quantities go **on hold** and are
    only deducted from on-hand when the order is handed over (Part I §8).

    Draws from pools in row order, filling each before moving to the next, so an
    order is satisfied from as few locations as possible — which is what staff
    picking it actually want. Returns the ``(level, units)`` split so the caller
    can record which pool each line came from and write the movement rows.

    ``levels`` **must** be the list returned by :func:`lock_stock_levels` in the
    same transaction. Calling this against unlocked rows reintroduces the exact
    race the module exists to close.
    """
    if quantity <= 0:
        return []

    candidates = [
        level
        for level in levels
        if level.fk_product_variant_id == variant_id and level.quantity_sellable > 0
    ]

    available = sum(level.quantity_sellable for level in candidates)
    if available < quantity:
        raise OutOfStock(
            "Only part of the requested quantity is still available.",
            details={"variant_id": variant_id, "requested": quantity, "available": available},
        )

    allocation: list[tuple[StockLevel, int]] = []
    remaining = quantity
    for level in candidates:
        if remaining <= 0:
            break
        take = min(level.quantity_sellable, remaining)
        level.quantity_reserved += take
        remaining -= take
        allocation.append((level, take))

    if remaining > 0:  # pragma: no cover - guarded by the availability check above
        raise StockLocked()

    return allocation


def release(levels: Sequence[StockLevel], *, variant_id: int, quantity: int) -> None:
    """Return held units to sellable stock — used when an order is cancelled.

    Never drives ``quantity_reserved`` below zero: a double-release is a bug
    worth surviving rather than one worth corrupting the ledger over.
    """
    remaining = quantity
    for level in levels:
        if remaining <= 0:
            break
        if level.fk_product_variant_id != variant_id or level.quantity_reserved <= 0:
            continue
        give_back = min(level.quantity_reserved, remaining)
        level.quantity_reserved -= give_back
        remaining -= give_back

    if remaining > 0:
        log.warning(
            "stock_release_incomplete",
            extra={"variant_id": variant_id, "unreleased": remaining},
        )


#: Movement kinds that pair with the two operations above, so callers writing to
#: TRX_STOCK_MOVEMENT do not have to remember which is which.
HOLD_MOVEMENT = MovementKind.RESERVATION_HOLD
RELEASE_MOVEMENT = MovementKind.RESERVATION_RELEASE
