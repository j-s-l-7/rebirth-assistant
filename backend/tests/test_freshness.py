"""Data freshness: business-day lag, the stale verdict, data-status and the readiness probe."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import freshness
from app.analytics.performance import MODEL_VERSION
from app.api import health
from app.main import app
from app.models import CategoryStat

NAV_DAY = date(2026, 10, 14)  # synthetic market's last NAV (a Wednesday)


@pytest.fixture
def ranked(db, market):
    """A ranking run on the synthetic market's last NAV date (shared DB: added once)."""
    key = "freshness_probe"
    if not db.query(CategoryStat).filter_by(category_key=key, as_of=NAV_DAY, model_version=MODEL_VERSION).first():
        db.add(CategoryStat(category_key=key, as_of=NAV_DAY, model_version=MODEL_VERSION, stats={}))
        db.commit()
    return NAV_DAY


def test_business_days_between_skips_weekends():
    fri, mon, tue = date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)
    assert freshness.business_days_between(fri, mon) == 1
    assert freshness.business_days_between(fri, tue) == 2
    assert freshness.business_days_between(mon, fri) == 0
    assert freshness.business_days_between(date(2026, 10, 5), date(2026, 10, 19)) == 10  # two whole weeks


def test_assess_freshness_tolerates_normal_lag_and_flags_missed_runs():
    fresh = freshness.assess_freshness(NAV_DAY, NAV_DAY, date(2026, 10, 15), max_lag=2)
    assert fresh["stale"] is False and fresh["message"] is None and fresh["nav_lag_business_days"] == 1

    stale = freshness.assess_freshness(NAV_DAY, NAV_DAY, date(2026, 10, 19), max_lag=2)  # Thu, Fri, Mon
    assert stale["stale"] is True and stale["nav_lag_business_days"] == 3
    assert "14 Oct 2026" in stale["message"] and stale["reasons"]

    old_rankings = freshness.assess_freshness(NAV_DAY, date(2026, 10, 1), date(2026, 10, 15), max_lag=2)
    assert old_rankings["stale"] is True and old_rankings["reasons"] == ["latest ranking run is 10 business days old"]


def test_assess_freshness_without_data_is_stale():
    out = freshness.assess_freshness(None, None, date(2026, 10, 15), max_lag=2)
    assert out["stale"] is True and out["latest_nav_date"] is None
    assert out["message"] == "RA's fund data hasn't been loaded yet, so it can't give current figures."


def test_data_freshness_reads_the_database(db, ranked):
    out = freshness.data_freshness(db, today=date(2026, 10, 15))
    assert out["latest_nav_date"] == "2026-10-14" and out["metrics_as_of"] == "2026-10-14"
    assert out["stale"] is False
    assert freshness.data_freshness(db, today=date(2026, 10, 21))["stale"] is True


def test_data_status_and_readiness_report_freshness(db, ranked, monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(freshness, "today_ist", lambda: date(2026, 10, 15))
    status = client.get("/api/meta/data-status").json()
    assert status["latest_nav_date"] == "2026-10-14" and status["freshness"]["stale"] is False

    r = client.get("/api/health/ready")
    assert r.status_code == 200 and r.json()["ready"] is True and r.json()["database"] == "ok"

    monkeypatch.setattr(freshness, "today_ist", lambda: date(2026, 10, 23))
    r = client.get("/api/health/ready")
    assert r.status_code == 503 and r.json()["data"]["stale"] is True

    assert client.get("/api/health").json()["status"] == "ok"


def test_readiness_hides_database_errors(monkeypatch):
    class Broken:
        def __enter__(self):
            raise RuntimeError("could not connect to host db.example user=postgres")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(health, "SessionLocal", Broken)
    r = TestClient(app).get("/api/health/ready")
    assert r.status_code == 503 and r.json()["database"] == "unreachable"
    assert "example" not in r.text and "postgres" not in r.text
