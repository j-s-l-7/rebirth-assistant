"""Database wiring.

Production/staging: Supabase Postgres via DATABASE_URL (session pooler on :5432 or
transaction pooler on :6543). All RA tables live in a dedicated Postgres schema
(`DB_SCHEMA`, default `mf`) so they never collide with other tables in a shared
project and are not exposed through Supabase's auto-generated REST API.
Local dev/tests: SQLite file (no schema support, so the schema map is skipped).
"""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str, schema: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if parsed.database:
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine

    if parsed.drivername in ("postgres", "postgresql"):
        parsed = parsed.set(drivername="postgresql+psycopg2")
    # Supabase's transaction pooler (:6543) already pools server-side; holding our own
    # pool on top of it just pins pooler slots, so connect per checkout instead.
    pool_kwargs = {"poolclass": NullPool} if parsed.port == 6543 else {"pool_size": 5, "max_overflow": 5}
    engine = create_engine(
        parsed,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 15, "application_name": "ra-backend"},
        **pool_kwargs,
    )
    return engine.execution_options(schema_translate_map={None: schema})


_settings = get_settings()
engine = _make_engine(_settings.database_url, _settings.db_schema)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    if is_postgres():
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{_settings.db_schema}"'))
    Base.metadata.create_all(engine)
    _add_missing_columns()
    if is_postgres():
        # Full-text search over document passages: generated tsvector + GIN index.
        schema = _settings.db_schema
        with engine.begin() as conn:
            conn.execute(text(
                f'ALTER TABLE "{schema}".document_chunk ADD COLUMN IF NOT EXISTS tsv tsvector GENERATED ALWAYS AS '
                "(setweight(to_tsvector('english', coalesce(heading, '')), 'A') || setweight(to_tsvector('english', text), 'B')) STORED"
            ))
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS document_chunk_tsv_idx ON "{schema}".document_chunk USING GIN (tsv)'))


def _add_missing_columns() -> None:
    """create_all doesn't alter existing tables: add any nullable model column a table lacks
    (lightweight forward-only migration for additive changes)."""
    insp = inspect(engine)
    schema = _settings.db_schema if is_postgres() else None
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name, schema=schema):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name, schema=schema)}
        for col in table.columns:
            if col.name in existing or not col.nullable:
                continue
            ddl_type = col.type.compile(dialect=engine.dialect)
            target = f'"{schema}"."{table.name}"' if schema else f'"{table.name}"'
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {target} ADD COLUMN "{col.name}" {ddl_type}'))


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
