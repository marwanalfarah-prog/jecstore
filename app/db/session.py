"""Engine and session management.

SQLite is the current engine, but every choice here is made so the swap to
PostgreSQL is a URL change (Part II §1). The one place the engines genuinely
differ is row locking, which matters directly for the stock-reservation race
condition in Part I §8 — that difference is handled explicitly in
``app/services/locking.py`` rather than papered over here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _build_engine() -> Engine:
    kwargs: dict = {"echo": settings.database_echo, "future": True}

    if settings.is_sqlite:
        db_path = settings.database_url.split("///", 1)[-1]
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        # Modest pool: the storefront is read-heavy and served by one app process.
        kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)

    return create_engine(settings.database_url, **kwargs)


engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record) -> None:
    """SQLite defaults are unsafe for this workload; fix them per connection."""
    if not settings.is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    # Foreign keys are OFF by default in SQLite — without this, the referential
    # integrity the schema declares would simply not be enforced.
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL lets readers proceed during a write, which keeps browsing responsive
    # while a checkout holds its reservation lock.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Wait rather than fail instantly when another writer holds the lock.
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts, seeds and background jobs."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
