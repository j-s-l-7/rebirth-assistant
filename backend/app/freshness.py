"""Is the fund data current? The assistant must never present stale data as current.

`data_freshness(db)` reads two dates:
- latest_nav_date: the newest NAV date across the investable universe (scheme.latest_nav_date,
  written by the daily scheme-master sync in the same transaction as that day's NAV history);
- metrics_as_of: the newest full ranking run (category_stat, current model version).
Each is compared with today's date in IST in business days (Mon-Fri; market holidays aren't
modelled, which FRESHNESS_MAX_LAG_BUSINESS_DAYS absorbs). AMFI publishes NAVs late each business
day and the daily job loads them at 23:45 IST, so a lag of 1 is normal during the day.

Both queries are small (the scheme and category_stat tables), so this is cheap enough to call on
every chat turn and from readiness probes.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.performance import MODEL_VERSION
from app.config import get_settings
from app.models import CategoryStat, Scheme

IST = timezone(timedelta(hours=5, minutes=30))  # no DST, so a fixed offset is exact


def today_ist() -> date:
    return datetime.now(IST).date()


def business_days_between(start: date, end: date) -> int:
    """Weekdays d with start < d <= end (0 when end <= start)."""
    if end <= start:
        return 0
    weeks, rest = divmod((end - start).days, 7)
    n, d = weeks * 5, start + timedelta(days=weeks * 7)
    for _ in range(rest):
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def _fmt(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def assess_freshness(latest_nav_date: date | None, metrics_as_of: date | None, today: date, max_lag: int) -> dict:
    nav_lag = business_days_between(latest_nav_date, today) if latest_nav_date else None
    metrics_lag = business_days_between(metrics_as_of, today) if metrics_as_of else None
    reasons = []
    if latest_nav_date is None:
        reasons.append("no NAVs loaded")
    elif nav_lag > max_lag:
        reasons.append(f"latest NAV is {nav_lag} business days old")
    if metrics_as_of is None:
        reasons.append("no ranking run")
    elif metrics_lag > max_lag:
        reasons.append(f"latest ranking run is {metrics_lag} business days old")
    message = None
    if reasons:
        if latest_nav_date is None:
            message = "RA's fund data hasn't been loaded yet, so it can't give current figures."
        else:
            ranked = f" and the rankings from {_fmt(metrics_as_of)}" if metrics_as_of else ""
            message = (f"RA's fund data is out of date: the latest NAVs are from {_fmt(latest_nav_date)}{ranked}. "
                       "Recent market moves aren't reflected.")
    return {
        "latest_nav_date": latest_nav_date.isoformat() if latest_nav_date else None,
        "metrics_as_of": metrics_as_of.isoformat() if metrics_as_of else None,
        "nav_lag_business_days": nav_lag,
        "metrics_lag_business_days": metrics_lag,
        "max_lag_business_days": max_lag,
        "stale": bool(reasons),
        "reasons": reasons,
        "message": message,
        "checked_on": today.isoformat(),
    }


def data_freshness(db: Session, *, today: date | None = None) -> dict:
    """{latest_nav_date, metrics_as_of (ISO dates or None), nav_lag_business_days,
    metrics_lag_business_days, max_lag_business_days, stale (bool), reasons (list[str]),
    message (a sentence to show people when stale, else None), checked_on}."""
    latest_nav = db.scalar(select(func.max(Scheme.latest_nav_date)).where(Scheme.in_universe.is_(True)))
    metrics_as_of = db.scalar(select(func.max(CategoryStat.as_of)).where(CategoryStat.model_version == MODEL_VERSION))
    return assess_freshness(latest_nav, metrics_as_of, today or today_ist(), get_settings().freshness_max_lag_business_days)
