"""One call that assembles everything the API, score, engine and copilot read from."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.navbook import NavBook
from app.analytics.overlap import overlap_report
from app.analytics.performance import benchmark_code, ensure_metrics, shadow_benchmark
from app.analytics.portfolio import PortfolioView, build_portfolio
from app.analytics.score import compute_score
from app.models import Investor, RiskProfile
from app.reference.categories import get_category
from app.reference.risk import suitability

METRIC_FIELDS = ("tier", "composite_score", "consistency", "cagr_1y", "cagr_3y", "cagr_5y", "bench_cagr_3y", "sortino_3y",
                 "max_dd_3y", "downside_capture_3y", "ter_regular", "nav_history_years", "peer_count", "factors_unavailable")


@dataclass
class Analysis:
    investor: Investor
    risk: RiskProfile | None
    pv: PortfolioView
    overlap: dict
    score: dict
    nb: NavBook

    @property
    def as_of(self) -> date:
        return self.pv.as_of


def latest_risk_profile(db: Session, investor_id: str) -> RiskProfile | None:
    return db.scalar(select(RiskProfile).where(RiskProfile.investor_id == investor_id).order_by(RiskProfile.created_at.desc()).limit(1))


def analyse(db: Session, investor: Investor, with_metrics: bool = True) -> Analysis:
    nb = NavBook(db)
    pv = build_portfolio(db, investor, nb)
    risk = latest_risk_profile(db, investor.id)

    metrics: dict[int, dict] = {}
    if with_metrics and pv.holdings:
        by_cat: dict[str, set[int]] = defaultdict(set)
        for h in pv.holdings:
            by_cat[h.scheme.category_key].add(h.code)
        metrics = ensure_metrics(db, nb, by_cat, pv.as_of)

    nb.preload({b for h in pv.holdings if (b := benchmark_code(h.scheme.category_key))})
    for h in pv.holdings:
        cat = get_category(h.scheme.category_key)
        m = metrics.get(h.code) or {}
        h.extras["metrics"] = {k: m.get(k) for k in METRIC_FIELDS if k in m} or None
        h.extras["vs_benchmark"] = shadow_benchmark(nb, h, pv.as_of)
        h.extras["typical_exit_load"] = f"{cat.exit_load_pct:g}% if redeemed within {cat.exit_load_days} days" if cat.exit_load_days else "None typical"
        h.extras["lock_in_days"] = cat.lock_in_days or None
        h.extras["suitability"] = suitability(risk.bucket, h.scheme.category_key, risk.horizon_years) if risk else None

    overlap = overlap_report(db, pv)
    score = compute_score(pv, risk, overlap)
    return Analysis(investor=investor, risk=risk, pv=pv, overlap=overlap, score=score, nb=nb)
