"""Structured logging must never crash the caller (Part II §5).

Python's logging raises ``KeyError`` if ``extra=`` carries a reserved
``LogRecord`` attribute. Several of those names — ``module``, ``filename``,
``name`` — are ordinary words in this domain, so the collision is easy to walk
into, and the consequence is wildly disproportionate: an observability call
taking down a checkout. This pins the guard in place.
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging import _RESERVED, configure_logging, get_logger

RESERVED_SAMPLE = ["module", "filename", "name", "args", "lineno", "process"]


@pytest.fixture(autouse=True)
def _configured():
    configure_logging()


@pytest.mark.parametrize("key", RESERVED_SAMPLE)
def test_reserved_extra_key_does_not_raise(key: str, caplog):
    log = get_logger("test.reserved")
    with caplog.at_level(logging.INFO):
        log.info("probe", extra={key: "value"})
    assert caplog.records, "the record should still be emitted"


def test_colliding_key_is_prefixed_not_dropped(caplog):
    """Degrade, don't discard — the value still reaches the log."""
    log = get_logger("test.prefix")
    with caplog.at_level(logging.INFO):
        log.info("probe", extra={"module": "orders"})

    record = caplog.records[-1]
    assert getattr(record, "ctx_module") == "orders"
    # The real LogRecord attribute is untouched.
    assert record.module != "orders"


def test_non_colliding_keys_pass_through_unchanged(caplog):
    log = get_logger("test.passthrough")
    with caplog.at_level(logging.INFO):
        log.info("probe", extra={"order_id": 42, "permission_module": "orders"})

    record = caplog.records[-1]
    assert record.order_id == 42
    assert record.permission_module == "orders"


def test_reserved_set_matches_the_stdlib():
    """If Python adds a LogRecord attribute, the guard should already cover it."""
    actual = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)
    assert actual <= set(_RESERVED)
