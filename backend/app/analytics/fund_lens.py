"""Fund lens: how any mutual fund compares with the other funds in its SEBI category.

Powers questions like "how does X compare with other flexi caps?" with numbers the copilot
explains (it never computes them itself):
  - rank and percentile among category peers (the ranking model's composite)
  - each metric beside the category median and quartiles
  - a "D" profile for a single fund: performance, consistency, risk, diversification,
    asset mix and cost
  - the category's top funds for context

Peers are the investable universe (open-ended Regular-plan Growth schemes). A Direct plan
or IDCW option is assessed through its Regular-Growth sibling so the comparison is like for
like; the answer says so.
"""

from datetime import date
from statistics import median

from rapidfuzz import fuzz
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.analytics.navbook import NavBook
from app.analytics.performance import benchmark_label, ensure_metrics, latest_metrics
from app.data.linking import norm_name
from app.data.ter import current_ter_rows
from app.models import NavPoint, Scheme, SchemeDetail, SchemeHolding, SchemeLink, SchemePerformance
from app.reference.categories import RISK_LABELS, get_category

# SEBI category mandates in brief (structural diversification rules). Summary wording; the
# scheme's SID is authoritative.
CATEGORY_RULES = {
    "large_cap": "At least 80% in the 100 largest companies.",
    "mid_cap": "At least 65% in companies ranked 101-250 by market cap.",
    "small_cap": "At least 65% in companies ranked 251 and below.",
    "large_mid_cap": "At least 35% each in large-cap and mid-cap companies.",
    "multi_cap": "At least 25% each in large, mid and small caps.",
    "flexi_cap": "At least 65% in equity, free to move across market caps.",
    "focused": "At most 30 stocks, at least 65% in equity: concentrated by design.",
    "value": "Value-style stock picking, at least 65% in equity.",
    "contra": "Contrarian stock picking, at least 65% in equity.",
    "dividend_yield": "Mostly dividend-yielding stocks, at least 65% in equity.",
    "elss": "At least 80% in equity with a 3-year lock-in on every purchase.",
    "sectoral": "At least 80% in one sector: concentrated by design.",
    "thematic": "At least 80% in one theme: concentrated by design.",
    "index_equity": "Tracks an index; its diversification is the index's.",
    "intl_fof": "Invests in overseas funds or ETFs.",
    "aggressive_hybrid": "65-80% in equity, 20-35% in debt.",
    "balanced_hybrid": "40-60% each in equity and debt, no arbitrage.",
    "conservative_hybrid": "10-25% in equity, 75-90% in debt.",
    "dynamic_asset_allocation": "Moves between equity and debt with market conditions.",
    "multi_asset": "At least 10% each in three asset classes (e.g. equity, debt, gold).",
    "arbitrage": "Hedged arbitrage positions: equity-taxed, low volatility.",
    "equity_savings": "Equity, arbitrage and at least 10% debt.",
    "overnight": "Securities maturing in one day.",
    "liquid": "Securities maturing within 91 days.",
    "ultra_short": "Portfolio duration of 3-6 months.",
    "low_duration": "Portfolio duration of 6-12 months.",
    "money_market": "Money-market instruments maturing within a year.",
    "short_duration": "Portfolio duration of 1-3 years.",
    "medium_duration": "Portfolio duration of 3-4 years.",
    "medium_long_duration": "Portfolio duration of 4-7 years.",
    "long_duration": "Portfolio duration above 7 years.",
    "dynamic_bond": "Any duration, actively managed.",
    "corporate_bond": "At least 80% in AA+ and higher-rated corporate bonds.",
    "credit_risk": "At least 65% in bonds rated below AA+: higher credit risk by design.",
    "banking_psu": "At least 80% in bonds of banks, PSUs and public financial institutions.",
    "gilt": "At least 80% in government securities.",
    "gilt_10y": "At least 80% in government securities with ~10-year duration.",
    "floater": "At least 65% in floating-rate instruments.",
    "gold": "Gold ETFs or physical gold.",
    "silver": "Silver ETFs or physical silver.",
}

# (metric key, label, direction): direction +1 higher is better, -1 lower is better, 0 descriptive.
METRICS = [
    ("cagr_1y", "1-year return", 1),
    ("cagr_3y", "3-year return (annualised)", 1),
    ("cagr_5y", "5-year return (annualised)", 1),
    ("consistency", "Consistency (share of 3-year periods in the top half of peers)", 1),
    ("sortino_3y", "Return per unit of downside risk (Sortino, 3y)", 1),
    ("vol_3y", "Volatility (3y)", -1),
    ("max_dd_3y", "Worst fall from a peak (3y)", 1),
    ("downside_capture_3y", "Share of the index's falls captured (3y)", -1),
    ("ter_regular", "Expense ratio, Regular plan", -1),
    ("r_squared_3y", "How closely it moves with its index (R-squared)", 0),
    ("tracking_error_3y", "How far it strays from its index (tracking error)", 0),
    ("beta_3y", "Sensitivity to index moves (beta)", 0),
]
PCT_KEYS = {"cagr_1y", "cagr_3y", "cagr_5y", "consistency", "vol_3y", "max_dd_3y", "tracking_error_3y"}


def _fmt(key: str, v: float | None) -> float | None:
    if v is None:
        return None
    return round(v * 100, 2) if key in PCT_KEYS else round(v, 3)


def _standing(pct: float | None) -> str | None:
    if pct is None:
        return None
    if pct >= 0.9:
        return "top 10% of the category"
    if pct >= 0.75:
        return "top quarter"
    if pct >= 0.5:
        return "above the median"
    if pct >= 0.25:
        return "below the median"
    return "bottom quarter"


def _beats(value: float, peers: list[float], direction: int) -> float:
    """Share of peers this value is better than (direction +1: higher is better)."""
    if not peers:
        return 0.5
    better = sum(1 for p in peers if (value > p if direction > 0 else value < p))
    return better / len(peers)


GENERIC_WORDS = {"fund", "funds", "scheme", "mutual", "the", "and", "india"}
MIN_NAME_SIMILARITY = 60


def resolve_funds(db: Session, query: str, limit: int = 6) -> list[dict]:
    """Find schemes by name, best match first: fuzzy similarity of the whole normalised name,
    then Regular-Growth plans in the peer set ahead of other plans of the same scheme."""
    q = norm_name(query)
    terms = [t for t in q.split() if (len(t) > 2 or t.isdigit()) and t not in GENERIC_WORDS][:6]
    if not terms:
        return []
    rows = db.scalars(select(Scheme).where(*[Scheme.name.ilike(f"%{t}%") for t in terms]).limit(300)).all()
    if not rows:  # tolerate a word the scheme name doesn't contain
        rows = db.scalars(select(Scheme).where(or_(*[Scheme.name.ilike(f"%{t}%") for t in terms])).limit(300)).all()

    def rank(s: Scheme) -> tuple:
        return (-fuzz.token_sort_ratio(q, norm_name(s.name)), not s.in_universe, s.plan_type != "regular", s.option_type != "growth", len(s.name))

    rows = [s for s in rows if fuzz.token_set_ratio(q, norm_name(s.name)) >= MIN_NAME_SIMILARITY]
    rows.sort(key=rank)
    return [{"amfi_code": s.amfi_code, "name": s.name, "amc": s.amc_name, "plan": s.plan_type, "option": s.option_type,
             "category": get_category(s.category_key).label, "in_peer_set": s.in_universe} for s in rows[:limit]]


def is_weak_match(query: str, name: str, clear: int = 85) -> bool:
    """True when `name` is not a clear match for `query`: a query word is missing from it, or it carries
    extra distinguishing words (e.g. 'US', 'Equity') the query lacks."""
    q, n = norm_name(query), norm_name(name)
    if fuzz.token_sort_ratio(q, n) >= clear:
        return False
    qt, nt = set(q.split()) - GENERIC_WORDS, set(n.split()) - GENERIC_WORDS
    return bool(qt ^ nt)


def candidate_names(matches: list[dict], n: int = 5) -> list[str]:
    return list(dict.fromkeys(m["name"] for m in matches))[:n]


def regular_growth_sibling(db: Session, s: Scheme) -> Scheme:
    """The Regular-Growth plan of the same scheme (itself if it already is one)."""
    if s.plan_type == "regular" and s.option_type == "growth":
        return s
    key = norm_name(s.name)
    for c in db.scalars(select(Scheme).where(Scheme.amc_name == s.amc_name, Scheme.category_key == s.category_key,
                                             Scheme.plan_type == "regular", Scheme.option_type == "growth")):
        if norm_name(c.name) == key:
            return c
    return s


def _peer_metrics(db: Session, category_key: str) -> dict[int, dict]:
    codes = set(db.scalars(select(Scheme.amfi_code).where(Scheme.category_key == category_key, Scheme.in_universe.is_(True))))
    return latest_metrics(db, codes)


def _ter_pair(db: Session, code: int) -> dict | None:
    row = current_ter_rows(db, [code]).get(code)
    if row is None:
        return None
    # AMFI writes 0 for a plan the scheme doesn't have: report it as absent, not as a free fund.
    return {"regular": row.r_ter or None, "direct": row.d_ter or None, "regular_breakdown": {"base": row.r_ber, "brokerage": row.r_brokerage,
            "transaction": row.r_transaction, "statutory": row.r_statutory}, "effective_from": row.effective_from.isoformat()}


def _official(db: Session, code: int) -> dict | None:
    row = db.scalars(select(SchemePerformance).where(SchemePerformance.amfi_code == code).order_by(SchemePerformance.as_of.desc()).limit(1)).first()
    if row is None:
        key = db.scalar(select(SchemeLink.external_key).where(SchemeLink.amfi_code == code, SchemeLink.source == "performance"))
        if key:
            row = db.scalars(select(SchemePerformance).where(SchemePerformance.external_key == key).order_by(SchemePerformance.as_of.desc()).limit(1)).first()
    if row is None:
        return None
    return {"as_of": row.as_of.isoformat(), "plan": row.plan, "benchmark": row.benchmark, "returns_pct": row.returns,
            "benchmark_returns_pct": row.benchmark_returns, "aum_cr": row.aum_cr,
            "riskometer": RISK_LABELS.get(row.riskometer) if row.riskometer else None,
            "benchmark_riskometer": RISK_LABELS.get(row.benchmark_riskometer) if row.benchmark_riskometer else None}


def _holdings(db: Session, code: int) -> dict | None:
    month = db.scalar(select(func.max(SchemeHolding.disclosure_month)).where(SchemeHolding.amfi_code == code))
    if month is None:
        return None
    rows = db.scalars(select(SchemeHolding).where(SchemeHolding.amfi_code == code, SchemeHolding.disclosure_month == month)).all()
    weights = sorted((r.weight_pct for r in rows), reverse=True)
    sectors: dict[str, float] = {}
    for r in rows:
        sectors[r.sector or "Unclassified"] = sectors.get(r.sector or "Unclassified", 0.0) + r.weight_pct
    top_sector = max(sectors.items(), key=lambda kv: kv[1]) if sectors else None
    top = sorted((r for r in rows if r.security_isin), key=lambda r: r.weight_pct, reverse=True)[:5]
    top_sectors = sorted(((k, v) for k, v in sectors.items() if k != "Unclassified"), key=lambda kv: kv[1], reverse=True)[:5]
    return {"disclosure_month": month.isoformat(), "holdings": len(rows), "top10_weight_pct": round(sum(weights[:10]), 1),
            "top_holdings": [{"name": r.security_name, "weight_pct": round(r.weight_pct, 2)} for r in top],
            "top_sectors": [{"name": k, "weight_pct": round(v, 2)} for k, v in top_sectors],
            "top_sector": {"name": top_sector[0], "weight_pct": round(top_sector[1], 1)} if top_sector else None}


def fund_lens(db: Session, amfi_code: int, nb: NavBook | None = None) -> dict:
    asked = db.get(Scheme, amfi_code)
    if asked is None:
        raise LookupError(f"No scheme with AMFI code {amfi_code}")
    s = regular_growth_sibling(db, asked)
    cat = get_category(s.category_key)
    nb = nb or NavBook(db)
    latest = nb.latest(s.amfi_code)
    as_of = latest[0] if latest else db.scalar(select(func.max(NavPoint.nav_date))) or date.today()

    m = ensure_metrics(db, nb, {s.category_key: {s.amfi_code}}, as_of).get(s.amfi_code) or {}
    peers = {c: pm for c, pm in _peer_metrics(db, s.category_key).items() if c != s.amfi_code}

    table = []
    for key, label, direction in METRICS:
        v = m.get(key)
        vals = sorted(pm[key] for pm in peers.values() if pm.get(key) is not None)
        row = {"metric": key, "label": label, "fund": _fmt(key, v), "unit": "%" if key in PCT_KEYS or key == "ter_regular" else None}
        if vals:
            row.update({"category_median": _fmt(key, median(vals)), "category_p25": _fmt(key, vals[len(vals) // 4]),
                        "category_p75": _fmt(key, vals[(3 * len(vals)) // 4]), "peers_with_data": len(vals)})
            if v is not None and direction:
                pct = _beats(v, vals, direction)
                row["beats_pct_of_peers"] = round(pct * 100)
                row["standing"] = _standing(pct)
        table.append(row)
    by_key = {r["metric"]: r for r in table}

    ranked = sorted((pm["composite_score"], c) for c, pm in peers.items() if pm.get("composite_score") is not None)
    composite = m.get("composite_score")
    rank = 1 + sum(1 for score, _ in ranked if composite is not None and score > composite) if composite is not None else None
    top = sorted(((pm["composite_score"], c) for c, pm in peers.items() if pm.get("composite_score") is not None), reverse=True)[:5]
    names = {c: n for c, n in db.execute(select(Scheme.amfi_code, Scheme.name).where(Scheme.amfi_code.in_([c for _, c in top])))} if top else {}
    top_peers = [{"amfi_code": c, "name": names.get(c), "tier": peers[c].get("tier"), "composite_score": round(score, 1),
                  "cagr_3y_pct": _fmt("cagr_3y", peers[c].get("cagr_3y")), "ter_regular_pct": peers[c].get("ter_regular")} for score, c in top]

    detail = db.get(SchemeDetail, s.amfi_code)
    official = _official(db, s.amfi_code)
    ter = _ter_pair(db, s.amfi_code)
    holdings = _holdings(db, s.amfi_code)
    alpha = (m["cagr_3y"] - m["bench_cagr_3y"]) * 100 if m.get("cagr_3y") is not None and m.get("bench_cagr_3y") is not None else None
    peer_count = len(peers) + 1

    d_profile = {
        "performance": {"standing": (by_key["cagr_3y"].get("standing") or by_key["cagr_1y"].get("standing")),
                        "return_3y_pct": by_key["cagr_3y"]["fund"], "category_median_3y_pct": by_key["cagr_3y"].get("category_median"),
                        "vs_benchmark_proxy_3y_pts": round(alpha, 2) if alpha is not None else None,
                        "benchmark_proxy": benchmark_label(s.category_key),
                        "official_benchmark": official["benchmark"] if official else None},
        "consistency": {"standing": by_key["consistency"].get("standing"), "value_pct": by_key["consistency"]["fund"],
                        "category_median_pct": by_key["consistency"].get("category_median")},
        "risk": {"standing_risk_adjusted": by_key["sortino_3y"].get("standing"), "volatility_standing": by_key["vol_3y"].get("standing"),
                 "drawdown_standing": by_key["max_dd_3y"].get("standing"), "downside_capture_standing": by_key["downside_capture_3y"].get("standing"),
                 "riskometer": (official or {}).get("riskometer") or RISK_LABELS.get(s.riskometer),
                 "riskometer_basis": "AMFI" if official and official.get("riskometer") else "category default"},
        "diversification": {"category_rule": CATEGORY_RULES.get(s.category_key), "r_squared": by_key["r_squared_3y"]["fund"],
                            "tracking_error_pct": by_key["tracking_error_3y"]["fund"], "beta": by_key["beta_3y"]["fund"],
                            "category_median_r_squared": by_key["r_squared_3y"].get("category_median"),
                            "stock_level": holdings,
                            "basis": "portfolio disclosure" if holdings else "NAV behaviour vs index (no portfolio disclosure loaded for this fund; other funds may have one)"},
        "asset_mix": {"look_through_pct": {k: round(v * 100) for k, v in cat.mix.items()}, "category_rule": CATEGORY_RULES.get(s.category_key)},
        "cost": {"standing": by_key["ter_regular"].get("standing"), "expense_ratio": ter,
                 "category_median_regular_pct": by_key["ter_regular"].get("category_median")},
    }

    return {
        "as_of": as_of.isoformat(),
        "fund": {"amfi_code": s.amfi_code, "name": s.name, "amc": s.amc_name, "plan": s.plan_type, "option": s.option_type,
                 "category": cat.label, "launch_date": detail.launch_date.isoformat() if detail and detail.launch_date else None,
                 "minimum_investment": detail.min_amount if detail else None, "aum_cr": (official or {}).get("aum_cr"),
                 "nav": latest[1] if latest else None},
        "asked_about": None if asked.amfi_code == s.amfi_code else {
            "amfi_code": asked.amfi_code, "plan": asked.plan_type, "option": asked.option_type,
            "note": "Compared through its Regular-Growth plan so peers are like for like; a Direct plan's returns are higher by roughly the expense-ratio difference."},
        "peer_set": {"category": cat.label, "funds": peer_count, "definition": "open-ended Regular-plan Growth schemes in the same SEBI category"},
        "rank": {"position": rank, "of": peer_count, "tier": m.get("tier"), "composite_score": composite,
                 "model": m.get("model_version"), "factors_unavailable": m.get("factors_unavailable")} if composite is not None else
                {"position": None, "of": peer_count, "tier": m.get("tier"), "note": "Not ranked: needs 3+ years of NAV history or a peer category."},
        "metrics_vs_category": table,
        "d_profile": d_profile,
        "official_performance": official,
        "top_peers": top_peers,
        "note": "Returns are from AMFI NAVs (annualised where noted). Past performance may or may not be sustained in future.",
    }


def _overlap(db: Session, rows: list[dict]) -> dict:
    """Pairwise holdings overlap from each fund's latest disclosure; names the funds lacking one instead."""
    from app.analytics import xray
    from app.portfolio_service import latest_holdings
    held, months, missing = {}, {}, []
    for r in rows:
        h, month = latest_holdings(db, regular_growth_sibling(db, db.get(Scheme, r["amfi_code"])).amfi_code)
        if h:
            held[r["fund"]], months[r["fund"]] = h, month
        else:
            missing.append(r["fund"])
    if len(rows) < 2:
        return {}
    if missing:
        return {"holdings_overlap_missing": missing}
    pairs = [{"fund_a": p["fund_a"], "fund_b": p["fund_b"], "overlap_pct": round(p["overlap_pct"], 1),
              "common_securities": p["common_securities"]} for p in xray.pairwise_overlap(held)]
    return {"holdings_overlap": {"disclosure_months": months, "pairs": pairs}}


def compare_funds(db: Session, amfi_codes: list[int]) -> dict:
    nb = NavBook(db)
    lenses = [fund_lens(db, c, nb) for c in amfi_codes[:4]]
    keys = ["cagr_1y", "cagr_3y", "cagr_5y", "consistency", "sortino_3y", "vol_3y", "max_dd_3y", "downside_capture_3y", "ter_regular", "r_squared_3y"]
    rows = []
    for lens in lenses:
        by_key = {r["metric"]: r for r in lens["metrics_vs_category"]}
        rows.append({"fund": lens["fund"]["name"], "amfi_code": lens["fund"]["amfi_code"], "category": lens["fund"]["category"],
                     "rank": lens["rank"].get("position"), "of": lens["rank"].get("of"), "tier": lens["rank"].get("tier"),
                     "metrics": {k: {"value": by_key[k]["fund"], "category_median": by_key[k].get("category_median"),
                                     "standing": by_key[k].get("standing")} for k in keys},
                     "aum_cr": lens["fund"]["aum_cr"], "expense_ratio": lens["d_profile"]["cost"]["expense_ratio"]})
    categories = {r["category"] for r in rows}
    overlap = _overlap(db, rows)
    return {**overlap, "as_of": max(lens["as_of"] for lens in lenses) if lenses else None, "same_category": len(categories) == 1,
            "funds": rows, "metric_labels": {k: lbl for k, lbl, _ in METRICS if k in keys},
            "note": "Each fund is ranked within its own category; ranks across different categories aren't comparable."}
