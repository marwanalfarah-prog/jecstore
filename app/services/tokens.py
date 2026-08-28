"""Single-use email links (Part I §2.5, §2.7, §2.4).

Verification and password-reset links used to be generated, mailed and
discarded. Nothing stored them, so:

* a password-reset link could **never** be redeemed — there was no POST handler
  and nothing to check it against, which meant a customer who forgot their
  password had no way back into their account at all; and
* email verification worked by substring-searching the outbox body for the
  token, so a verification link never expired and anyone who could read
  ``TRX_EMAIL_OUTBOX`` could verify any account.

This module is the fix. Four properties carry it:

**Only the hash is stored.** The link itself exists in the customer's inbox and
nowhere else. A database dump must not hand an attacker a working set of
password-reset URLs.

**Purpose is part of the lookup.** Both link types arrive by the same channel
and look alike; without this, a verification link could be replayed as a
password reset.

**Issuing supersedes.** Asking for a second reset link invalidates the first,
so a forwarded or shoulder-surfed older email stops working.

**Redeeming consumes.** A used link dies immediately rather than staying live
until it expires. Reset emails sit in inboxes for years.

Lookups are constant-time on the secret: see :func:`consume`.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.security import constant_time_equals, generate_token
from app.db.base import utcnow
from app.models.enums import AuthTokenPurpose
from app.models.identity import AuthToken, User

log = get_logger(__name__)

#: How long each kind of link lives. A reset link is the more dangerous of the
#: two — it grants account access — so it expires far sooner. Verification is
#: generous because §2.5 says unverified accounts sit indefinitely and a
#: customer may not open the email until the weekend.
LIFETIMES: dict[str, dt.timedelta] = {
    AuthTokenPurpose.PASSWORD_RESET: dt.timedelta(hours=2),
    AuthTokenPurpose.EMAIL_VERIFICATION: dt.timedelta(days=7),
}


def _digest(token: str) -> str:
    """SHA-256, not a password hash.

    Deliberate: these are 32 bytes of `secrets` output, not a human-chosen
    password, so there is no dictionary to slow an attacker down against — and
    a lookup by hash has to be fast enough to index.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(
    db: Session,
    user: User,
    purpose: str,
    *,
    requested_ip: str | None = None,
) -> str:
    """Mint a link for ``user`` and return the **raw** token.

    The raw value is returned once, here, and never stored — put it straight
    into the email. Any earlier live token for the same purpose is superseded.
    """
    if purpose not in LIFETIMES:
        raise ValueError(f"Unknown token purpose: {purpose}")

    supersede(db, user.pk_user_id, purpose)

    raw = generate_token()
    now = utcnow()
    db.add(
        AuthToken(
            fk_user_id=user.pk_user_id,
            purpose=purpose,
            token_hash=_digest(raw),
            expires_dt=now + LIFETIMES[purpose],
            requested_ip=requested_ip,
            scd_active_from=now,
            scd_changed_by=user.pk_user_id,
        )
    )
    db.flush()

    log.info(
        "auth_token_issued",
        extra={"user_id": user.pk_user_id, "purpose": purpose},
    )
    return raw


def supersede(db: Session, user_id: int, purpose: str) -> int:
    """Close every live token of one purpose for one user.

    Called on every issue, so only the newest link works. Also called after a
    password change, which should invalidate any reset link still in flight.
    """
    closed = 0
    for token in db.scalars(
        select(AuthToken).where(
            AuthToken.fk_user_id == user_id,
            AuthToken.purpose == purpose,
            AuthToken.scd_active_flag.is_(True),
            AuthToken.consumed_dt.is_(None),
        )
    ).all():
        token.close(changed_by=user_id)
        closed += 1
    return closed


def consume(db: Session, raw_token: str, purpose: str) -> User | None:
    """Redeem a link. Returns the user, or ``None`` if it is not usable.

    ``None`` covers every failure — unknown, expired, already used, wrong
    purpose, closed account — deliberately without distinguishing them to the
    caller. A page that says "this link expired" rather than "no such link"
    tells an attacker which guesses were once real.
    """
    if not raw_token:
        return None

    now = utcnow()
    candidate = db.scalars(
        select(AuthToken).where(
            AuthToken.token_hash == _digest(raw_token),
            AuthToken.purpose == purpose,
            AuthToken.scd_active_flag.is_(True),
        )
    ).first()

    if candidate is None:
        return None

    # The indexed lookup above is already by digest, so this compares equal
    # values; it stays because a later change to that query must not silently
    # turn the comparison into a short-circuiting one.
    if not constant_time_equals(candidate.token_hash, _digest(raw_token)):
        return None

    if not candidate.is_live(now):
        log.info(
            "auth_token_rejected",
            extra={"user_id": candidate.fk_user_id, "purpose": purpose},
        )
        return None

    user = db.get(User, candidate.fk_user_id)
    if user is None or not user.scd_active_flag or not user.is_active_flag:
        return None

    candidate.consumed_dt = now
    candidate.close(changed_by=user.pk_user_id, at=now)
    db.flush()

    log.info(
        "auth_token_consumed",
        extra={"user_id": user.pk_user_id, "purpose": purpose},
    )
    return user


def live_token_count(db: Session, user_id: int, purpose: str) -> int:
    """Usable tokens a user currently holds — at most one, by construction."""
    now = utcnow()
    return sum(
        1
        for token in db.scalars(
            select(AuthToken).where(
                AuthToken.fk_user_id == user_id,
                AuthToken.purpose == purpose,
                AuthToken.scd_active_flag.is_(True),
            )
        ).all()
        if token.is_live(now)
    )


def prune_expired(db: Session, *, older_than: dt.timedelta = dt.timedelta(days=30)) -> int:
    """Close tokens long past their expiry, for the maintenance worker.

    Closes rather than deletes (Part II §6): the row is evidence that a reset
    was requested, which matters when investigating a compromised account.
    """
    cutoff = utcnow() - older_than
    closed = 0
    for token in db.scalars(
        select(AuthToken).where(
            AuthToken.scd_active_flag.is_(True),
            AuthToken.expires_dt < cutoff,
        )
    ).all():
        token.close()
        closed += 1
    if closed:
        log.info("auth_tokens_pruned", extra={"count": closed})
    return closed
