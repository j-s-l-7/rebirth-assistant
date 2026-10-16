"""Scheme metrics and within-category ranking (PRD §11.2).

Factors: consistency (rolling 3-year returns vs peers), risk-adjusted return (3-year
Sortino), downside protection (downside capture vs the benchmark proxy, max drawdown) and
cost (the fund's Regular-plan total expense ratio from AMFI's TER disclosure, lower is
better). Portfolio quality and stewardship need feeds we don't have yet; their weights are
redistributed and every result lists what was unavailable (`factors_unavailable`).
"""

import math
from datetime import date, timedelta
from statistics import mean, median, pstdev

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.analytics.dates import add_months
from app.analytics.navbook import NavBook
from app.models import CategoryStat, Scheme, SchemeLink, SchemeMetric, SchemeTer
from app.reference.categories import BENCHMARK_PROXIES, get_category

MODEL_VERSION = "rank-v0.2"
RF_ANNUAL = 0.065
WEIGHTS = {"consistency": 25, "risk_adjusted": 20, "downside": 20, "cost": 10}
FACTORS_UNAVAILABLE = ["portfolio_quality", "stewardship"]
# Funds need 3+ years of NAV history to be tiered (PRD §11.1). History is capped by the
# loaded window (NAV_HISTORY_YEARS), so `nav_history_years` is not the fund's age.
MIN_TRACK_YEARS = 3.0


def benchmark_code(category_key: str) -> int | None:
    key = get_category(category_key).benchmark
    return BENCHMARK_PROXIES[key]["amfi_code"] if key else None


def benchmark_label(category_key: str) -> str | None:
    key = get_category(category_key).benchmark
    return BENCHMARK_PROXIES[key]["label"] if key else None


def regular_ter(db: Session, codes: set[int]) -> dict[int, float]:
    """Latest Regular-plan TER (%) of each scheme's fund. Direct-plan codes get their fund's
    Regular TER too, so cost is compared like for like across a category."""
    if not codes:
        return {}
    latest = select(SchemeTer.nsdl_code, func.max(SchemeTer.effective_from).label("d")).group_by(SchemeTer.nsdl_code).subquery()
    rows = db.execute(
        select(SchemeLink.amfi_code, SchemeTer.r_ter)
        .join(SchemeTer, SchemeTer.nsdl_code == SchemeLink.external_key)
        .join(latest, (latest.c.nsdl_code == SchemeTer.nsdl_code) & (latest.c.d == SchemeTer.effective_from))
        .where(SchemeLink.source == "ter", SchemeLink.amfi_code.in_(codes))
    ).all()
    return {code: float(ter) for code, ter in rows if ter is not None}


def cagr(start_value: float, end_value: float, years: float) -> float | None:
    if start_value <= 0 or end_value <= 0 or years <= 0:
        return None
    return (end_value / start_value) ** (1 / years) - 1


def trailing_cagr(nb: NavBook, code: int, as_of: date, years: int) -> float | None:
    first = nb.first_date(code)
    start_d = add_months(as_of, -12 * years)
    if first is None or first > start_d + timedelta(days=7):
        return None
    start, end = nb.nav_on(code, start_d), nb.nav_on(code, as_of)
    if not start or not end:
        return None
    return cagr(start, end, years)


def _monthly_returns(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    return [(points[i][0], points[i][1] / points[i - 1][1] - 1) for i in range(1, len(points)) if points[i - 1][1] > 0]


def max_drawdown(navs: list[float]) -> float | None:
    if len(navs) < 2:
        return None
    peak, worst = navs[0], 0.0
    for n in navs:
        peak = max(peak, n)
        worst = min(worst, n / peak - 1)
    return worst


def _rolling(points: list[tuple[date, float]], months: int = 36) -> dict[tuple[int, int], float]:
    out = {}
    for i in range(months, len(points)):
        a, b = points[i - months][1], points[i][1]
        if a > 0:
            out[(points[i][0].year, points[i][0].month)] = (b / a) ** (12 / months) - 1
    return out


def scheme_metrics(nb: NavBook, code: int, as_of: date, bench: int | None) -> tuple[dict, dict]:
    """Returns (metrics, rolling-3y-by-month)."""
    first = nb.first_date(code)
    me = [(d, n) for d, n in nb.month_end_series(code, since=add_months(as_of, -12 * 7)) if d <= as_of]
    rets = _monthly_returns(me)
    r36 = [r for d, r in rets if d > add_months(as_of, -36)]
    m: dict = {
        "nav_history_years": round((as_of - first).days / 365.25, 2) if first else 0.0,
        "cagr_1y": trailing_cagr(nb, code, as_of, 1),
        "cagr_3y": trailing_cagr(nb, code, as_of, 3),
        "cagr_5y": trailing_cagr(nb, code, as_of, 5),
    }
    if len(r36) >= 30:
        rf_m = (1 + RF_ANNUAL) ** (1 / 12) - 1
        downside = math.sqrt(mean(min(0.0, r - rf_m) ** 2 for r in r36)) * math.sqrt(12)
        ann = (math.prod(1 + r for r in r36)) ** (12 / len(r36)) - 1
        m["vol_3y"] = pstdev(r36) * math.sqrt(12)
        m["sortino_3y"] = (ann - RF_ANNUAL) / downside if downside > 0 else None
        dates, navs = nb.series(code, since=add_months(as_of, -36))
        m["max_dd_3y"] = max_drawdown([n for d, n in zip(dates, navs) if d <= as_of])
    if bench and bench != code and len(r36) >= 30:
        bret = dict(_monthly_returns([(d, n) for d, n in nb.month_end_series(bench, since=add_months(as_of, -12 * 4)) if d <= as_of]))
        paired = [(r, bret[d]) for d, r in rets if d > add_months(as_of, -36) and d in bret]
        down = [(s, b) for s, b in paired if b < 0]
        up = [(s, b) for s, b in paired if b > 0]
        if len(down) >= 5:
            m["downside_capture_3y"] = mean(s for s, _ in down) / mean(b for _, b in down) * 100
        if len(up) >= 5:
            m["upside_capture_3y"] = mean(s for s, _ in up) / mean(b for _, b in up) * 100
        active = [s - b for s, b in paired]
        if len(active) >= 24 and pstdev(active) > 0:
            m["info_ratio_3y"] = mean(active) / pstdev(active) * math.sqrt(12)
            # How closely the fund moves with its index: a NAV-only read on diversification.
            # High R-squared and low tracking error = index-like; low R-squared = concentrated/different.
            fs, bs = [s for s, _ in paired], [b for _, b in paired]
            mf, mb = mean(fs), mean(bs)
            cov = mean((f - mf) * (b - mb) for f, b in paired)
            var_b, var_f = pstdev(bs) ** 2, pstdev(fs) ** 2
            if var_b > 0 and var_f > 0:
                m["beta_3y"] = cov / var_b
                m["r_squared_3y"] = cov * cov / (var_b * var_f)
            m["tracking_error_3y"] = pstdev(active) * math.sqrt(12)
        for y in (1, 3, 5):
            m[f"bench_cagr_{y}y"] = trailing_cagr(nb, bench, as_of, y)
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()}, _rolling(me)


def _pct_rank(values: dict[int, float | None], higher_is_better: bool = True) -> dict[int, float]:
    clean = {k: v for k, v in values.items() if v is not None}
    if len(clean) < 2:
        return {k: 0.5 for k in clean}
    ordered = sorted(clean, key=lambda k: clean[k], reverse=not higher_is_better)
    n = len(ordered)
    return {k: i / (n - 1) for i, k in enumerate(ordered)}  # 0 = worst, 1 = best


def _tier(pct: float) -> str:
    if pct >= 0.70:
        return "core"
    if pct >= 0.40:
        return "satellite"
    if pct >= 0.15:
        return "watch"
    return "exit"


def rank_category(db: Session, nb: NavBook, category_key: str, as_of: date, extra_codes: set[int] | None = None, persist: bool = True) -> dict[int, dict]:
    """Score every universe scheme in a category (plus `extra_codes`, e.g. held Direct plans,
    which are scored against the same peer distribution but don't shape it)."""
    cat = get_category(category_key)
    peers = set(db.scalars(select(Scheme.amfi_code).where(Scheme.category_key == category_key, Scheme.in_universe.is_(True))))
    codes = peers | (extra_codes or set())
    bench = benchmark_code(category_key)
    nb.preload(codes | ({bench} if bench else set()))

    metrics, rolling = {}, {}
    ters = regular_ter(db, codes)
    for code in codes:
        metrics[code], rolling[code] = scheme_metrics(nb, code, as_of, bench)
        metrics[code]["ter_regular"] = ters.get(code)

    # Consistency: share of rolling-3y windows (last 5y) in the top half of peers.
    months = sorted({mk for c in peers for mk in rolling[c] if mk > (add_months(as_of, -60).year, add_months(as_of, -60).month)})
    medians = {}
    for mk in months:
        vals = [rolling[c][mk] for c in peers if mk in rolling[c]]
        if len(vals) >= 3:
            medians[mk] = median(vals)
    for code in codes:
        hits = [rolling[code][mk] >= med for mk, med in medians.items() if mk in rolling[code]]
        metrics[code]["consistency"] = round(sum(hits) / len(hits), 4) if len(hits) >= 12 else None
        metrics[code]["peer_count"] = len(peers)

    eligible = {c for c in codes if metrics[c]["nav_history_years"] >= MIN_TRACK_YEARS}
    ranked_pool = (peers & eligible) | (eligible & (extra_codes or set()))
    pct_cons = _pct_rank({c: metrics[c].get("consistency") for c in ranked_pool})
    pct_sort = _pct_rank({c: metrics[c].get("sortino_3y") for c in ranked_pool})
    pct_dc = _pct_rank({c: metrics[c].get("downside_capture_3y") for c in ranked_pool}, higher_is_better=False)
    pct_dd = _pct_rank({c: metrics[c].get("max_dd_3y") for c in ranked_pool})  # less negative is better
    pct_cost = _pct_rank({c: metrics[c].get("ter_regular") for c in ranked_pool}, higher_is_better=False)

    composites = {}
    for c in ranked_pool:
        parts = {}
        if c in pct_cons:
            parts["consistency"] = pct_cons[c]
        if c in pct_sort:
            parts["risk_adjusted"] = pct_sort[c]
        down = [p for p in (pct_dc.get(c), pct_dd.get(c)) if p is not None]
        if down:
            parts["downside"] = mean(down)
        if c in pct_cost:
            parts["cost"] = pct_cost[c]
        if parts:
            w = sum(WEIGHTS[k] for k in parts)
            composites[c] = sum(WEIGHTS[k] * v for k, v in parts.items()) / w * 100
            metrics[c]["factor_percentiles"] = {k: round(v, 3) for k, v in parts.items()}
    comp_rank = _pct_rank({c: composites.get(c) for c in ranked_pool if c in peers and c in composites})

    for c in codes:
        m = metrics[c]
        m["composite_score"] = round(composites[c], 1) if c in composites else None
        if not cat.peer_group or c not in composites:
            m["tier"] = "new" if m["nav_history_years"] < MIN_TRACK_YEARS else None
        elif c in comp_rank:
            m["tier"] = _tier(comp_rank[c])
        else:  # extra code: place it within the peer distribution
            peer_scores = sorted(composites[p] for p in peers if p in composites)
            below = sum(1 for s in peer_scores if s <= composites[c])
            m["tier"] = _tier(below / len(peer_scores)) if peer_scores else None
        m["model_version"] = MODEL_VERSION
        m["factors_unavailable"] = FACTORS_UNAVAILABLE + ([] if m.get("ter_regular") is not None else ["cost_ter"])

    if persist and codes:
        db.execute(delete(SchemeMetric).where(SchemeMetric.amfi_code.in_(codes), SchemeMetric.as_of == as_of, SchemeMetric.model_version == MODEL_VERSION))
        for c, m in metrics.items():
            db.add(SchemeMetric(amfi_code=c, as_of=as_of, model_version=MODEL_VERSION, metrics=m, composite_score=m.get("composite_score"), tier=m.get("tier")))
        if cat.peer_group:
            pool = peers & eligible
            stats = {
                "rolling_medians": {f"{y}-{mo:02d}": round(v, 6) for (y, mo), v in medians.items()},
                "peer_factors": {f: [metrics[c][f] for c in pool if metrics[c].get(f) is not None]
                                 for f in ("consistency", "sortino_3y", "downside_capture_3y", "max_dd_3y", "ter_regular")},
                "peer_composites": sorted(round(composites[p], 3) for p in peers if p in composites),
                "peer_count": len(peers),
            }
            db.execute(delete(CategoryStat).where(CategoryStat.category_key == category_key, CategoryStat.as_of == as_of, CategoryStat.model_version == MODEL_VERSION))
            db.add(CategoryStat(category_key=category_key, as_of=as_of, model_version=MODEL_VERSION, stats=stats))
        db.commit()
    return metrics


def _pct_against(value: float | None, peer_values: list[float] | None, higher_is_better: bool = True) -> float | None:
    if value is None or not peer_values:
        return None
    worse = sum(1 for v in peer_values if (v < value if higher_is_better else v > value))
    return worse / len(peer_values)


def score_against_stats(nb: NavBook, code: int, category_key: str, as_of: date, stats: dict | None, ter: float | None = None) -> dict:
    """Score one scheme against a cached peer distribution: needs only its own NAVs and its
    benchmark's, not every peer's. Without stats (or for non-peer categories) it returns the
    scheme's own metrics with no tier."""
    m, rolling = scheme_metrics(nb, code, as_of, benchmark_code(category_key))
    m["ter_regular"] = ter
    m["model_version"] = MODEL_VERSION
    m["factors_unavailable"] = FACTORS_UNAVAILABLE + ([] if ter is not None else ["cost_ter"])
    m["composite_score"] = None
    m["tier"] = "new" if m["nav_history_years"] < MIN_TRACK_YEARS else None
    if not stats or not get_category(category_key).peer_group:
        return m
    medians = stats["rolling_medians"]
    hits = [v >= medians[f"{y}-{mo:02d}"] for (y, mo), v in rolling.items() if f"{y}-{mo:02d}" in medians]
    m["consistency"] = round(sum(hits) / len(hits), 4) if len(hits) >= 12 else None
    m["peer_count"] = stats.get("peer_count")
    if m["tier"] == "new":
        return m
    pf = stats["peer_factors"]
    parts = {}
    if (p := _pct_against(m.get("consistency"), pf.get("consistency"))) is not None:
        parts["consistency"] = p
    if (p := _pct_against(m.get("sortino_3y"), pf.get("sortino_3y"))) is not None:
        parts["risk_adjusted"] = p
    down = [x for x in (_pct_against(m.get("downside_capture_3y"), pf.get("downside_capture_3y"), higher_is_better=False),
                        _pct_against(m.get("max_dd_3y"), pf.get("max_dd_3y"))) if x is not None]
    if down:
        parts["downside"] = mean(down)
    if (p := _pct_against(ter, pf.get("ter_regular"), higher_is_better=False)) is not None:
        parts["cost"] = p
    if not parts:
        return m
    composite = sum(WEIGHTS[k] * v for k, v in parts.items()) / sum(WEIGHTS[k] for k in parts) * 100
    peer_scores = stats.get("peer_composites") or []
    m["composite_score"] = round(composite, 1)
    m["factor_percentiles"] = {k: round(v, 3) for k, v in parts.items()}
    m["tier"] = _tier(sum(1 for s in peer_scores if s <= composite) / len(peer_scores)) if peer_scores else None
    return m


def latest_category_stats(db: Session, category_key: str, max_age_days: int = 10) -> dict | None:
    cutoff = date.today() - timedelta(days=max_age_days)
    row = db.scalar(
        select(CategoryStat).where(CategoryStat.category_key == category_key, CategoryStat.model_version == MODEL_VERSION, CategoryStat.as_of >= cutoff)
        .order_by(CategoryStat.as_of.desc()).limit(1)
    )
    return row.stats if row else None


def latest_metrics(db: Session, codes: set[int], max_age_days: int = 10) -> dict[int, dict]:
    if not codes:
        return {}
    cutoff = date.today() - timedelta(days=max_age_days)
    rows = db.scalars(
        select(SchemeMetric).where(SchemeMetric.amfi_code.in_(codes), SchemeMetric.model_version == MODEL_VERSION, SchemeMetric.as_of >= cutoff).order_by(SchemeMetric.as_of)
    ).all()
    return {r.amfi_code: r.metrics for r in rows}  # later rows overwrite earlier


def ensure_metrics(db: Session, nb: NavBook, codes_by_category: dict[str, set[int]], as_of: date) -> dict[int, dict]:
    """Metrics for the given schemes. Cached rows are reused; a missing scheme is scored
    against its category's cached peer statistics. A whole category is ranked here only
    when it has never been ranked (e.g. a fresh database before compute-metrics)."""
    out: dict[int, dict] = {}
    all_codes = {c for cs in codes_by_category.values() for c in cs}
    cached = latest_metrics(db, all_codes)
    fresh: dict[int, dict] = {}
    for cat_key, codes in codes_by_category.items():
        out.update({c: cached[c] for c in codes if c in cached})
        missing = codes - cached.keys()
        if not missing:
            continue
        peer_group = get_category(cat_key).peer_group
        stats = latest_category_stats(db, cat_key) if peer_group else None
        if peer_group and stats is None:
            ranked = rank_category(db, nb, cat_key, as_of, extra_codes=missing)
            out.update({c: ranked[c] for c in missing if c in ranked})
            continue
        bench = benchmark_code(cat_key)
        nb.preload(missing | ({bench} if bench else set()))
        ters = regular_ter(db, missing)
        for code in missing:
            fresh[code] = out[code] = score_against_stats(nb, code, cat_key, as_of, stats, ters.get(code))
    if fresh:
        db.execute(delete(SchemeMetric).where(SchemeMetric.amfi_code.in_(fresh), SchemeMetric.as_of == as_of, SchemeMetric.model_version == MODEL_VERSION))
        db.add_all(SchemeMetric(amfi_code=c, as_of=as_of, model_version=MODEL_VERSION, metrics=m, composite_score=m.get("composite_score"), tier=m.get("tier"))
                   for c, m in fresh.items())
        db.commit()
    return out

