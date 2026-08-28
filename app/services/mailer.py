"""Sending queued email (Part I §2.7; Part II §5).

``services/email.py`` renders and queues; this drains the queue. Keeping the two
apart is what makes a slow or unreachable SMTP server a background problem
rather than a checkout that hangs.

Delivery rules:

* **At-most-once per row.** A row is claimed by bumping ``attempt_count`` and
  is only marked sent after SMTP accepts it. Combined with the unique
  ``idempotency_key`` at queue time, a customer cannot be emailed twice for the
  same event (Part II §5).
* **Failures are recorded, never swallowed.** ``error_detail`` holds the reason
  and the row stays in the queue until it exhausts its attempts, at which point
  it is closed so it stops being retried forever.
* **Backoff between attempts**, so a temporarily down mail server is retried
  patiently rather than hammered.

Bodies are sent as both plain text and HTML. The templates are authored as
text — an Arabic email should read correctly in a plain-text client — and the
HTML part just wraps it with direction and a readable font.
"""

from __future__ import annotations

import datetime as dt
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.marketing import EmailOutbox

log = get_logger(__name__)

#: Give up after this many failures and close the row, so a permanently bad
#: address does not sit in the queue forever.
MAX_ATTEMPTS = 5

#: Minutes to wait before retrying, indexed by attempts already made.
BACKOFF_MINUTES = (1, 5, 15, 60, 240)


@dataclass(slots=True)
class SendResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    abandoned: int = 0

    @property
    def attempted(self) -> int:
        return self.sent + self.failed


class Transport:
    """SMTP transport. Subclass or swap for testing.

    Deliberately a small object rather than a module function so a test can
    inject a fake without monkeypatching ``smtplib``.
    """

    def send(self, message: EmailMessage) -> None:  # pragma: no cover - I/O
        if settings.smtp_use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=context)
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)


class ConsoleTransport(Transport):
    """Logs instead of sending — the default when no SMTP host is configured.

    Better than a silent no-op in development: the message is visible, and
    nothing is marked sent that was not actually handed somewhere.
    """

    def send(self, message: EmailMessage) -> None:
        log.info(
            "email_console",
            extra={
                "to": message["To"],
                "subject": message["Subject"],
                "bytes": len(message.as_bytes()),
            },
        )


def default_transport() -> Transport:
    """The configured transport.

    Explicit rather than inferred: guessing from whether an SMTP host looks
    reachable would mean development quietly recording a delivery failure for
    every queued email.
    """
    if settings.mail_transport == "smtp" and settings.smtp_host:
        return Transport()
    return ConsoleTransport()


# ---------------------------------------------------------------------------
# Building the message
# ---------------------------------------------------------------------------


def build_message(row: EmailOutbox) -> EmailMessage:
    """Compose a MIME message with correct headers for Arabic content."""
    message = EmailMessage()

    # Headers are assigned as plain strings: EmailMessage's default policy does
    # the RFC 2047 encoding an Arabic sender name or subject needs. Wrapping
    # them in email.header.Header is the older Message API and raises here.
    message["From"] = formataddr((settings.mail_from_name, settings.mail_from))
    message["To"] = row.recipient_email
    message["Subject"] = row.subject
    message["Message-ID"] = make_msgid(domain=settings.mail_from.split("@")[-1])
    message["Date"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    # base64, not the default 8bit: an Arabic body left as raw 8-bit requires
    # the receiving server to support the 8BITMIME extension, and one that does
    # not will mangle or reject it. base64 is 7-bit clean everywhere, and more
    # compact than quoted-printable for Arabic (where QP expands every byte).
    message.set_content(row.body, subtype="plain", charset="utf-8", cte="base64")
    message.add_alternative(
        _html_body(row), subtype="html", charset="utf-8", cte="base64"
    )

    # Set last: add_alternative turns the message multipart and moves content
    # headers into the first subpart, so setting this earlier would bury it.
    # It lets a mail client pick the right direction and hyphenation.
    message["Content-Language"] = row.language
    return message


def _html_body(row: EmailOutbox) -> str:
    """Wrap the authored text in minimal, direction-aware HTML.

    No layout beyond direction and a legible font: email clients mangle
    anything ambitious, and the text part is the authored source of truth.
    """
    direction = "rtl" if row.language == "ar" else "ltr"
    escaped = (
        row.body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        f'<!doctype html><html lang="{row.language}" dir="{direction}">'
        "<head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:24px;background:#F5F5F5;">'
        '<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:10px;'
        'padding:24px;font-family:\'IBM Plex Sans Arabic\',\'IBM Plex Sans\','
        "system-ui,sans-serif;font-size:15px;line-height:1.65;color:#2B2B2F;"
        f'direction:{direction};text-align:{"right" if direction == "rtl" else "left"};">'
        f'<div style="border-bottom:3px solid #E63950;padding-bottom:12px;margin-bottom:16px;'
        f'font-weight:700;color:#2C3446;">{settings.mail_from_name}</div>'
        f'<div style="white-space:pre-wrap;">{escaped}</div>'
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# Draining the queue
# ---------------------------------------------------------------------------


def pending(db: Session, *, limit: int = 50, now: dt.datetime | None = None) -> list[EmailOutbox]:
    """Rows due for a send attempt, oldest first.

    A row is due when it has never been sent, still has attempts left, and its
    backoff window has elapsed.
    """
    now = now or utcnow()
    candidates = db.scalars(
        select(EmailOutbox)
        .where(
            EmailOutbox.scd_active_flag.is_(True),
            EmailOutbox.sent_dt.is_(None),
            EmailOutbox.attempt_count < MAX_ATTEMPTS,
        )
        .order_by(EmailOutbox.queued_dt)
        .limit(limit * 4)
    ).all()

    due = [row for row in candidates if _is_due(row, now)]
    return due[:limit]


def _is_due(row: EmailOutbox, now: dt.datetime) -> bool:
    if row.attempt_count == 0:
        return True
    wait = BACKOFF_MINUTES[min(row.attempt_count - 1, len(BACKOFF_MINUTES) - 1)]
    last = row.scd_active_from or row.queued_dt
    return now >= last + dt.timedelta(minutes=wait)


def send_pending(
    db: Session,
    *,
    transport: Transport | None = None,
    limit: int = 50,
    now: dt.datetime | None = None,
) -> SendResult:
    """Attempt delivery for every due row. Safe to call repeatedly."""
    transport = transport or default_transport()
    now = now or utcnow()
    result = SendResult()

    for row in pending(db, limit=limit, now=now):
        # Claim the row first: if the process dies mid-send, the attempt is
        # still counted, so a message can never be retried indefinitely.
        row.attempt_count += 1
        row.scd_active_from = now
        db.flush()

        try:
            transport.send(build_message(row))
        except Exception as exc:  # noqa: BLE001 - any delivery failure is recorded
            row.error_detail = f"{type(exc).__name__}: {exc}"[:2000]
            result.failed += 1
            log.warning(
                "email_send_failed",
                extra={
                    "outbox_id": row.pk_email_outbox_id,
                    "recipient": row.recipient_email,
                    "attempt": row.attempt_count,
                },
            )
            if row.attempt_count >= MAX_ATTEMPTS:
                # Out of attempts: close the row so it stops being picked up,
                # while its error stays on the record.
                row.close()
                result.abandoned += 1
                log.error(
                    "email_abandoned",
                    extra={
                        "outbox_id": row.pk_email_outbox_id,
                        "recipient": row.recipient_email,
                    },
                )
            continue

        row.sent_dt = utcnow()
        row.error_detail = None
        result.sent += 1
        log.info(
            "email_sent",
            extra={
                "outbox_id": row.pk_email_outbox_id,
                "template_code": row.template_code,
                "recipient": row.recipient_email,
            },
        )

    db.flush()
    return result


def outbox_summary(db: Session) -> dict[str, int]:
    """Counts for the admin view: queued, sent, failed, abandoned.

    Uses ``case()`` rather than SQLite's ``iif`` so the query stays portable to
    PostgreSQL (Part II §1).
    """
    from sqlalchemy import case, func

    def count_where(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    rows = db.execute(
        select(
            func.count(),
            count_where(EmailOutbox.sent_dt.is_not(None)),
            count_where(
                EmailOutbox.sent_dt.is_(None) & EmailOutbox.error_detail.is_not(None)
            ),
            count_where(EmailOutbox.scd_active_flag.is_(False)),
        ).select_from(EmailOutbox)
    ).one()

    total, sent, failed, abandoned = (int(v or 0) for v in rows)
    return {
        "total": total,
        "sent": sent,
        "failed": failed,
        "abandoned": abandoned,
        "queued": total - sent - abandoned,
    }
