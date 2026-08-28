"""Sessions and activity tracking (Part I §2.3, §2.8; Part II §6).

The split here is deliberate and is called out in the spec: a **lean live
session table** answers "who is logged in right now" and powers forced logout,
while the **insert-only TRX log** holds the full history. Keeping them apart
stops the hot session lookup from scanning millions of historical events.

Applies to every account type — customers, staff and admin alike.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SCDMixin, TrxBase, UtcDateTime, fk, pk


class UserSession(Base, SCDMixin):
    """A live login session — the fast-lookup store Part II §6 calls for.

    Kept lean by *pruning*, not by cutting columns: the full history of every
    session lives in ``TRX_ACTIVITY_EVENT``, so once a session has ended and been
    archived, its row here can be removed without losing anything. That is what
    keeps the "currently logged in" dashboard and forced-logout lookups fast
    while the historical log grows without bound.

    Ending a session is an SCD ``close()``: ``scd_active_to`` *is* the moment it
    ended, so there is no second "ended" column to disagree with it.
    """

    __tablename__ = "scd_user_session"
    __grain__ = "One login session, live or recently ended."

    pk_user_session_id: Mapped[int] = pk("user_session")
    #: Opaque random key held in the cookie; never the primary key, so a session
    #: id is not guessable by counting.
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")

    started_dt: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    last_seen_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    #: Computed from the global default or the role override (Part I §2.3).
    expires_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    #: Why it ended — logout, idle timeout, password change, forced by admin.
    #: *When* it ended is scd_active_to.
    end_reason: Mapped[str | None] = mapped_column(String(30))

    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    device_label: Mapped[str | None] = mapped_column(
        String(120), comment="Human-readable device summary for the customer's own sessions view."
    )
    approx_location: Mapped[str | None] = mapped_column(String(120))

    #: Impersonation (Part I §2.2.2). ``fk_user_id`` is the *target* — the
    #: session inherits the target's permissions, not the impersonator's — while
    #: this column records who is really behind the keyboard, so the banner and
    #: the audit trail can both name them.
    impersonator_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    #: The impersonator's own session, resumed automatically when this one ends
    #: — ending an impersonation is not a full re-login (Part I §2.2.2).
    parent_session_key: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_scd_user_session_live", "scd_active_flag", "fk_user_id"),
    )

    @property
    def is_impersonated(self) -> bool:
        return self.impersonator_user_id is not None

    def is_expired(self, now: dt.datetime) -> bool:
        expires = self.expires_dt
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.timezone.utc)
        return expires <= now


class ActivityEventLog(TrxBase):
    """Every logged event, insert-only (Part II §6).

    One table rather than one per event type: the admin dashboards in Part I
    §2.8 (currently-logged-in, login history, suspicious activity, funnel
    drop-off) all slice the same stream, and a single indexed table keeps those
    queries to one scan instead of five unions.
    """

    __tablename__ = "trx_activity_event"
    __grain__ = "One logged user or anonymous activity event."

    pk_activity_event_id: Mapped[int] = pk("activity_event")
    event_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    #: Null for anonymous visitors — guest browsing is allowed (Part I §14), and
    #: their funnel behaviour still counts.
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    username: Mapped[str | None] = mapped_column(String(60))
    session_key: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Tags the event as taken during impersonation so reports never conflate it
    #: with the target user's own activity (Part I §2.2.2).
    impersonator_user_id: Mapped[int | None] = mapped_column(Integer, index=True)

    #: Captured on every event, not just login (Part I §2.8).
    ip_address: Mapped[str | None] = mapped_column(String(45), index=True)
    user_agent: Mapped[str | None] = mapped_column(Text)

    path: Mapped[str | None] = mapped_column(String(500))
    referrer: Mapped[str | None] = mapped_column(String(500))
    #: Which row the event concerns (a product for PRODUCT_VIEW, a variant for
    #: CART_ITEM_ADDED) — plain columns, so funnel queries aggregate in SQL.
    target_table: Mapped[str | None] = mapped_column(String(80))
    target_row_id: Mapped[int | None] = mapped_column(Integer, index=True)
    quantity: Mapped[int | None] = mapped_column(Integer)

    success_flag: Mapped[bool | None] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Indexed on user, session and timestamp for dashboard performance at
        # scale, as Part II §6 requires.
        Index("ix_trx_activity_event_user_time", "user_id", "created_dt"),
        Index("ix_trx_activity_event_session_time", "session_key", "created_dt"),
        Index("ix_trx_activity_event_code_time", "event_code", "created_dt"),
        Index("ix_trx_activity_event_ip_time", "ip_address", "created_dt"),
    )


class LoginAttempt(TrxBase):
    """Failed and successful login attempts, kept separately from the general
    event stream because the suspicious-activity view queries them constantly
    and they need their own tight index (Part I §2.4, §2.8)."""

    __tablename__ = "trx_login_attempt"
    __grain__ = "One login attempt against one identifier from one IP."

    pk_login_attempt_id: Mapped[int] = pk("login_attempt")
    #: As typed — an attempt against a non-existent username is exactly the
    #: pattern the suspicious-activity view needs to surface.
    identifier: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), index=True)
    user_agent: Mapped[str | None] = mapped_column(Text)
    success_flag: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(60))
    captcha_required_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lockout_triggered_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_trx_login_attempt_identifier_time", "identifier", "created_dt"),
        Index("ix_trx_login_attempt_ip_time", "ip_address", "created_dt"),
    )
