"""Single-use email links (Part I §2.5, §2.7, §2.4).

These links are the only thing standing between an email address and an
account, so the tests here are almost all about what must *not* work: a
replayed link, a link used twice, a link for the wrong purpose, a link that
outlived its window.

The bug this module was written to fix is pinned first: reset tokens used to be
generated, mailed and discarded, so the reset flow could never complete at all.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.enums import AuthTokenPurpose
from app.models.identity import AuthToken, User
from app.services import tokens
from tests.test_checkout import db, store  # noqa: F401

RESET = AuthTokenPurpose.PASSWORD_RESET
VERIFY = AuthTokenPurpose.EMAIL_VERIFICATION


@pytest.fixture
def user(db: Session, store: dict) -> User:
    return store["user"]


@pytest.fixture
def other_user(db: Session, store: dict) -> User:
    other = User(
        fk_role_id=store["user"].fk_role_id,
        username="second",
        email="second@example.com",
        password_hash="x",
        scd_active_from=utcnow(),
    )
    db.add(other)
    db.commit()
    return other


def _row(db: Session, raw: str) -> AuthToken:
    return db.scalars(
        select(AuthToken).where(AuthToken.token_hash == tokens._digest(raw))
    ).one()


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_a_token_can_be_issued_and_redeemed(db: Session, user: User):
    raw = tokens.issue(db, user, RESET)
    db.commit()

    redeemed = tokens.consume(db, raw, RESET)
    assert redeemed is not None
    assert redeemed.pk_user_id == user.pk_user_id


def test_the_raw_token_is_never_stored(db: Session, user: User):
    """A database dump must not hand over working reset links."""
    raw = tokens.issue(db, user, RESET)
    db.commit()

    stored = db.scalars(select(AuthToken)).one()
    assert stored.token_hash != raw
    assert raw not in stored.token_hash
    assert len(stored.token_hash) == 64, "sha256 hex"


def test_issuing_returns_a_token_long_enough_to_be_unguessable(db: Session, user: User):
    raw = tokens.issue(db, user, RESET)
    assert len(raw) >= 32


def test_two_issues_produce_different_tokens(db: Session, user: User):
    first = tokens.issue(db, user, VERIFY)
    second = tokens.issue(db, user, VERIFY)
    assert first != second


# ---------------------------------------------------------------------------
# What must not work
# ---------------------------------------------------------------------------


def test_a_token_cannot_be_used_twice(db: Session, user: User):
    """Reset emails sit in inboxes for years and get forwarded."""
    raw = tokens.issue(db, user, RESET)
    db.commit()

    assert tokens.consume(db, raw, RESET) is not None
    assert tokens.consume(db, raw, RESET) is None


def test_issuing_supersedes_the_previous_link(db: Session, user: User):
    """Asking for a second link must kill the first, so a shoulder-surfed older
    email stops working."""
    first = tokens.issue(db, user, RESET)
    second = tokens.issue(db, user, RESET)
    db.commit()

    assert tokens.consume(db, first, RESET) is None
    assert tokens.consume(db, second, RESET) is not None


def test_a_verification_link_cannot_be_replayed_as_a_password_reset(
    db: Session, user: User
):
    """Both arrive by the same channel and look alike; purpose is what keeps
    them apart."""
    raw = tokens.issue(db, user, VERIFY)
    db.commit()

    assert tokens.consume(db, raw, RESET) is None
    # And it still works for what it was for.
    assert tokens.consume(db, raw, VERIFY) is not None


def test_an_expired_token_is_refused(db: Session, user: User):
    raw = tokens.issue(db, user, RESET)
    _row(db, raw).expires_dt = utcnow() - dt.timedelta(seconds=1)
    db.commit()

    assert tokens.consume(db, raw, RESET) is None


def test_an_unknown_token_is_refused(db: Session, user: User):
    assert tokens.consume(db, "not-a-real-token", RESET) is None


def test_an_empty_token_is_refused(db: Session, user: User):
    assert tokens.consume(db, "", RESET) is None


def test_a_deactivated_account_cannot_be_reset_into(db: Session, user: User):
    """Closing an account has to close the way back into it too."""
    raw = tokens.issue(db, user, RESET)
    user.is_active_flag = False
    db.commit()

    assert tokens.consume(db, raw, RESET) is None


def test_one_users_token_never_resolves_to_another(
    db: Session, user: User, other_user: User
):
    mine = tokens.issue(db, user, RESET)
    theirs = tokens.issue(db, other_user, RESET)
    db.commit()

    assert tokens.consume(db, mine, RESET).pk_user_id == user.pk_user_id
    assert tokens.consume(db, theirs, RESET).pk_user_id == other_user.pk_user_id


def test_issuing_for_one_user_does_not_disturb_another(
    db: Session, user: User, other_user: User
):
    theirs = tokens.issue(db, other_user, RESET)
    tokens.issue(db, user, RESET)
    db.commit()

    assert tokens.consume(db, theirs, RESET) is not None


def test_an_unknown_purpose_is_a_programming_error(db: Session, user: User):
    with pytest.raises(ValueError):
        tokens.issue(db, user, "log_in_as_admin")


# ---------------------------------------------------------------------------
# Lifetimes
# ---------------------------------------------------------------------------


def test_a_reset_link_expires_far_sooner_than_a_verification_link():
    """A reset link grants account access; a verification link does not."""
    assert tokens.LIFETIMES[RESET] < tokens.LIFETIMES[VERIFY]


def test_a_reset_link_lives_hours_not_days():
    assert tokens.LIFETIMES[RESET] <= dt.timedelta(hours=24)


def test_the_expiry_is_set_from_the_purpose(db: Session, user: User):
    before = utcnow()
    raw = tokens.issue(db, user, RESET)
    db.commit()

    expires = _row(db, raw).expires_dt
    assert expires > before
    assert expires <= before + tokens.LIFETIMES[RESET] + dt.timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def test_a_user_holds_at_most_one_live_token_per_purpose(db: Session, user: User):
    for _ in range(4):
        tokens.issue(db, user, RESET)
    db.commit()

    assert tokens.live_token_count(db, user.pk_user_id, RESET) == 1


def test_consuming_leaves_no_live_token(db: Session, user: User):
    raw = tokens.issue(db, user, RESET)
    db.commit()
    tokens.consume(db, raw, RESET)
    db.commit()

    assert tokens.live_token_count(db, user.pk_user_id, RESET) == 0


def test_superseded_rows_are_closed_not_deleted(db: Session, user: User):
    """Part II §6, and the row is evidence a reset was requested — which
    matters when investigating a compromised account."""
    tokens.issue(db, user, RESET)
    tokens.issue(db, user, RESET)
    db.commit()

    assert db.scalar(select(AuthToken).where(AuthToken.scd_active_flag.is_(False))) is not None
    assert len(db.scalars(select(AuthToken)).all()) == 2


def test_pruning_closes_long_expired_tokens_without_deleting_them(
    db: Session, user: User
):
    raw = tokens.issue(db, user, RESET)
    _row(db, raw).expires_dt = utcnow() - dt.timedelta(days=90)
    db.commit()

    assert tokens.prune_expired(db) == 1
    db.commit()

    assert len(db.scalars(select(AuthToken)).all()) == 1, "closed, not deleted"
    assert tokens.live_token_count(db, user.pk_user_id, RESET) == 0


def test_pruning_leaves_recent_tokens_alone(db: Session, user: User):
    tokens.issue(db, user, RESET)
    db.commit()

    assert tokens.prune_expired(db) == 0
    assert tokens.live_token_count(db, user.pk_user_id, RESET) == 1


def test_the_requesting_ip_is_recorded(db: Session, user: User):
    """§2.8 wants an IP on every logged event, and a reset request is one of the
    events worth being able to trace."""
    raw = tokens.issue(db, user, RESET, requested_ip="203.0.113.7")
    db.commit()

    assert _row(db, raw).requested_ip == "203.0.113.7"
