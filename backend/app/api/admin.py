import os
import threading

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.config import get_settings
from app.db import SessionLocal, get_db, is_postgres
from app.freshness import data_freshness
from app.models import NavPoint, Scheme

router = APIRouter(tags=["admin"])
_jobs: dict[str, dict] = {}


def _background(name: str, fn) -> dict:
    if _jobs.get(name, {}).get("status") == "running":
        return _jobs[name]
    _jobs[name] = {"status": "running"}

    def runner():
        db = SessionLocal()
        try:
            _jobs[name] = {"status": "done", "result": fn(db)}
        except Exception as exc:  # surfaced via /admin/jobs
            _jobs[name] = {"status": "failed", "error": str(exc)}
        finally:
            db.close()

    threading.Thread(target=runner, daemon=True).start()
    return _jobs[name]


@router.get("/meta/data-status")
def data_status(db: Session = Depends(get_db)):
    freshness = data_freshness(db)
    return {
        "database": "postgres" if is_postgres() else "sqlite",
        "schemes": db.scalar(select(func.count()).select_from(Scheme)),
        "investable_schemes": db.scalar(select(func.count()).select_from(Scheme).where(Scheme.in_universe.is_(True))),
        "nav_points": db.scalar(select(func.count()).select_from(NavPoint)),
        "latest_nav_date": freshness["latest_nav_date"],
        "metrics_as_of": freshness["metrics_as_of"],
        "freshness": freshness,
        "copilot_provider": get_settings().copilot_provider,
        "copilot_configured": bool(os.environ.get("OPENAI_API_KEY") if get_settings().copilot_provider == "openai"
                                   else os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
    }


@router.post("/admin/sync/scheme-master", dependencies=[Depends(require_admin)])
def sync_master():
    from app.data.amfi import sync_scheme_master

    return _background("scheme_master", sync_scheme_master)


@router.post("/admin/sync/nav-history", dependencies=[Depends(require_admin)])
def sync_history(years: int | None = None):
    from app.data.amfi import backfill_history

    return _background("nav_history", lambda db: backfill_history(db, years=years))


@router.post("/admin/metrics/recompute", dependencies=[Depends(require_admin)])
def recompute():
    from app.cli import compute_all_metrics

    return _background("metrics", compute_all_metrics)


@router.get("/admin/jobs", dependencies=[Depends(require_admin)])
def jobs():
    return _jobs
