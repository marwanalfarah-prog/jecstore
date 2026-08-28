"""Transactional email queueing (Part I §2.7; Part II §5).

Every email goes through one admin-controlled template system, in the
recipient's language, and lands in an outbox row before anything is sent. Two
reasons that ordering matters:

* **Idempotency.** A retried job reuses the same ``idempotency_key``, and the
  unique constraint means the second attempt is a no-op rather than a duplicate
  email to a customer (Part II §5).
* **Nothing fails silently.** A send that errors records the reason on its row;
  it does not vanish into a log line nobody reads.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.marketing import EmailOutbox, EmailTemplate

log = get_logger(__name__)


class MissingPlaceholderError(ValueError):
    """Raised when a template body drops a token the system requires.

    A password-reset email without its reset link is worse than no email at all,
    so this is rejected at save time rather than discovered by a locked-out
    customer (Part I §2.7).
    """


def render_template(
    template: EmailTemplate, language: str, params: dict[str, Any]
) -> tuple[str, str]:
    subject = (template.subject_ar if language == "ar" else template.subject_en) or ""
    body = (template.body_ar if language == "ar" else template.body_en) or ""

    for token in _required_tokens(template):
        if token not in body:
            raise MissingPlaceholderError(
                f"Template '{template.template_code}' ({language}) is missing "
                f"the required placeholder {{{token}}}."
            )

    return _substitute(subject, params), _substitute(body, params)


def queue_template_email(
    db: Session,
    template_code: str,
    *,
    recipient: str,
    language: str,
    idempotency_key: str,
    params: dict[str, Any] | None = None,
) -> EmailOutbox | None:
    """Render and queue one email. Returns ``None`` if it was already queued."""
    params = params or {}

    template = db.scalars(
        select(EmailTemplate).where(
            EmailTemplate.template_code == template_code,
            EmailTemplate.scd_active_flag.is_(True),
            EmailTemplate.is_enabled_flag.is_(True),
        )
    ).first()

    if template is None:
        log.error("email_template_missing", extra={"template_code": template_code})
        return None

    subject, body = render_template(template, language, params)

    entry = EmailOutbox(
        idempotency_key=idempotency_key,
        template_code=template_code,
        recipient_email=recipient,
        language=language,
        subject=subject,
        body=body,
        queued_dt=utcnow(),
        scd_active_from=utcnow(),
    )
    # Inside a SAVEPOINT, so a duplicate unwinds *only this insert*.
    #
    # A plain db.rollback() here would discard the caller's whole transaction —
    # an order being placed, a previously queued email, anything else pending.
    # The duplicate is an expected, benign outcome (that is what the
    # idempotency key is for), so it must not take unrelated work with it.
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
    except IntegrityError:
        log.info("email_already_queued", extra={"idempotency_key": idempotency_key})
        return None

    log.info(
        "email_queued",
        extra={"template_code": template_code, "recipient": recipient, "language": language},
    )
    return entry


def _required_tokens(template: EmailTemplate) -> list[str]:
    if not template.required_placeholders:
        return []
    return [t.strip() for t in template.required_placeholders.split(",") if t.strip()]


def _substitute(text: str, params: dict[str, Any]) -> str:
    """Replace ``{token}`` placeholders.

    Deliberately not ``str.format``: an admin-authored template containing a
    stray brace should render imperfectly, not raise mid-send.
    """
    for key, value in params.items():
        text = text.replace(f"{{{key}}}", str(value))
    return text
