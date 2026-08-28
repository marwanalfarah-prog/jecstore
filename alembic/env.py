"""Alembic environment.

All schema changes go through migration files — never a manual ALTER TABLE in
production (Part II §3). The database URL is read from settings, so it comes
from the environment and is never committed.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings

# Importing the models package registers every table on Base.metadata.
from app.models import Base  # noqa: F401  (side-effectful import, by design)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate focused on tables this application owns."""
    if type_ == "table" and not name.startswith(("trx_", "scd_", "lkp_", "alembic_")):
        return False
    return True


def render_item(type_, obj, autogen_context) -> str | bool:
    """Render ``UtcDateTime`` as the plain SQLAlchemy type it wraps.

    ``UtcDateTime`` is a ``TypeDecorator`` over ``DateTime(timezone=True)`` — it
    only changes what Python gets back, never the DDL. Emitting the decorator
    name would make every migration import live application code, so a later
    refactor of ``app.db.base`` would break migrations that already ran in
    production. Migrations describe the database; they should not depend on the
    models that happened to generate them.
    """
    if type_ == "type" and type(obj).__name__ == "UtcDateTime":
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, so the same migration runs on SQLite and PostgreSQL.
            render_as_batch=settings.is_sqlite,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
