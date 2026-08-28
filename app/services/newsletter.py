"""Newsletter subscription state (Part I §2.6)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import normalize_email
from app.db.base import utcnow
from app.models.marketing import NewsletterSubscription


def active_subscription(db: Session, email: str) -> NewsletterSubscription | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    return db.scalars(
        select(NewsletterSubscription)
        .where(
            NewsletterSubscription.email == normalized,
            NewsletterSubscription.scd_active_flag.is_(True),
        )
        .order_by(NewsletterSubscription.pk_newsletter_subscription_id.desc())
    ).first()


def is_subscribed(db: Session, email: str) -> bool:
    row = active_subscription(db, email)
    return bool(row and row.is_subscribed_flag)


def set_subscription(
    db: Session,
    *,
    email: str,
    subscribed: bool,
    source: str,
    user_id: int | None = None,
    changed_by: int | None = None,
) -> NewsletterSubscription:
    """Close the previous active row and insert the new current state."""
    normalized = normalize_email(email)
    now = utcnow()
    existing = active_subscription(db, normalized)
    if (
        existing is not None
        and existing.is_subscribed_flag == subscribed
        and existing.fk_user_id == user_id
    ):
        return existing

    if existing is not None:
        existing.close(changed_by=changed_by, at=now)

    row = NewsletterSubscription(
        email=normalized,
        fk_user_id=user_id,
        is_subscribed_flag=subscribed,
        subscribed_dt=now if subscribed else None,
        unsubscribed_dt=None if subscribed else now,
        source=source,
        scd_active_from=now,
        scd_changed_by=changed_by,
    )
    db.add(row)
    return row
