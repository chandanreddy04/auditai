"""
Engine/session setup, same shape as InvoiceIQ's: a single engine, a
SessionLocal factory, and a get_db() generator dependency FastAPI
routes use so every request gets its own session that always closes,
success or exception.
"""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.core.config import DATABASE_URL
from app.models.models import Base

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Real, live-found gap: Base.metadata.create_all() only creates tables
# that don't exist yet - it never alters an EXISTING table to add a
# newly-introduced column. Adding Document.audit_area broke every
# upload against any database this app had already been run against
# (local dev, and would have broken the live Render deployment too,
# the moment it redeployed against its existing Postgres database).
# This project deliberately has no migration framework (a size-
# appropriate simplicity choice, same reasoning as everywhere else in
# this app) - this is the honest, minimal fix: a short list of columns
# introduced after a table already existed in the wild, added if
# missing, safe to call on every startup. Add a new (table, column,
# ddl) entry here any time a future column faces the same gap.
_RETROFIT_COLUMNS = [
    ("documents", "audit_area", "VARCHAR(20) DEFAULT 'uncategorized'"),
]


def _apply_column_retrofits() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl_type in _RETROFIT_COLUMNS:
            if table not in existing_tables:
                continue  # create_all() above already created it with every current column
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            if column in existing_columns:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


# A second, sharper version of the same gap, Postgres-only: SQLAlchemy's
# Enum() column type creates a native Postgres ENUM TYPE (e.g. `pbcstatus`)
# with a fixed, frozen set of allowed values at the time the table was
# first created. Adding VALIDATED/FOLLOW_UP to PBCStatus in Python code
# does nothing to that already-existing Postgres type - confirmed live:
# every /pbc/{id}/validate call 500'd in production with an invalid-enum-
# value error, while the identical code worked fine locally, because
# SQLite has no native enum type (Enum() is just a plain column there
# with no such gap). `ADD VALUE IF NOT EXISTS` is idempotent on its own
# (PG 9.6+), so no inspection is needed first, unlike the column case
# above. Run outside any transaction - ALTER TYPE ... ADD VALUE has real
# restrictions inside a multi-statement transaction on some PG versions,
# and there's no reason to risk it when autocommit is just as safe here.
_RETROFIT_ENUM_VALUES = [
    ("pbcstatus", "validated"),
    ("pbcstatus", "follow_up"),
]


def _apply_enum_value_retrofits() -> None:
    if engine.dialect.name != "postgresql":
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for type_name, value in _RETROFIT_ENUM_VALUES:
            conn.execute(text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'"))


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_column_retrofits()
    _apply_enum_value_retrofits()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
