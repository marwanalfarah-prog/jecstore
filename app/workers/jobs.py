"""The recurring jobs themselves.

Each is a plain function taking no arguments, opening its own session, and
committing its own work. That keeps them callable three ways without change:
from the scheduler, from the CLI (``python -m app.workers.run --once``), and
from a test.

All are **idempotent**. Re-running a job must not double-send an email, double-
count an alert, or re-abandon a cart — because a scheduler that overlaps runs,
or an operator who runs one by hand, must not corrupt anything.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import session_scope
from app.models.enums import ActivityEvent
from app.models.orders import Cart

log = get_logger(__name__)

#: A cart untouched for this long is treated as abandoned (Part I §2.8).
CART_ABANDON_AFTER_HOURS = 24

#: Live sessions are pruned once ended and archived; this is how long a closed
#: session row is kept in the lean live table before removal (Part II §6).
SESSION_PRUNE_AFTER_DAYS = 7


def drain_outbox() -> dict[str, int]:
    """Attempt delivery for every due queued email (Part I §2.7)."""
    from app.services import mailer

    with session_scope() as db:
        result = mailer.send_pending(db)

    if result.attempted:
        log.info(
            "outbox_drained",
            extra={
                "sent": result.sent,
                "failed": result.failed,
                "abandoned": result.abandoned,
            },
        )
    return {
        "sent": result.sent,
        "failed": result.failed,
        "abandoned": result.abandoned,
    }


def queue_low_stock_alerts() -> int:
    """Alert on items at or below their minimum (Part I §11).

    Idempotent by construction: the queue key includes the date, so an item
    below its minimum generates one alert per day however often this runs.
    """
    from app.services import inventory

    with session_scope() as db:
        count = inventory.queue_low_stock_alerts(db)

    if count:
        log.info("low_stock_alerts_queued", extra={"items": count})
    return count


def mark_abandoned_carts() -> int:
    """Flag carts idle past the threshold, for the §2.8 funnel dashboards.

    Only carts that have never been flagged are touched, so the abandonment
    timestamp records when the cart *was first* abandoned rather than when this
    job last happened to run.
    """
    from app.services.activity import record_event

    cutoff = utcnow() - dt.timedelta(hours=CART_ABANDON_AFTER_HOURS)
    marked = 0

    with session_scope() as db:
        carts = db.scalars(
            select(Cart).where(
                Cart.scd_active_flag.is_(True),
                Cart.converted_order_id.is_(None),
                Cart.abandoned_dt.is_(None),
                Cart.last_activity_dt.is_not(None),
                Cart.last_activity_dt < cutoff,
            )
        ).all()

        for cart in carts:
            cart.abandoned_dt = utcnow()
            record_event(
                db,
                ActivityEvent.CART_ABANDONED,
                target_table="scd_cart",
                target_row_id=cart.pk_cart_id,
            )
            marked += 1

    if marked:
        log.info("carts_marked_abandoned", extra={"carts": marked})
    return marked


def prune_expired_sessions() -> int:
    """Close sessions past their expiry, and drop long-closed rows.

    The live session table is deliberately lean (Part II §6): its history lives
    in the insert-only activity log, so a closed row can be removed once it is
    no longer useful to the "currently logged in" dashboard.
    """
    from app.models.activity import UserSession
    from app.models.enums import SessionEndReason

    now = utcnow()
    prune_before = now - dt.timedelta(days=SESSION_PRUNE_AFTER_DAYS)
    closed = 0

    with session_scope() as db:
        live = db.scalars(
            select(UserSession).where(
                UserSession.scd_active_flag.is_(True),
                UserSession.expires_dt <= now,
            )
        ).all()
        for session in live:
            session.end_reason = SessionEndReason.IDLE_TIMEOUT
            session.close(at=now)
            closed += 1

        # Removing an *ended* session row is the one sanctioned delete in the
        # system, and only because TRX_ACTIVITY_EVENT already holds its history
        # (see the SCD_USER_SESSION docstring).
        #
        # A Core DELETE rather than db.delete(): the ORM guard in
        # app/db/base.py refuses to delete SCD rows, and rightly so — this is
        # bulk table maintenance on already-archived rows, not a business
        # delete, so it is stated explicitly here instead of weakening a guard
        # that protects every other table.
        pruned = db.execute(
            sa_delete(UserSession).where(
                UserSession.scd_active_flag.is_(False),
                UserSession.scd_active_to < prune_before,
            )
        ).rowcount
        if pruned:
            log.info("sessions_pruned", extra={"count": pruned})

    if closed:
        log.info("sessions_expired", extra={"count": closed})
    return closed


def reindex_search() -> int:
    """Rebuild the product search projection (Part I §15).

    Catalog writes refresh a product's index inline, so this is a safety net
    for bulk imports and for anything that changed a tag or publisher name
    without touching the products that reference it.
    """
    from app.services import search

    with session_scope() as db:
        return search.reindex_all(db)


#: name → (callable, interval in seconds). The scheduler reads this, and
#: ``--once`` runs each exactly one time.
JOBS: dict[str, tuple] = {
    "drain_outbox": (drain_outbox, 60),
    "mark_abandoned_carts": (mark_abandoned_carts, 60 * 30),
    "queue_low_stock_alerts": (queue_low_stock_alerts, 60 * 60 * 6),
    "prune_expired_sessions": (prune_expired_sessions, 60 * 15),
    "reindex_search": (reindex_search, 60 * 60 * 12),
}
