"""Dialect-aware bulk writes (Postgres/Supabase or SQLite)."""

import csv
import io
from collections.abc import Iterable, Sequence
from itertools import islice

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings


def _chunks(rows: Sequence[dict], size: int) -> Iterable[Sequence[dict]]:
    it = iter(rows)
    while chunk := list(islice(it, size)):
        yield chunk


def upsert(db: Session, model, rows: Sequence[dict], conflict_cols: list[str], update_cols: list[str] | None = None) -> int:
    """INSERT ... ON CONFLICT DO UPDATE/NOTHING in parameter-safe chunks."""
    if not rows:
        return 0
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    size = max(1, 30_000 // max(1, len(rows[0])))  # stay under bind-parameter limits
    for chunk in _chunks(rows, size):
        stmt = insert(model).values(list(chunk))
        if update_cols:
            stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_={c: stmt.excluded[c] for c in update_cols})
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
        db.execute(stmt)
    return len(rows)


def copy_nav_points(db: Session, rows: Sequence[tuple[int, object, float]]) -> int:
    """Fast path for NAV history on Postgres: COPY into a temp table, then merge.

    Falls back to `upsert` on other dialects. Rows are (amfi_code, nav_date, nav).
    """
    from app.models import NavPoint

    if not rows:
        return 0
    if db.get_bind().dialect.name != "postgresql":
        return upsert(db, NavPoint, [{"amfi_code": c, "nav_date": d, "nav": n} for c, d, n in rows], ["amfi_code", "nav_date"])

    schema = get_settings().db_schema
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    buf.seek(0)

    db.execute(text("CREATE TEMP TABLE IF NOT EXISTS _nav_stage (amfi_code int, nav_date date, nav numeric(14,4)) ON COMMIT DELETE ROWS"))
    raw = db.connection().connection  # DB-API connection inside the current transaction
    with raw.cursor() as cur:
        cur.copy_expert("COPY _nav_stage (amfi_code, nav_date, nav) FROM STDIN WITH (FORMAT csv)", buf)
    db.execute(
        text(
            f'INSERT INTO "{schema}".nav_history (amfi_code, nav_date, nav) '
            "SELECT amfi_code, nav_date, nav FROM _nav_stage "
            f'WHERE amfi_code IN (SELECT amfi_code FROM "{schema}".scheme) '
            "ON CONFLICT (amfi_code, nav_date) DO NOTHING"
        )
    )
    return len(rows)
