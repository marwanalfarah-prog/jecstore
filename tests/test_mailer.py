"""Sending queued email (Part I §2.7; Part II §5).

The properties that matter are delivery guarantees, not formatting: a customer
must never be emailed twice for one event, a failure must be recorded rather
than swallowed, and a permanently bad address must eventually stop being
retried.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, utcnow
from app.models.marketing import EmailOutbox
from app.services import mailer


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class RecordingTransport(mailer.Transport):
    """Captures messages instead of sending them."""

    def __init__(self) -> None:
        self.messages: list = []

    def send(self, message) -> None:
        self.messages.append(message)


class FailingTransport(mailer.Transport):
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or ConnectionRefusedError("no route to mail server")
        self.attempts = 0

    def send(self, message) -> None:
        self.attempts += 1
        raise self.error


def _queue(db: Session, **overrides) -> EmailOutbox:
    now = utcnow()
    row = EmailOutbox(
        idempotency_key=overrides.pop("idempotency_key", f"key-{now.timestamp()}"),
        template_code=overrides.pop("template_code", "order_confirmation"),
        recipient_email=overrides.pop("recipient_email", "shopper@example.com"),
        language=overrides.pop("language", "ar"),
        subject=overrides.pop("subject", "تأكيد الطلب JEC-1"),
        body=overrides.pop("body", "شكراً لك.\nرقم الطلب JEC-1"),
        queued_dt=now,
        scd_active_from=now,
        **overrides,
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def test_arabic_message_is_seven_bit_clean(db: Session):
    """The whole message must survive a server without 8BITMIME or SMTPUTF8.

    Headers are RFC 2047 encoded and bodies base64'd, so nothing depends on an
    extension the receiving server might not offer.
    """
    row = _queue(db)
    message = mailer.build_message(row)

    raw = message.as_bytes()
    assert all(byte < 128 for byte in raw), "the serialised message must be 7-bit"
    # And it still decodes back to the original text.
    assert message["Subject"] == "تأكيد الطلب JEC-1"
    assert "شكراً" in message.get_body(("plain",)).get_content()


def test_message_has_plain_and_html_parts(db: Session):
    row = _queue(db)
    types = [part.get_content_type() for part in mailer.build_message(row).walk()]
    assert "text/plain" in types and "text/html" in types


def test_html_part_carries_direction_for_arabic(db: Session):
    row = _queue(db, language="ar")
    html = [
        part for part in mailer.build_message(row).walk()
        if part.get_content_type() == "text/html"
    ][0].get_content()
    assert 'dir="rtl"' in html


def test_html_part_escapes_the_body(db: Session):
    """An authored template must not be able to inject markup."""
    row = _queue(db, body="<script>alert(1)</script>", language="en")
    html = [
        part for part in mailer.build_message(row).walk()
        if part.get_content_type() == "text/html"
    ][0].get_content()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_content_language_survives_the_multipart_conversion(db: Session):
    """add_alternative moves content headers into a subpart, so this is set last."""
    row = _queue(db, language="ar")
    assert mailer.build_message(row)["Content-Language"] == "ar"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_successful_send_marks_the_row(db: Session):
    row = _queue(db)
    transport = RecordingTransport()

    result = mailer.send_pending(db, transport=transport)
    db.commit()

    assert result.sent == 1
    assert len(transport.messages) == 1
    assert row.sent_dt is not None
    assert row.error_detail is None


def test_a_sent_row_is_never_sent_again(db: Session):
    """At-most-once: the core delivery guarantee (Part II §5)."""
    _queue(db)
    transport = RecordingTransport()

    mailer.send_pending(db, transport=transport)
    db.commit()
    mailer.send_pending(db, transport=transport)
    db.commit()

    assert len(transport.messages) == 1


def test_failure_is_recorded_not_swallowed(db: Session):
    row = _queue(db)
    result = mailer.send_pending(db, transport=FailingTransport())
    db.commit()

    assert result.failed == 1
    assert row.sent_dt is None
    assert row.attempt_count == 1
    assert "ConnectionRefusedError" in row.error_detail


def test_backoff_holds_a_failed_row_before_retrying(db: Session):
    """A temporarily down server is retried patiently, not hammered."""
    _queue(db)
    transport = FailingTransport()

    mailer.send_pending(db, transport=transport)
    db.commit()
    assert transport.attempts == 1

    # Immediately after, the row is not due.
    mailer.send_pending(db, transport=transport)
    db.commit()
    assert transport.attempts == 1, "backoff prevented an immediate retry"

    # Past the first backoff window it is.
    later = utcnow() + dt.timedelta(minutes=mailer.BACKOFF_MINUTES[0] + 1)
    mailer.send_pending(db, transport=transport, now=later)
    db.commit()
    assert transport.attempts == 2


def test_a_row_is_abandoned_after_max_attempts(db: Session):
    """A permanently bad address must stop being retried forever."""
    row = _queue(db)
    transport = FailingTransport()

    moment = utcnow()
    for _ in range(mailer.MAX_ATTEMPTS):
        mailer.send_pending(db, transport=transport, now=moment)
        db.commit()
        moment += dt.timedelta(hours=6)

    assert row.attempt_count == mailer.MAX_ATTEMPTS
    assert row.scd_active_flag is False, "closed so it stops being picked up"
    assert row.error_detail is not None, "and its reason stays on the record"

    # It is no longer offered for sending.
    assert mailer.pending(db, now=moment) == []


def test_attempt_is_counted_before_sending(db: Session):
    """If the process dies mid-send the attempt still counts, so a message can
    never be retried indefinitely."""
    row = _queue(db)

    class Exploding(mailer.Transport):
        def send(self, message):
            assert row.attempt_count == 1, "claimed before the send was tried"
            raise RuntimeError("boom")

    mailer.send_pending(db, transport=Exploding())
    db.commit()
    assert row.attempt_count == 1


def test_summary_counts_each_state(db: Session):
    sent_row = _queue(db, idempotency_key="a")
    _queue(db, idempotency_key="b")

    mailer.send_pending(db, transport=RecordingTransport(), limit=1)
    db.commit()

    summary = mailer.outbox_summary(db)
    assert summary["total"] == 2
    assert summary["sent"] == 1
    assert summary["queued"] == 1


def test_console_transport_is_the_default_without_smtp(monkeypatch):
    """Development must not record a delivery failure for every queued email."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "mail_transport", "console")
    assert isinstance(mailer.default_transport(), mailer.ConsoleTransport)

    monkeypatch.setattr(settings, "mail_transport", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    assert type(mailer.default_transport()) is mailer.Transport


# ---------------------------------------------------------------------------
# Queueing (Part II §5)
# ---------------------------------------------------------------------------


def test_duplicate_queue_does_not_discard_other_pending_work(db: Session):
    """A duplicate must unwind only its own insert.

    Regression: queue_template_email used to call db.rollback() on a duplicate
    key, which threw away the caller's entire transaction — a previously queued
    email, an order being placed, anything else pending. It now uses a
    SAVEPOINT.
    """
    from app.db.base import utcnow
    from app.models.marketing import EmailTemplate
    from app.services.email import queue_template_email

    db.add(
        EmailTemplate(
            template_code="welcome",
            subject_ar="أهلاً", subject_en="Welcome",
            body_ar="مرحباً", body_en="Hello",
            scd_active_from=utcnow(),
        )
    )
    db.commit()

    first = queue_template_email(
        db, "welcome", recipient="a@example.com", language="en",
        idempotency_key="shared-key",
    )
    assert first is not None

    # Same key: expected to be refused, and must leave `first` intact.
    duplicate = queue_template_email(
        db, "welcome", recipient="a@example.com", language="en",
        idempotency_key="shared-key",
    )
    assert duplicate is None

    # A different key in the same transaction still works afterwards.
    third = queue_template_email(
        db, "welcome", recipient="b@example.com", language="en",
        idempotency_key="other-key",
    )
    assert third is not None

    db.commit()

    rows = db.scalars(select(EmailOutbox)).all()
    keys = {row.idempotency_key for row in rows}
    assert keys == {"shared-key", "other-key"}, (
        "the first row survived the duplicate attempt"
    )
