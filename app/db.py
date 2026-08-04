"""Database engine, session factory, and declarative base.

Uses SQLAlchemy 2.x. The engine is built from ``DATABASE_URL`` so the exact
same code runs against SQLite (POC) and PostgreSQL (server deploy).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# SQLite needs a special connect arg when used across threads (the scheduler
# runs in a background thread). PostgreSQL ignores this branch entirely.
_connect_args: dict[str, object] = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


#: Columns added after the first release, applied to existing DBs on startup.
#: (name -> SQL type) — kept in sync with the ORM models. New tables are handled
#: by create_all; only *added columns on existing tables* need this.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "hosts": {
        "cpu_pct": "FLOAT",
        "mem_pct": "FLOAT",
        "disk_pct": "FLOAT",
        "metrics": "JSON",
    },
    "alerts": {
        "state": "VARCHAR(64)",
        "dedup_key": "VARCHAR(255)",
        "metric_missing": "BOOLEAN",
        "monitor_name": "VARCHAR(512)",
    },
}


def _ensure_columns() -> None:
    """Add any missing columns to existing tables (tiny forward-only migration).

    ``create_all`` never alters existing tables, so a schema that grew a column
    (e.g. the Capacity metrics) would break queries against an older DB. Both
    SQLite and PostgreSQL support ``ALTER TABLE ... ADD COLUMN``; we only add
    columns that are absent, so this is safe to run on every startup.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all will build it fresh with all columns
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in present:
                    conn.execute(
                        text(f'ALTER TABLE {table} ADD COLUMN {name} {sql_type}')
                    )


def init_db() -> None:
    """Create all tables, then apply small column migrations. Idempotent."""
    # Import models so they register with the metadata before create_all.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
