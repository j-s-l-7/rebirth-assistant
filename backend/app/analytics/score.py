"""RA Score: an explainable 0-100 portfolio score (PRD PF-5, extended).

Six dimensions, each scored 0-100 with a one-line summary, the inputs behind it and what
would raise it. A dimension without enough data is reported as "not_scored" and its
weight is redistributed - the score never pretends to know what it can't see.

  discipline       steady investing: SIP continuity, months invested
  balance          asset mix (equity / debt / gold) vs the risk profile's bands
  risk_fit         share of money in funds suitable for the investor's profile
  diversification  concentration by fund, AMC and sector; clutter; overlap
  performance      returns vs benchmark on the investor's *own* cashflows, peer tiers
  efficiency       money leaks: idle cash, short-term churn, IDCW drag
"""

from collections import defaultdict

from app.analytics.portfolio import PortfolioView
from app.analytics.sip import months_invested
from app.models import RiskProfile
from app.reference.risk import BUCKET_LABELS, TEMPLATES, suitability

SCORE_VERSION = "ra-score-v1"
# Weights and penalty slopes are v1 calibration defaults (sign-off: Jayesh, J-06/J-09).
DIMENSIONS = [
    ("discipline", "Discipline", 15),
    ("balance", "Balance", 20),
    ("risk_fit", "Risk fit", 20),
    ("diversification", "Diversification", 15),
    ("performance", "Performance", 20),
    ("efficiency", "Efficiency", 10),
]
BALANCE_PENALTY_PER_PP = 3.5  # points lost per percentage point outside a band
MISFIT_PENALTY_PER_PCT = 2.0  # points lost per % of money in unsuitable funds
TIER_SCORE = {"core": 88, "satellite": 68, "watch": 42, "exit": 15}


def _clamp(x: float) -> int:
    return int(round(max(0.0, min(100.0, x))))


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def grade(score: int) -> str:
    return "Strong" if score >= 85 else "Good" if score >= 70 else "Fair" if score >= 55 else "Needs attention"


def _discipline(pv: PortfolioView) -> dict:
    if not pv.has_transactions:
        return {"status": "not_scored", "summary": "Needs a detailed CAS with transactions to read your investing pattern."}
    m12 = months_invested(pv.all_txns, pv.as_of, 12)
    sips = [h.sip for h in pv.holdings if h.sip]
    active = [s for s in sips if s.status == "active"]
    lapsing = [s for s in sips if s.status == "missed_latest"]
    stopped = [s for s in sips if s.status == "stopped"]
    missed = sum(s.missed_12m for s in sips if s.status != "stopped")
    if active:
        health = max(0.3, 1 - 0.12 * missed - 0.15 * len(lapsing))
    elif lapsing:
        health = 0.5
    elif stopped:
        health = 0.25
    else:
        health = 0.0
    score = _clamp(100 * (0.55 * m12 / 12 + 0.45 * health))
    improve = []
    if missed:
        improve.append(f"{missed} SIP instalment(s) missed in the last 12 months; keep the mandate funded so SIPs don't bounce.")
    if lapsing:
        improve.append(f"{len(lapsing)} SIP(s) missed their latest instalment; check the bank mandate.")
    if not active and not lapsing:
        improve.append("No running SIP: a monthly SIP keeps you investing through ups and downs.")
    if m12 < 9:
        improve.append("Invest more regularly: money went in during only some months this year.")
    return {
        "status": "scored",
        "score": score,
        "summary": f"Invested in {m12} of the last 12 months; {len(active)} active SIP(s), {missed} missed instalment(s).",
        "inputs": {"months_invested_12m": m12, "active_sips": len(active), "lapsing_sips": len(lapsing), "stopped_sips": len(stopped), "missed_instalments_12m": missed},
        "improve": improve,
    }


def _balance(pv: PortfolioView, risk: RiskProfile | None) -> dict:
    alloc = pv.allocation
    if risk is None:
        # Without a profile, apply a universal cushion rule: >=10% debt and >=5% gold.
        gaps = []
        if alloc["debt"] < 0.10:
            gaps.append(("debt", 0.10 - alloc["debt"]))
        if alloc["gold"] < 0.05:
            gaps.append(("gold", 0.05 - alloc["gold"]))
        score = _clamp(100 - sum(g for _, g in gaps) * 300)
        return {
            "status": "scored",
            "score": score,
            "summary": f"Equity {_pct(alloc['equity'])}, debt {_pct(alloc['debt'])}, gold {_pct(alloc['gold'])}. Scored on a basic cushion rule until you complete your risk profile.",
            "inputs": {"allocation": {k: round(v * 100, 1) for k, v in alloc.items()}, "basis": "cushion_rule"},
            "improve": [f"Build a {a} cushion (about {_pct(g)} of the portfolio is missing)." for a, g in gaps] + ["Take the risk profile quiz for a personal target mix."],
        }
    t = TEMPLATES[risk.bucket]
    outside, notes = 0.0, []
    for asset, (lo, hi) in t.bands.items():
        actual = alloc.get(asset, 0.0) * 100
        if actual < lo:
            outside += lo - actual
            notes.append(f"{asset.title()} {actual:.0f}% is below your {lo:.0f}-{hi:.0f}% band")
        elif actual > hi:
            outside += actual - hi
            notes.append(f"{asset.title()} {actual:.0f}% is above your {lo:.0f}-{hi:.0f}% band")
    score = _clamp(100 - BALANCE_PENALTY_PER_PP * outside)
    summary = "; ".join(notes) if notes else f"Within your {BUCKET_LABELS[risk.bucket]} bands for equity, debt and gold."
    return {
        "status": "scored",
        "score": score,
        "summary": summary + ".",
        "inputs": {"allocation": {k: round(v * 100, 1) for k, v in alloc.items()}, "target": t.target, "bands": {k: list(v) for k, v in t.bands.items()}, "points_outside_bands": round(outside, 1)},
        "improve": ["Steer new SIPs toward the under-weight asset first; switch only if the gap persists."] if notes else [],
    }


def _risk_fit(pv: PortfolioView, risk: RiskProfile | None) -> dict:
    if risk is None:
        return {"status": "not_scored", "summary": "Complete the risk profile quiz to check whether your funds fit you."}
    misfits, fit_value = [], 0.0
    for h in pv.holdings:
        s = suitability(risk.bucket, h.scheme.category_key, risk.horizon_years)
        if s["pass"]:
            fit_value += h.value
        else:
            misfits.append({"name": h.scheme.name, "weight_pct": round(h.weight * 100, 1), "reason": s["reason"]})
    share = fit_value / pv.value if pv.value else 1.0
    return {
        "status": "scored",
        "score": _clamp(100 - MISFIT_PENALTY_PER_PCT * (1 - share) * 100),
        "summary": f"{_pct(share)} of your money is in funds that fit a {BUCKET_LABELS[risk.bucket]} profile.",
        "inputs": {"suitable_share_pct": round(share * 100, 1), "misfits": misfits},
        "improve": [f"{m['name']} ({m['weight_pct']}%): {m['reason']}." for m in misfits[:3]],
    }


def _diversification(pv: PortfolioView, overlap: dict | None) -> dict:
    penalties: list[tuple[float, str]] = []
    n = len(pv.holdings)
    if n == 0:
        return {"status": "not_scored", "summary": "No holdings yet."}
    top = pv.holdings[0]
    if top.weight > 0.30:
        penalties.append(((top.weight - 0.30) * 120, f"{top.scheme.name} is {_pct(top.weight)} of the portfolio"))
    eq = [h for h in pv.holdings if h.category.mix.get("equity", 0) >= 0.65]
    eq_value = sum(h.value for h in eq)
    by_amc = defaultdict(float)
    for h in eq:
        by_amc[h.scheme.amc_name] += h.value
    if eq_value and by_amc:
        amc, v = max(by_amc.items(), key=lambda kv: kv[1])
        if v / eq_value > 0.40 and len(eq) > 1:
            penalties.append(((v / eq_value - 0.40) * 80, f"{_pct(v / eq_value)} of equity is with {amc}"))
    by_cat = defaultdict(list)
    for h in pv.holdings:
        by_cat[h.category.label].append(h)
    for label, hs in by_cat.items():
        if len(hs) > 2:
            penalties.append((8 * (len(hs) - 2), f"{len(hs)} {label} funds"))
    if n > 10:
        penalties.append((4 * (n - 10), f"{n} funds is more than you need"))
    if overlap:
        for p in overlap.get("pairs", []):
            if p["overlap_pct"] and p["overlap_pct"] > 50:
                penalties.append((10, f"{p['a']} and {p['b']} share {p['overlap_pct']:.0f}% of holdings"))
        for sector, pct in list(overlap.get("sector_exposure_pct", {}).items())[:1]:
            if pct > 25 and sector != "Unclassified":
                penalties.append(((pct - 25) * 1.5, f"{sector} is {pct:.0f}% of your equity"))
    sector_funds = [h for h in pv.holdings if h.scheme.category_key in ("sectoral", "thematic")]
    sector_weight = sum(h.weight for h in sector_funds)
    if sector_weight > 0.15:
        penalties.append(((sector_weight - 0.15) * 100, f"{_pct(sector_weight)} in sector/thematic bets"))
    if n < 3 and pv.value > 100_000:
        penalties.append((10, "Only one or two funds"))
    score = _clamp(100 - sum(p for p, _ in penalties))
    return {
        "status": "scored",
        "score": score,
        "summary": ("; ".join(why for _, why in penalties[:2]) + ".") if penalties else f"Well spread across {n} funds with no single fund, AMC or sector dominating.",
        "inputs": {"funds": n, "largest_holding_pct": round(top.weight * 100, 1), "equity_amc_max_pct": round(max(by_amc.values()) / eq_value * 100, 1) if eq_value and by_amc else None,
                   "overlap_basis": "portfolio_disclosure" if overlap and overlap.get("disclosure_coverage_pct") else "same_category_estimate"},
        "improve": [why for _, why in penalties[:3]],
    }


def _performance(pv: PortfolioView) -> dict:
    scored, rows = 0.0, []
    for h in pv.holdings:
        if h.first_invested and (pv.as_of - h.first_invested).days < 365:
            continue
        shadow, metrics = h.extras.get("vs_benchmark"), h.extras.get("metrics") or {}
        if shadow:
            s = 70 + 12 * shadow["alpha_pp"]
            rows.append((h, s, f"{shadow['alpha_pp']:+.1f} pts/yr vs {shadow['benchmark']}"))
        elif metrics.get("tier") in TIER_SCORE:
            rows.append((h, TIER_SCORE[metrics["tier"]], f"{metrics['tier'].title()} tier among category peers"))
        else:
            continue
        scored += h.value
    total = sum(h.value for h in pv.holdings)
    if not rows or (total and scored / total < 0.3):
        return {"status": "not_scored", "summary": "Not enough return history yet (funds held under a year, or missing benchmark/peer data)."}
    score = _clamp(sum(h.value * max(0, min(100, s)) for h, s, _ in rows) / scored)
    with_alpha = [(h, h.extras["vs_benchmark"]) for h, _, _ in rows if h.extras.get("vs_benchmark")]
    lead = ""
    if with_alpha:
        w = sum(h.value for h, _ in with_alpha)
        avg_alpha = sum(h.value * a["alpha_pp"] for h, a in with_alpha) / w
        lead = f"On your actual investments, benchmarked funds {'beat' if avg_alpha >= 0 else 'trailed'} their index by {abs(avg_alpha):.1f} pts a year on average. "
    laggards = sorted(rows, key=lambda r: r[1])[:2]
    return {
        "status": "scored",
        "score": score,
        "summary": (lead + f"Scored on {_pct(scored / total)} of the portfolio.").strip(),
        "inputs": {"per_fund": [{"name": h.scheme.name, "score": _clamp(s), "basis": why} for h, s, why in rows], "coverage_pct": round(scored / total * 100, 1)},
        "improve": [f"{h.scheme.name}: {why}" for h, s, why in laggards if s < 55],
    }


def _efficiency(pv: PortfolioView) -> dict:
    penalties = []
    if pv.cash_like_share > 0.20:
        penalties.append((25 if pv.cash_like_share <= 0.35 else 40, f"{_pct(pv.cash_like_share)} parked in liquid/overnight-type funds"))
    idcw = sum(h.weight for h in pv.holdings if h.scheme.option_type == "idcw")
    if idcw > 0.10:
        penalties.append((10, f"{_pct(idcw)} in IDCW options (payouts are taxed at your slab every year)"))
    st_gain = sum(r.gain for h in pv.holdings for r in h.lots.realised if r.term == "STCG" and (pv.as_of - r.sell_date).days <= 365 and r.gain > 0)
    if pv.value and st_gain / pv.value > 0.02:
        penalties.append((20, f"Short-term gains of {st_gain:,.0f} booked in the last year (taxed at 20%)"))
    score = _clamp(100 - sum(p for p, _ in penalties))
    return {
        "status": "scored",
        "score": score,
        "summary": ("; ".join(w for _, w in penalties) + ".") if penalties else "No obvious leaks: little idle cash, no short-term churn.",
        "inputs": {"cash_like_pct": round(pv.cash_like_share * 100, 1), "idcw_pct": round(idcw * 100, 1), "stcg_booked_12m": round(st_gain, 2)},
        "improve": [w for _, w in penalties],
    }


def compute_score(pv: PortfolioView, risk: RiskProfile | None, overlap: dict | None = None) -> dict:
    parts = {
        "discipline": _discipline(pv),
        "balance": _balance(pv, risk),
        "risk_fit": _risk_fit(pv, risk),
        "diversification": _diversification(pv, overlap),
        "performance": _performance(pv),
        "efficiency": _efficiency(pv),
    }
    dims, weighted, weights = [], 0.0, 0
    for key, label, w in DIMENSIONS:
        p = parts[key]
        dims.append({"key": key, "label": label, "weight": w, "score": p.get("score"), "status": p["status"], "summary": p["summary"],
                     "inputs": p.get("inputs", {}), "improve": p.get("improve", [])})
        if p["status"] == "scored":
            weighted += w * p["score"]
            weights += w
    total = _clamp(weighted / weights) if weights else None
    scored_dims = [d for d in dims if d["status"] == "scored"]
    weakest = sorted(scored_dims, key=lambda d: d["score"])[:2]
    return {
        "version": SCORE_VERSION,
        "as_of": pv.as_of.isoformat(),
        "score": total,
        "grade": grade(total) if total is not None else None,
        "dimensions": dims,
        "coverage": f"{len(scored_dims)} of {len(dims)} dimensions scored",
        "focus": [{"dimension": d["label"], "score": d["score"], "first_step": d["improve"][0] if d["improve"] else d["summary"]} for d in weakest],
        "how_scores_move": "Higher is better. Each dimension is 0-100; the total is their weighted average over the dimensions we could score.",
    }
