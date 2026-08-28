"""Password hashing, token generation and input normalisation.

Normalisation lives here rather than in each form handler because Part I §2.5 is
explicit that it happens **on storage**, once. A username compared one way at
registration and another at login is how duplicate accounts appear.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.logging import get_logger

log = get_logger(__name__)

# Argon2id at library defaults: memory-hard, and the current OWASP
# recommendation over bcrypt/PBKDF2.
_hasher = PasswordHasher()

#: Instagram-style: Latin letters, digits, dot and underscore. Arabic usernames
#: are not allowed (Part I §2.5) — the email and display name carry Arabic.
USERNAME_PATTERN = re.compile(r"^[a-z0-9._]{3,30}$")

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current cost parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_token(length: int = 32) -> str:
    """URL-safe token for email verification, password reset and session keys."""
    return secrets.token_urlsafe(length)[:64]


def constant_time_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)


# ---------------------------------------------------------------------------
# Normalisation (Part I §2.5 — "all input is trimmed/cleaned before storage")
# ---------------------------------------------------------------------------


def normalize_username(value: str) -> str:
    """Lower-case and NFKC-normalise. Usernames are not case-sensitive."""
    return unicodedata.normalize("NFKC", value or "").strip().lower()


def is_valid_username(value: str) -> bool:
    return bool(USERNAME_PATTERN.match(normalize_username(value)))


def normalize_email(value: str) -> str:
    """Lower-case the whole address.

    The local part is case-sensitive per RFC, but no real mail provider treats
    it that way, and allowing ``Ali@x.com`` and ``ali@x.com`` as two accounts
    creates exactly the duplicate-account confusion §2.5 is guarding against.
    """
    return unicodedata.normalize("NFKC", value or "").strip().lower()


_PHONE_NOISE = re.compile(r"[^\d]")


def normalize_phone(value: str) -> str:
    """Strip dashes, spaces and formatting — digits only (Part I §2.5)."""
    return _PHONE_NOISE.sub("", value or "")


def normalize_country_code(value: str) -> str:
    digits = _PHONE_NOISE.sub("", value or "")
    return f"+{digits}" if digits else ""


def clean_text(value: str | None, *, max_length: int | None = None) -> str | None:
    """Trim and collapse whitespace on any free-text field before storage."""
    if value is None:
        return None
    cleaned = unicodedata.normalize("NFKC", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None
    return cleaned[:max_length] if max_length else cleaned


def password_problems(password: str, confirm: str | None = None) -> list[str]:
    """Return every problem at once, so the form is not a guessing game."""
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if not any(c.isalpha() for c in password):
        problems.append("Password must contain at least one letter.")
    if not any(c.isdigit() for c in password):
        problems.append("Password must contain at least one number.")
    if confirm is not None and password != confirm:
        problems.append("Passwords do not match.")
    return problems
