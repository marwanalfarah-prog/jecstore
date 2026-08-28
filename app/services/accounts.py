"""Customer self-service on their own account (Part I §2.3, §2.5, §2.6).

§2.6 lists what a customer may do with their own record: edit their submitted
data, change their password, and see their active sessions. §2.3 attaches a
rule to the second of those — *changing a password immediately invalidates all
other active sessions for that account, on every device*.

That rule is the reason :func:`set_password` exists rather than each caller
hashing and assigning. It has to hold for the reset-by-email path as much as
for the change-while-signed-in path, and a rule implemented twice is a rule
implemented once and forgotten once.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.logging import get_logger
from app.core.security import (
    clean_text,
    hash_password,
    normalize_phone,
    password_problems,
    verify_password,
)
from app.db.base import utcnow
from app.models.activity import UserSession
from app.models.enums import AuthTokenPurpose, SessionEndReason
from app.models.identity import Address, Country, IndividualProfile, Province, User
from app.services import tokens
from app.services.sessions import end_all_sessions_for_user

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Passwords (Part I §2.3, §2.6)
# ---------------------------------------------------------------------------


def set_password(
    db: Session,
    user: User,
    new_password: str,
    confirm: str | None = None,
    *,
    keep_session_key: str | None = None,
) -> int:
    """Change a password and log every other device out (§2.3).

    ``keep_session_key`` spares the session doing the changing, so a customer
    updating their password from their own browser is not immediately kicked
    out of it. Pass ``None`` on the reset-by-email path: whoever held the old
    password may still be signed in somewhere, and that is exactly who the
    reset is meant to remove.

    Returns the number of sessions terminated.
    """
    problems = password_problems(new_password, confirm)
    if problems:
        raise ValidationFailed(problems[0], details={"problems": problems})

    user.password_hash = hash_password(new_password)
    user.password_changed_dt = utcnow()

    # Any reset link still in flight is now stale — including the one that may
    # have just been used to get here.
    tokens.supersede(db, user.pk_user_id, AuthTokenPurpose.PASSWORD_RESET)

    closed = end_all_sessions_for_user(
        db,
        user.pk_user_id,
        SessionEndReason.PASSWORD_CHANGED,
        except_key=keep_session_key,
    )
    db.flush()

    log.info(
        "password_changed",
        extra={"user_id": user.pk_user_id, "sessions_ended": closed},
    )
    return closed


def change_own_password(
    db: Session,
    user: User,
    *,
    current_password: str,
    new_password: str,
    confirm: str,
    keep_session_key: str | None = None,
) -> int:
    """Change a password while signed in (§2.6).

    The current password is required even though the session already proves who
    they are: it is what stops a borrowed unlocked laptop from becoming a
    permanent takeover.
    """
    if not verify_password(current_password, user.password_hash):
        raise ValidationFailed("That is not your current password.")
    if verify_password(new_password, user.password_hash):
        raise ValidationFailed("Choose a password you have not used here before.")

    return set_password(
        db, user, new_password, confirm, keep_session_key=keep_session_key
    )


# ---------------------------------------------------------------------------
# Sessions (Part I §2.6 — the customer-facing counterpart to §2.3/§2.8)
# ---------------------------------------------------------------------------


def own_sessions(db: Session, user_id: int) -> list[UserSession]:
    """The customer's live sessions, newest first."""
    return list(
        db.scalars(
            select(UserSession)
            .where(
                UserSession.fk_user_id == user_id,
                UserSession.scd_active_flag.is_(True),
            )
            .order_by(UserSession.last_seen_dt.desc())
        ).all()
    )


def end_own_session(
    db: Session, user: User, session_key: str, *, current_session_key: str | None
) -> UserSession:
    """Sign one device out (§2.6).

    Scoped to the caller's own sessions, and refuses the session making the
    request — "log out of this device" is the Sign out button, and conflating
    them makes the list behave unpredictably.
    """
    if current_session_key and session_key == current_session_key:
        raise ValidationFailed("Use Sign out to end the session you are using.")

    session = db.scalars(
        select(UserSession).where(
            UserSession.session_key == session_key,
            UserSession.fk_user_id == user.pk_user_id,
            UserSession.scd_active_flag.is_(True),
        )
    ).first()
    if session is None:
        raise NotFound("That session is no longer active.")

    from app.services.sessions import end_session

    end_session(db, session, SessionEndReason.FORCED_BY_ADMIN)
    db.flush()

    log.info(
        "customer_ended_own_session",
        extra={"user_id": user.pk_user_id},
    )
    return session


def end_other_sessions(db: Session, user: User, *, keep_session_key: str | None) -> int:
    """Sign out everywhere else (§2.6)."""
    closed = end_all_sessions_for_user(
        db,
        user.pk_user_id,
        SessionEndReason.FORCED_BY_ADMIN,
        except_key=keep_session_key,
    )
    db.flush()
    return closed


# ---------------------------------------------------------------------------
# Profile (Part I §2.6, §2.5)
# ---------------------------------------------------------------------------

#: Editable here. Deliberately excludes username and email: §2.5 makes both
#: unique identifiers with normalisation rules, and email doubles as the
#: verification target — changing it silently would leave an account verified
#: against an address its owner no longer controls.
EDITABLE_PROFILE_FIELDS = ("first_name", "second_name", "third_name", "last_name")


def update_profile(
    db: Session,
    user: User,
    *,
    first_name: str | None = None,
    second_name: str | None = None,
    third_name: str | None = None,
    last_name: str | None = None,
    phone_country_code: str | None = None,
    phone_number: str | None = None,
    preferred_language: str | None = None,
    preferred_currency: str | None = None,
) -> User:
    """Edit the customer's own submitted data (§2.6).

    Everything is trimmed and the phone re-normalised, because §2.5 requires it
    on the way in and an edit is a way in.
    """
    if preferred_language in {"ar", "en"}:
        user.preferred_language = preferred_language
    if preferred_currency in {"JOD", "USD"}:
        user.preferred_currency = preferred_currency

    profile = db.scalars(
        select(IndividualProfile).where(
            IndividualProfile.fk_user_id == user.pk_user_id,
            IndividualProfile.scd_active_flag.is_(True),
        )
    ).first()

    if profile is not None:
        if first_name is not None:
            cleaned = clean_text(first_name, max_length=80)
            if not cleaned:
                raise ValidationFailed("A first name is required.")
            profile.first_name = cleaned
        if last_name is not None:
            cleaned = clean_text(last_name, max_length=80)
            if not cleaned:
                raise ValidationFailed("A last name is required.")
            profile.last_name = cleaned
        if second_name is not None:
            profile.second_name = clean_text(second_name, max_length=80)
        if third_name is not None:
            profile.third_name = clean_text(third_name, max_length=80)

        if phone_number is not None:
            normalized = normalize_phone(phone_number)
            if not normalized:
                raise ValidationFailed("A phone number is required.")
            profile.phone_number = normalized
        if phone_country_code is not None:
            from app.core.security import normalize_country_code

            profile.phone_country_code = normalize_country_code(phone_country_code)

        profile.scd_changed_by = user.pk_user_id

    user.scd_changed_by = user.pk_user_id
    db.flush()

    log.info("profile_updated", extra={"user_id": user.pk_user_id})
    return user


def own_addresses(db: Session, user_id: int) -> list[Address]:
    """Saved addresses, default first (§2.6)."""
    return list(
        db.scalars(
            select(Address)
            .where(
                Address.fk_user_id == user_id,
                Address.scd_active_flag.is_(True),
            )
            .order_by(Address.is_default_flag.desc(), Address.pk_address_id)
        ).all()
    )


def place_names(db: Session, addresses: list[Address]) -> dict[int, tuple[Country | None, Province | None]]:
    """Country and province rows for a batch of addresses, for display."""
    if not addresses:
        return {}

    countries = {
        row.pk_country_id: row
        for row in db.scalars(
            select(Country).where(
                Country.pk_country_id.in_({a.fk_country_id for a in addresses})
            )
        ).all()
    }
    provinces = {
        row.pk_province_id: row
        for row in db.scalars(
            select(Province).where(
                Province.pk_province_id.in_(
                    {a.fk_province_id for a in addresses if a.fk_province_id}
                )
            )
        ).all()
    }
    return {
        address.pk_address_id: (
            countries.get(address.fk_country_id),
            provinces.get(address.fk_province_id),
        )
        for address in addresses
    }
