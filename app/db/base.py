"""Declarative base and the data-modelling conventions from Part II §1.

The conventions are enforced here rather than documented and hoped for:

* ``TRX_`` tables are insert-only. Attempting to UPDATE or DELETE a mapped
  ``TrxBase`` row raises — the ORM refuses at flush time, so a stray
  ``session.delete()`` fails loudly instead of quietly rewriting history.
* ``SCD_``/``LKP_`` tables carry the four mandated SCD columns via
  :class:`SCDMixin` and are closed, never deleted. :func:`active_only` and
  :func:`as_of` are *the* reusable "as of" query patterns — no query
  reinvents point-in-time logic.
* Every model declares ``__grain__``: one sentence describing what a single
  row represents. ``tests/test_conventions.py`` fails the build if one is
  missing, so the data dictionary can never drift from the schema.

Identifier casing: all identifiers are lowercase ``snake_case``. PostgreSQL
folds unquoted identifiers to lowercase, so lowercase storage keeps the schema
portable (Part II §1) — ``SELECT * FROM TRX_ORDER`` still resolves, because SQL
identifiers are case-insensitive. The prefixes and suffixes the spec mandates
(``TRX_``/``SCD_``/``LKP_``, ``pk_``/``fk_``, ``_id``/``_dt``/``_flag``/``_amt``)
are what carry meaning, and they are preserved exactly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.types import TypeDecorator

# Deterministic constraint names so Alembic autogenerate produces stable, and
# therefore reviewable, migrations (Part II §3).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Money: JOD is a 3-decimal currency (fils). Prices are stored in JOD only —
# USD is display-only conversion (Part I §1.1). Never a float.
MONEY = Numeric(14, 3)
RATE = Numeric(12, 6)


def utcnow() -> dt.datetime:
    """All timestamps are stored in UTC; local time is a presentation concern."""
    return dt.datetime.now(dt.timezone.utc)


class UtcDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC in Python.

    SQLite has no native timezone support: it stores what it is given and hands
    back a *naive* ``datetime``. PostgreSQL with ``timestamptz`` hands back an
    aware one. Without this, the same expression —
    ``discount.ends_dt <= utcnow()`` — works on PostgreSQL and raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` on
    SQLite, which is exactly the kind of engine difference Part II §1 asks the
    schema to stay portable across.

    Fixing it once at the type level means no comparison site anywhere in the
    application has to remember to normalise, and the "all timestamps are UTC"
    rule is true of the objects in memory, not just of the bytes on disk.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect) -> dt.datetime | None:
        if value is None:
            return None
        # A naive value reaching the database is a bug upstream; assume UTC
        # rather than silently writing local time.
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def process_result_value(self, value: dt.datetime | None, dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


def ensure_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Coerce a datetime to aware UTC.

    Rarely needed now that :class:`UtcDateTime` normalises at the boundary —
    keep it for values arriving from outside the ORM, such as parsed form input.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: One sentence: what does a single row of this table represent?
    __grain__: str = ""

    type_annotation_map = {
        Decimal: MONEY,
        dt.datetime: UtcDateTime,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = self.__mapper__.primary_key[0].name
        return f"<{type(self).__name__} {pk}={getattr(self, pk, None)!r}>"


# ---------------------------------------------------------------------------
# Column factories — keep the suffix convention impossible to get wrong.
# ---------------------------------------------------------------------------


def pk(table: str) -> Mapped[int]:
    """Surrogate primary key named ``pk_<table>_id``."""
    return mapped_column(f"pk_{table}_id", Integer, primary_key=True, autoincrement=True)


def fk(
    table: str,
    column: str,
    *,
    nullable: bool = False,
    ondelete: str | None = None,
    index: bool = True,
    **kw: Any,
) -> Mapped[int]:
    """Foreign key named ``fk_<table>_id``, indexed by default (Part II §2)."""
    return mapped_column(
        f"fk_{table}_id",
        Integer,
        ForeignKey(column, ondelete=ondelete),
        nullable=nullable,
        index=index,
        **kw,
    )


def bilingual(name: str, length: int = 255, *, nullable: bool = False) -> tuple[Any, Any]:
    """Any value that can differ by language gets separate AR/EN columns (Part I §1).

    Returns the ``(arabic, english)`` pair so models read as one declaration.
    """
    return (
        mapped_column(f"{name}_ar", String(length), nullable=nullable),
        mapped_column(f"{name}_en", String(length), nullable=nullable),
    )


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class SCDMixin:
    """The four mandated SCD columns (Part II §1).

    Rows are *closed* (``scd_active_to`` set, ``scd_active_flag`` cleared), never
    hard-deleted — which is also how every "nothing is ever deleted" business
    rule in Part I §2 is implemented.
    """

    scd_active_flag: Mapped[bool] = mapped_column(
        "scd_active_flag", Boolean, nullable=False, default=True, index=True
    )
    scd_active_from: Mapped[dt.datetime] = mapped_column(
        "scd_active_from", UtcDateTime, nullable=False, default=utcnow, index=True
    )
    scd_active_to: Mapped[dt.datetime | None] = mapped_column(
        "scd_active_to", UtcDateTime, nullable=True, index=True
    )
    scd_changed_by: Mapped[int | None] = mapped_column(
        "scd_changed_by", Integer, nullable=True,
        comment="Acting user id. Deliberately not a live FK: staff records are "
                "immutable historical references (Part I §2.2).",
    )

    def close(self, *, changed_by: int | None = None, at: dt.datetime | None = None) -> None:
        """Close this version. The replacement row is inserted by the caller."""
        self.scd_active_flag = False
        self.scd_active_to = at or utcnow()
        if changed_by is not None:
            self.scd_changed_by = changed_by


class CreatedMixin:
    """Insert-time provenance for transactional rows."""

    created_dt: Mapped[dt.datetime] = mapped_column(
        "created_dt", UtcDateTime, nullable=False, default=utcnow, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        "created_by", Integer, nullable=True,
        comment="Acting user id; immutable historical reference, not a live FK.",
    )


class TrxBase(Base, CreatedMixin):
    """Marker base for ``TRX_`` tables: insert-only, enforced at flush time.

    A correction to a transactional fact is a *new compensating row*, never an
    edit of the original — that is what makes the audit trail trustworthy.
    """

    __abstract__ = True


class ImmutableRowError(RuntimeError):
    """Raised when something tries to update or delete an insert-only row."""


@event.listens_for(Session, "before_flush")
def _forbid_trx_mutation(session: Session, flush_context: Any, instances: Any) -> None:
    for obj in session.dirty:
        if isinstance(obj, TrxBase) and session.is_modified(obj, include_collections=False):
            raise ImmutableRowError(
                f"{type(obj).__name__} is a TRX_ (insert-only) table — rows are never "
                f"updated. Insert a compensating row instead (Part II §1)."
            )
    for obj in session.deleted:
        if isinstance(obj, TrxBase):
            raise ImmutableRowError(
                f"{type(obj).__name__} is a TRX_ (insert-only) table — rows are never "
                f"deleted (Part II §1)."
            )
        if isinstance(obj, SCDMixin):
            raise ImmutableRowError(
                f"{type(obj).__name__} is an SCD_/LKP_ table — close the row with "
                f".close() instead of deleting it (Part II §1)."
            )


# ---------------------------------------------------------------------------
# The reusable "as of" query patterns (Part II §1 — documented, not reinvented)
# ---------------------------------------------------------------------------


def active_only(stmt: Any, model: type[SCDMixin]) -> Any:
    """Current state: the row version in effect right now.

    Use for anything a user is looking at *now* — the live catalog, current
    permissions, today's exchange rate.
    """
    return stmt.where(model.scd_active_flag.is_(True))


def as_of(stmt: Any, model: type[SCDMixin], moment: dt.datetime) -> Any:
    """Point-in-time rollback: the row version in effect at ``moment``.

    Use for anything that must reproduce history — reprinting a past invoice,
    reporting margins at the rate that applied at time of sale (Part I §1.1).
    """
    return stmt.where(
        model.scd_active_from <= moment,
        (model.scd_active_to > moment) | (model.scd_active_to.is_(None)),
    )
