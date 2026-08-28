"""The Part II §1 data-modelling standards, as executable rules.

A convention that lives only in a document drifts the moment someone is in a
hurry. These tests fail the build instead, so the schema and the standard cannot
disagree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.types import TypeDecorator

from app.db.base import SCDMixin, TrxBase
from app.models import Base

TABLE_PREFIXES = ("trx_", "scd_", "lkp_")
SCD_REQUIRED_COLUMNS = {
    "scd_active_flag",
    "scd_active_from",
    "scd_active_to",
    "scd_changed_by",
}
COLUMN_SUFFIXES = ("_id", "_dt", "_flag", "_amt")

ALL_MODELS = [m.class_ for m in Base.registry.mappers]
ALL_TABLES = list(Base.metadata.tables.values())


def _base_type(column):
    """The underlying SQL type, seeing through any ``TypeDecorator``.

    ``UtcDateTime`` wraps ``DateTime(timezone=True)`` so SQLite returns aware
    datetimes. Without unwrapping here, every datetime column would quietly stop
    being checked by the tests below — which is worse than never having had
    them, because the suite would still report green.
    """
    type_ = column.type
    while isinstance(type_, TypeDecorator):
        type_ = type_.impl_instance if hasattr(type_, "impl_instance") else type_.impl
    return type_


def _type_name(column) -> str:
    return type(_base_type(column)).__name__.upper()


def _model_ids(models):
    return [m.__name__ for m in models]


@pytest.mark.parametrize("model", ALL_MODELS, ids=_model_ids(ALL_MODELS))
def test_table_carries_a_type_prefix(model):
    """Every table declares its type: transactional, dimension or lookup."""
    assert model.__tablename__.startswith(TABLE_PREFIXES), (
        f"{model.__name__} -> '{model.__tablename__}' must start with one of "
        f"{TABLE_PREFIXES} (Part II §1)."
    )


@pytest.mark.parametrize("model", ALL_MODELS, ids=_model_ids(ALL_MODELS))
def test_every_table_documents_its_grain(model):
    """One row represents *what*? If nobody can answer, the table is wrong."""
    assert model.__grain__, (
        f"{model.__name__} is missing __grain__ — every table documents what one "
        f"row represents (Part II §1)."
    )


@pytest.mark.parametrize("model", ALL_MODELS, ids=_model_ids(ALL_MODELS))
def test_scd_and_lkp_tables_have_the_scd_columns(model):
    """SCD_/LKP_ rows are closed, never deleted — which needs all four columns."""
    if not model.__tablename__.startswith(("scd_", "lkp_")):
        return
    columns = {c.name for c in inspect(model).columns}
    missing = SCD_REQUIRED_COLUMNS - columns
    assert not missing, (
        f"{model.__name__} is an SCD_/LKP_ table missing {sorted(missing)}. "
        f"Either add SCDMixin or rename the table to reflect what it is."
    )


@pytest.mark.parametrize("model", ALL_MODELS, ids=_model_ids(ALL_MODELS))
def test_trx_tables_are_insert_only(model):
    """A TRX_ table must inherit TrxBase, which is what enforces insert-only."""
    if not model.__tablename__.startswith("trx_"):
        return
    assert issubclass(model, TrxBase), (
        f"{model.__name__} is named trx_* but does not inherit TrxBase, so nothing "
        f"stops it being updated or deleted."
    )


@pytest.mark.parametrize("model", ALL_MODELS, ids=_model_ids(ALL_MODELS))
def test_trx_tables_never_carry_scd_columns(model):
    """Insert-only and slowly-changing are mutually exclusive; a table that
    claims both is telling you the grain was never decided."""
    if not model.__tablename__.startswith("trx_"):
        return
    assert not issubclass(model, SCDMixin), (
        f"{model.__name__} is both TRX_ and SCD_. Pick one."
    )


@pytest.mark.parametrize("model", ALL_MODELS, ids=_model_ids(ALL_MODELS))
def test_primary_key_follows_the_pk_convention(model):
    mapper = inspect(model)
    pk_columns = list(mapper.primary_key)
    assert len(pk_columns) == 1, (
        f"{model.__name__} has a composite primary key. Use a surrogate "
        f"pk_<table>_id and enforce the business key with a unique constraint."
    )
    expected = f"pk_{model.__tablename__.split('_', 1)[1]}_id"
    assert pk_columns[0].name == expected, (
        f"{model.__name__} primary key is '{pk_columns[0].name}', expected '{expected}'."
    )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_foreign_keys_follow_the_fk_convention(table):
    for column in table.columns:
        if column.foreign_keys:
            assert column.name.startswith("fk_") and column.name.endswith("_id"), (
                f"{table.name}.{column.name} is a foreign key and must be named "
                f"fk_<name>_id (Part II §1)."
            )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_foreign_keys_are_indexed(table):
    """Part II §2: index every foreign key."""
    indexed = {
        tuple(c.name for c in index.columns)[0]
        for index in table.indexes
        if len(index.columns) >= 1
    }
    indexed |= {c.name for c in table.primary_key.columns}
    # A leading column of any composite index counts as covered.
    for index in table.indexes:
        cols = list(index.columns)
        if cols:
            indexed.add(cols[0].name)

    for column in table.columns:
        if column.foreign_keys and not (column.index or column.unique):
            assert column.name in indexed, (
                f"{table.name}.{column.name} is an unindexed foreign key (Part II §2)."
            )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_column_names_are_lowercase_snake_case(table):
    for column in table.columns:
        assert column.name == column.name.lower(), (
            f"{table.name}.{column.name} is not lowercase (Part II §1)."
        )
        assert " " not in column.name and "-" not in column.name, (
            f"{table.name}.{column.name} is not snake_case."
        )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_no_json_columns(table):
    """Part II §1: nothing representable as rows/columns lives in a JSON blob."""
    for column in table.columns:
        type_name = _type_name(column)
        assert "JSON" not in type_name, (
            f"{table.name}.{column.name} is {type_name}. Anything tabular belongs "
            f"in relational tables (Part II §1)."
        )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_money_columns_are_never_floats(table):
    """Money is Numeric. A float total is a rounding bug waiting for an audit."""
    for column in table.columns:
        if column.name.endswith("_amt"):
            type_name = _type_name(column)
            assert type_name in {"NUMERIC", "DECIMAL"}, (
                f"{table.name}.{column.name} is {type_name}; _amt columns must be Numeric."
            )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_datetime_columns_are_timezone_aware(table):
    """All timestamps are UTC (Part II §1); a naive column loses that fact."""
    for column in table.columns:
        if _type_name(column) == "DATETIME":
            assert getattr(_base_type(column), "timezone", False), (
                f"{table.name}.{column.name} is a naive DATETIME. Use "
                f"DateTime(timezone=True) and store UTC."
            )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_flag_columns_are_boolean(table):
    for column in table.columns:
        if column.name.endswith("_flag"):
            assert _type_name(column) == "BOOLEAN", (
                f"{table.name}.{column.name} ends in _flag but is not Boolean."
            )


@pytest.mark.parametrize("table", ALL_TABLES, ids=[t.name for t in ALL_TABLES])
def test_datetime_columns_are_named_dt(table):
    """A reader should know a column holds a timestamp from its name alone."""
    # scd_active_from/scd_active_to are named verbatim by Part II §1 and are
    # deliberately exempt — the standard names them, so the standard wins.
    allowed_without_suffix = {"scd_active_from", "scd_active_to"}
    for column in table.columns:
        if _type_name(column) == "DATETIME":
            assert column.name.endswith("_dt") or column.name in allowed_without_suffix, (
                f"{table.name}.{column.name} is a DATETIME and should end in _dt "
                f"(Part II §1)."
            )
