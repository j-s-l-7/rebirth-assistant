"""Copy look-through holdings from the local DB to production Postgres (Supabase).

Dry run by default: reads only. `apply=True` adds the missing columns (never drops anything), then replaces
each (fund, disclosure month) portfolio in one transaction, so a re-run gives the same result. The URL comes
from SUPABASE_DATABASE_URL (environment, or backend/.env.supabase which is git-ignored). A local file path is
never copied: source_url becomes the AMC's public page from portfolio_sources, or NULL.
"""

import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data.portfolio_sources import PORTFOLIO_SOURCES
from app.db import _make_engine
from app.models import Scheme, SchemeHolding

_ADDED = {"industry": "VARCHAR(120)", "asset_class": "VARCHAR(20)", "market_value_inr": "DOUBLE PRECISION",
          "quantity": "DOUBLE PRECISION", "rating": "VARCHAR(40)", "source_url": "TEXT"}
_COLS = [c.name for c in SchemeHolding.__table__.columns]
_BATCH = 2000


def target_engine() -> Engine:
    url = os.environ.get("SUPABASE_DATABASE_URL") or dotenv_values(Path(__file__).resolve().parents[2] / ".env.supabase").get("SUPABASE_DATABASE_URL")
    if not url or not url.startswith("postgres"):
        raise SystemExit("Put SUPABASE_DATABASE_URL=<Supabase session pooler URI> in backend/.env.supabase")
    return _make_engine(url, get_settings().db_schema)


def _public_source(amc_name: str) -> str | None:
    return next((s["page_url"] for k, s in PORTFOLIO_SOURCES.items() if k.lower() in amc_name.lower()), None)


def push_holdings(src: Session, dst: Engine, apply: bool = False) -> dict:
    table = SchemeHolding.__tablename__
    schema = get_settings().db_schema if dst.dialect.name == "postgresql" else None
    insp = inspect(dst)
    have = {c["name"] for c in insp.get_columns(table, schema=schema)} if insp.has_table(table, schema=schema) else set()
    missing = [c for c in _ADDED if c not in have]
    amc = dict(src.execute(select(Scheme.amfi_code, Scheme.amc_name)).all())
    with Session(dst) as d:
        known = set(d.scalars(select(Scheme.amfi_code)))
    all_rows = src.scalars(select(SchemeHolding)).all()
    rows = [r for r in all_rows if r.amfi_code in known]
    skipped = len(all_rows) - len(rows)
    pairs = sorted({(r.amfi_code, r.disclosure_month) for r in rows})
    out = {"rows": len(rows), "portfolios": len(pairs), "rows_for_funds_not_in_target": skipped,
           "columns_to_add": missing, "applied": False}
    if not apply:
        return out
    if dst.dialect.name == "postgresql":
        with dst.begin() as c:
            for col in missing:
                c.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS {col} {_ADDED[col]}'))
            c.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{table}_security_isin ON "{schema}"."{table}" (security_isin)'))
    with Session(dst) as d, d.begin():
        for code, month in pairs:
            d.execute(delete(SchemeHolding).where(SchemeHolding.amfi_code == code, SchemeHolding.disclosure_month == month))
        for i in range(0, len(rows), _BATCH):
            d.execute(SchemeHolding.__table__.insert(), [
                {**{c: getattr(r, c) for c in _COLS}, "source_url": _public_source(amc.get(r.amfi_code, ""))}
                for r in rows[i:i + _BATCH]])
    out["applied"] = True
    return out
