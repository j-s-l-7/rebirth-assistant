"""Liveness and readiness.

/api/health   - the process is up (no DB call). Render's health check: keep it this cheap, or a
                slow database or stale data would make Render restart a working instance.
/api/health/ready - the database answers and the fund data is fresh (app/freshness.py); 503
                otherwise. Point an external uptime monitor here to be alerted to stale data.
"""

import logging

from fastapi import APIRouter, Response
from sqlalchemy import text

from app import __version__
from app.db import SessionLocal
from app.freshness import data_freshness
from app.telemetry import log_event

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
def ready(response: Response):
    freshness, database = None, "ok"
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            freshness = data_freshness(db)
    except Exception as exc:  # never echo driver errors: they can name hosts and users
        database = "unreachable"
        log_event("health.db_unreachable", logging.ERROR, exc_type=type(exc).__name__)
    ok = database == "ok" and freshness is not None and not freshness["stale"]
    response.status_code = 200 if ok else 503
    return {"ready": ok, "database": database, "data": freshness, "version": __version__}
