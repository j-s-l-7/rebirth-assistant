"""Operational commands.

    python -m app.cli init-db
    python -m app.cli sync-schemes            # AMFI scheme master + today's NAVs (run daily ~23:30 IST)
    python -m app.cli sync-history --years 6  # bulk NAV history from AMFI (first load)
    python -m app.cli repair-history          # re-fetch months that came back empty/partial
    python -m app.cli backfill-missing        # history for tracked schemes that have little or none
    python -m app.cli compute-metrics         # peer rankings for every category
    python -m app.cli sync-scheme-details     # AMFI scheme master details (launch dates, minimums, ISINs)
    python -m app.cli sync-ter --months 36    # AMFI total expense ratios (stored as change points)
    python -m app.cli sync-documents          # regulatory texts + AMFI monthly reports (originals + full text)
    python -m app.cli search-docs "QUERY"     # full-text search over stored documents
    python -m app.cli daily                   # all daily feeds, gap repair, universe refinement, rankings
    python -m app.cli load-disclosures FILE.csv
    python -m app.cli load-portfolios PATH_OR_URL [...]   # AMC monthly portfolio workbooks (.xls/.xlsx/.zip)
    python -m app.cli push-holdings [--apply]   # local holdings -> Supabase (needs SUPABASE_DATABASE_URL; dry run unless --apply)
    python -m app.cli stats
    python -m app.cli print-ddl > sql/schema.sql   # Postgres DDL for review (tables live in DB_SCHEMA)
"""

import argparse
import json
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, init_db


def compute_all_metrics(db: Session) -> dict:
    from app.analytics.navbook import NavBook
    from app.analytics.performance import rank_category
    from app.models import NavPoint, Scheme
    from app.reference.categories import CATEGORIES

    as_of = db.scalar(select(func.max(NavPoint.nav_date)))
    if as_of is None:
        return {"error": "No NAV history loaded"}
    done = {}
    for key, cat in CATEGORIES.items():
        if not cat.peer_group:
            continue
        n = db.scalar(select(func.count()).select_from(Scheme).where(Scheme.category_key == key, Scheme.in_universe.is_(True)))
        if not n:
            continue
        ranked = rank_category(db, NavBook(db), key, as_of)
        done[key] = sum(1 for m in ranked.values() if m.get("tier") in ("core", "satellite", "watch", "exit"))
    return {"as_of": as_of.isoformat(), "tiered_by_category": done}


def run_daily(db: Session) -> dict:
    """Every daily feed in dependency order. A failing step is recorded and the rest still
    run: one flaky AMFI endpoint must not stop the NAV refresh."""
    from app.data.amfi import ensure_history, repair_history, sync_scheme_master, tracked_codes
    from app.data.scheme_details import sync_scheme_details
    from app.data.ter import sync_ter
    from app.data.universe import refine_universe

    steps = [
        ("scheme_details", lambda: sync_scheme_details(db)),
        ("schemes", lambda: sync_scheme_master(db)),
        ("repair", lambda: repair_history(db)),
        ("new_schemes_history", lambda: {"backfilled": len(ensure_history(db, tracked_codes(db), min_points=30))}),
        ("ter", lambda: sync_ter(db, months=1)),
        ("universe", lambda: refine_universe(db)),
        ("metrics", lambda: compute_all_metrics(db)),
    ]
    out: dict = {}
    for name, step in steps:
        try:
            out[name] = step()
        except Exception as exc:  # noqa: BLE001 - recorded, and the remaining steps still run
            db.rollback()
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def load_portfolio_sources(db: Session, sources: list[str]) -> dict:
    """Load each file or URL in turn (URLs fetched one at a time, a few seconds apart); print a summary per file."""
    from pathlib import Path

    from app.data.portfolios import load_portfolios
    from app.data.ter import _NameMatcher
    from app.http import http_client, with_retries

    matcher = _NameMatcher(db, 95.0)
    files = []
    with http_client() as client:
        for i, src in enumerate(sources):
            if src.startswith(("http://", "https://")):
                if i:
                    time.sleep(3)
                try:
                    content = with_retries(f"portfolio {src}", lambda src=src: client.get(src).raise_for_status().content, attempts=2)
                except RuntimeError as exc:
                    files.append({"file": src, "error": str(exc)})
                    continue
            else:
                content = Path(src).read_bytes()
            res = load_portfolios(db, content, src, matcher)
            print(f"{src}: {res['schemes_parsed']} schemes parsed, {res['linked']} linked, {res['quarantined']} quarantined, "
                  f"{res['unlinked']} unlinked, {res['rows']} rows", flush=True)
            files.append(res)
    return {"files": files}


def print_ddl() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app import models  # noqa: F401
    from app.config import get_settings
    from app.db import Base

    schema = get_settings().db_schema
    dialect = postgresql.dialect()
    print(f"-- RA (Rebirth Assistant) schema for Supabase Postgres. Generated by `python -m app.cli print-ddl`.\n"
          f"-- The app creates these itself on startup (init_db); this file is for review.\n"
          f'CREATE SCHEMA IF NOT EXISTS "{schema}";\nSET search_path TO "{schema}";\n')
    for table in Base.metadata.sorted_tables:
        print(str(CreateTable(table).compile(dialect=dialect)).strip() + ";\n")
        for index in table.indexes:
            print(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
        if table.indexes:
            print()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("sync-schemes")
    h = sub.add_parser("sync-history")
    h.add_argument("--years", type=int, default=None)
    h.add_argument("--workers", type=int, default=4)
    sub.add_parser("repair-history")
    sub.add_parser("backfill-missing")
    sub.add_parser("compute-metrics")
    sub.add_parser("sync-scheme-details")
    t = sub.add_parser("sync-ter")
    t.add_argument("--months", type=int, default=36)
    sub.add_parser("refine-universe")
    docs = sub.add_parser("sync-documents")
    docs.add_argument("--monthly-reports", type=int, default=24)
    sd = sub.add_parser("search-docs")
    sd.add_argument("query")
    sd.add_argument("--limit", type=int, default=5)
    sub.add_parser("daily")
    d = sub.add_parser("load-disclosures")
    d.add_argument("path")
    lp = sub.add_parser("load-portfolios")
    lp.add_argument("sources", nargs="+", metavar="PATH_OR_URL")
    ph = sub.add_parser("push-holdings")
    ph.add_argument("--apply", action="store_true")
    sub.add_parser("stats")
    sub.add_parser("print-ddl")
    args = p.parse_args()

    if args.cmd == "print-ddl":
        print_ddl()
        return

    init_db()
    db = SessionLocal()
    t0 = time.time()
    try:
        if args.cmd == "init-db":
            out = {"ok": True}
        elif args.cmd == "sync-schemes":
            from app.data.amfi import sync_scheme_master

            out = sync_scheme_master(db)
        elif args.cmd == "sync-history":
            from app.data.amfi import backfill_history

            def progress(done, total, a, b, n):
                print(f"  [{done}/{total}] {a}..{b}: {n:,} NAVs", flush=True)

            out = backfill_history(db, years=args.years, workers=args.workers, progress=progress)
        elif args.cmd == "repair-history":
            from app.data.amfi import repair_history

            out = repair_history(db)
        elif args.cmd == "backfill-missing":
            from app.data.amfi import ensure_history, tracked_codes

            filled = ensure_history(db, tracked_codes(db), min_points=30)
            out = {"backfilled": len(filled)}
        elif args.cmd == "compute-metrics":
            out = compute_all_metrics(db)
        elif args.cmd == "sync-scheme-details":
            from app.data.scheme_details import sync_scheme_details

            out = sync_scheme_details(db)
        elif args.cmd == "sync-ter":
            from app.data.ter import sync_ter

            out = sync_ter(db, months=args.months, progress=lambda *a: print("  ", *a, flush=True))
        elif args.cmd == "refine-universe":
            from app.data.universe import refine_universe

            out = refine_universe(db)
        elif args.cmd == "sync-documents":
            from app.docs.sources import sync_documents

            out = sync_documents(db, monthly_reports=args.monthly_reports, progress=lambda *a: print("  ", *a, flush=True))
        elif args.cmd == "search-docs":
            from app.docs.search import search_documents

            out = {"results": search_documents(db, args.query, limit=args.limit)}
        elif args.cmd == "daily":
            out = run_daily(db)
        elif args.cmd == "load-disclosures":
            from pathlib import Path

            from app.data.disclosures import load_disclosure_csv

            out = {"rows": load_disclosure_csv(db, Path(args.path))}
        elif args.cmd == "load-portfolios":
            out = load_portfolio_sources(db, args.sources)
        elif args.cmd == "push-holdings":
            from app.data.push_holdings import push_holdings, target_engine

            out = push_holdings(db, target_engine(), apply=args.apply)
        else:
            from app.models import Investor, NavPoint, Scheme

            out = {
                "schemes": db.scalar(select(func.count()).select_from(Scheme)),
                "investable": db.scalar(select(func.count()).select_from(Scheme).where(Scheme.in_universe.is_(True))),
                "nav_points": db.scalar(select(func.count()).select_from(NavPoint)),
                "latest_nav": str(db.scalar(select(func.max(NavPoint.nav_date)))),
                "investors": db.scalar(select(func.count()).select_from(Investor)),
            }
        out["elapsed_s"] = round(time.time() - t0, 1)
        print(json.dumps(out, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
