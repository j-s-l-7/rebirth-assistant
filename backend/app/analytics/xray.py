"""Portfolio X-ray maths: look-through exposure, pairwise overlap, concentration, coverage. Pure functions, no DB or HTTP.

`holdings` maps fund name -> rows shaped like `SchemeHolding` (dicts or objects with security_isin,
security_name, sector, industry, asset_class, weight_pct). `weight_pct` is % of net assets and may be
negative (short derivative legs); exposure nets them; None counts as 0. `fund_values` maps fund name -> rupees held.
"""

from collections import defaultdict
from itertools import combinations

UNCLASSIFIED = "Unclassified"
# Wording no X-ray text may contain (case-insensitive substrings); reused by the wording guard and chat tool.
BANNED_WORDS = ("buy", "sell", "switch", "should", "recommend", "advise", "advice", "better", "best", "ideal",
                "exit", "redeem", "invest in")
LIMITS = {"max_security_pct": "securities", "max_sector_pct": "sectors", "max_industry_pct": "industries",
          "max_amc_pct": "amcs"}
EXPERT_NOTE = "A human expert (EUIN holder) can go through this with you at no cost."
SCREEN_CRITERION = "lowest overlap with your current look-through holdings"
SCREEN_NOTE = "Screen on one criterion (overlap), not a recommendation. It ignores returns, risk, cost and your goals."


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def exposure(holdings, fund_values):
    """E(s) = sum_f v_f * w_f(s) / 100; pct = E(s) / total * 100, grouped four ways.

    Securities key on ISIN (falling back to the name for lines without one, e.g. cash). Funds without a
    value are skipped. Security entries also carry `isin` (None for cash-like lines). Each list is sorted by value_inr, largest first."""
    total = sum(fund_values.values())
    groups = {"securities": {}, "sectors": {}, "industries": {}, "asset_classes": {}}
    for fund, rows in holdings.items():
        value = fund_values.get(fund)
        if value is None:
            continue
        for row in rows:
            weight = _get(row, "weight_pct")
            if not weight:
                continue
            name, isin = _get(row, "security_name"), _get(row, "security_isin")
            keys = {
                "securities": (isin or name, name),
                "sectors": (_get(row, "sector") or UNCLASSIFIED,) * 2,
                "industries": (_get(row, "industry") or UNCLASSIFIED,) * 2,
                "asset_classes": (_get(row, "asset_class") or UNCLASSIFIED,) * 2,
            }
            amount = value * weight / 100
            for group, (key, label) in keys.items():
                entry = groups[group].setdefault(key, {"name": label, "value_inr": 0.0, "funds": []})
                if group == "securities":
                    entry.setdefault("isin", isin)
                entry["value_inr"] += amount
                if fund not in entry["funds"]:
                    entry["funds"].append(fund)
    out = {}
    for group, entries in groups.items():
        for e in entries.values():
            e["pct"] = e["value_inr"] / total * 100 if total else 0.0
        out[group] = sorted(entries.values(), key=lambda e: e["value_inr"], reverse=True)
    return out


def _longs(rows):
    """ISIN -> summed positive weight; ISIN-less lines (cash, TREPS) are not securities here."""
    w = defaultdict(float)
    for row in rows:
        isin, weight = _get(row, "security_isin"), _get(row, "weight_pct")
        if isin and (weight or 0) > 0:
            w[isin] += weight
    return w


def pairwise_overlap(holdings):
    """O(a,b) = sum_s min(w_a(s), w_b(s)) over securities both funds hold long, keyed by ISIN.

    Only positive weights count; lines without an ISIN (cash, TREPS) are not securities here.
    common_securities is the count of shared ISINs. Sorted by overlap_pct, largest first."""
    longs = {fund: _longs(rows) for fund, rows in holdings.items()}
    pairs = []
    for a, b in combinations(longs, 2):
        shared = longs[a].keys() & longs[b].keys()
        pairs.append({
            "fund_a": a,
            "fund_b": b,
            "overlap_pct": sum(min(longs[a][s], longs[b][s]) for s in shared),
            "common_securities": len(shared),
        })
    return sorted(pairs, key=lambda p: p["overlap_pct"], reverse=True)


def concentration(exposures, overlap):
    """From exposure() and pairwise_overlap() output. Securities are ISIN lines with positive pct (cash
    and other ISIN-less lines excluded). share_s = pct_s / sum pct; hhi = sum share_s^2;
    effective_stocks = 1 / hhi; top_sector_pct ignores Unclassified; redundancy_pct = mean overlap_pct."""
    pcts = sorted((e["pct"] for e in exposures["securities"] if e.get("isin") and e["pct"] > 0), reverse=True)
    total = sum(pcts)
    hhi = sum((p / total) ** 2 for p in pcts) if total else 0.0
    sectors = [e["pct"] for e in exposures["sectors"] if e["name"] != UNCLASSIFIED]
    return {
        "top10_pct": sum(pcts[:10]),
        "effective_stocks": 1 / hhi if hhi else 0.0,
        "hhi": hhi,
        "top_sector_pct": max(sectors, default=0.0),
        "redundancy_pct": sum(p["overlap_pct"] for p in overlap) / len(overlap) if overlap else 0.0,
    }


def coverage(fund_values, holdings, disclosure_months, today):
    """covered_value_pct = value of funds with holdings / total value * 100. A disclosure ("YYYY-MM") is
    stale when it is more than 2 calendar months before today's month."""
    total = sum(fund_values.values())
    covered = sum(v for f, v in fund_values.items() if holdings.get(f))
    now = today.year * 12 + today.month
    stale = []
    for fund in fund_values:
        month = disclosure_months.get(fund)
        if month and now - (int(month[:4]) * 12 + int(month[5:7])) > 2:
            stale.append({"fund": fund, "disclosure_month": month})
    return {
        "covered_value_pct": covered / total * 100 if total else 0.0,
        "funds_without_disclosure": [f for f in fund_values if not holdings.get(f)],
        "stale_disclosures": stale,
    }


def objective_checks(exposures, objective):
    """Compare exposure() output with the investor's own limits; limits are only compared, never judged.

    Each max_* limit reports the largest item in its group (Unclassified and ISIN-less lines excluded) as
    within | exceeds. Each `avoid` name found (case-insensitive, whole leading words: "ITC" matches "ITC Limited") in securities, sectors or industries is present."""
    objective = {k: v for k, v in (objective or {}).items() if v not in (None, [], "")}
    if not objective:
        return [{"check": "no_limits_set", "status": "info"}]
    checks = []
    for key, group in LIMITS.items():
        if key not in objective:
            continue
        items = [e for e in exposures.get(group) or []
                 if e["name"] != UNCLASSIFIED and (group != "securities" or e.get("isin"))]
        if not items:
            continue
        top, limit = max(items, key=lambda e: e["pct"]), objective[key]
        checks.append({"check": key, "status": "exceeds" if top["pct"] > limit else "within", "item": top["name"],
                       "actual_pct": top["pct"], "limit_pct": limit, "contributors": top.get("funds", [])})
    for name in objective.get("avoid", []):
        for group in ("securities", "sectors", "industries"):
            want = name.strip().lower()
            hit = next((e for e in exposures.get(group, []) if e["name"].lower() == want or e["name"].lower().startswith(want + " ")), None)
            if hit:
                checks.append({"check": "avoid", "status": "present", "item": hit["name"],
                               "actual_pct": hit["pct"], "contributors": hit.get("funds", [])})
                break
    return checks


def observations(overlap, coverage, disclosure_months, overlap_threshold_pct=50):
    """Neutral, factual notes: high_overlap (pairs at or above the threshold), low_coverage, stale_disclosure.
    Text states numbers and the disclosure month of each fund ("YYYY-MM") and never suggests an action."""
    def basis(fund):
        return f"{fund} (holdings as of {disclosure_months.get(fund) or 'an unknown month'})"

    out = []
    for p in overlap:
        if p["overlap_pct"] >= overlap_threshold_pct:
            out.append({"code": "high_overlap", "funds": [p["fund_a"], p["fund_b"]], "text": (
                f"{basis(p['fund_a'])} and {basis(p['fund_b'])} share {p['overlap_pct']:.1f}% of their portfolios "
                f"by weight ({p['common_securities']} common securities). " + EXPERT_NOTE)})
    if coverage["covered_value_pct"] < 100:
        missing = coverage["funds_without_disclosure"]
        out.append({"code": "low_coverage", "funds": missing, "text": (
            f"Holdings disclosures cover {coverage['covered_value_pct']:.1f}% of portfolio value; no disclosure is "
            f"available for {', '.join(missing)}, so exposure figures leave out that value.")})
    stale = coverage["stale_disclosures"]
    if stale:
        out.append({"code": "stale_disclosure", "funds": [s["fund"] for s in stale], "text": (
            "Holdings for " + ", ".join(f"{s['fund']} ({s['disclosure_month']})" for s in stale)
            + " come from disclosures more than two months old; their figures reflect those months.")})
    return out


def _snapshot(holdings, fund_values):
    """exposure, overlap and concentration over funds with a positive value."""
    live = {f: v for f, v in fund_values.items() if v > 0}
    held = {f: rows for f, rows in holdings.items() if f in live}
    ex, ov = exposure(held, live), pairwise_overlap(held)
    return {"exposures": ex, "overlap": ov, "concentration": concentration(ex, ov)}


def simulate_changes(holdings, fund_values, changes):
    """Apply the investor's own what-if moves [{from_fund, to_fund, amount_inr}] in order; never picks one.

    to_fund needs holdings in `holdings` (it may be new to the portfolio). A move that is not positive, names
    the same fund twice, or takes from_fund below 0 is not applied and says why. deltas lists, per group,
    the 10 largest absolute pct changes (unchanged items left out)."""
    values, applied = dict(fund_values), []
    for c in changes:
        src, dst, amount = c.get("from_fund"), c.get("to_fund"), c.get("amount_inr") or 0
        held = values.get(src, 0)
        if amount <= 0:
            reason = "amount_inr must be above 0"
        elif src == dst:
            reason = "from_fund and to_fund are the same fund"
        elif src not in values:
            reason = f"{src} is not in the portfolio"
        elif not holdings.get(dst):
            reason = f"no holdings disclosure for {dst}"
        elif amount > held:
            reason = f"amount_inr {amount:,.0f} is above the {held:,.0f} held in {src}"
        else:
            values[src] = held - amount
            values[dst] = values.get(dst, 0) + amount
            reason = None
        applied.append({**c, "applied": reason is None, "reason": reason})
    before, after = _snapshot(holdings, fund_values), _snapshot(holdings, values)
    deltas = {}
    for group in ("securities", "sectors", "industries"):
        b, a = ({e.get("isin") or e["name"]: e for e in snap["exposures"][group]} for snap in (before, after))
        rows = []
        for key in b.keys() | a.keys():
            bp, ap = b.get(key, {}).get("pct", 0.0), a.get(key, {}).get("pct", 0.0)
            if abs(ap - bp) > 1e-9:
                rows.append({"name": (a.get(key) or b[key])["name"], "before_pct": bp, "after_pct": ap,
                             "delta_pct": ap - bp})
        deltas[group] = sorted(rows, key=lambda r: (-abs(r["delta_pct"]), r["name"]))[:10]
    return {"before": before, "after": after, "deltas": deltas, "changes_applied": applied}


def low_overlap_screen(holdings, fund_values, candidate_holdings, amount_inr=None, limit=10):
    """Rank candidate funds (name -> rows) by overlap with the current portfolio, lowest first; a screen on
    one criterion, never a single pick.

    overlap_pct = sum_s min(w_c(s), p(s)), p(s) = the portfolio's long look-through pct per ISIN. With
    amount_inr, effect gives top_security_pct_after and effective_stocks_after had that amount gone into
    the candidate. Candidates without holdings rows are left out."""
    p = {e["isin"]: e["pct"] for e in exposure(holdings, fund_values)["securities"] if e.get("isin") and e["pct"] > 0}
    out = []
    for name, rows in candidate_holdings.items():
        if not rows:
            continue
        w = _longs(rows)
        item = {"name": name, "overlap_pct": sum(min(w[s], p[s]) for s in w.keys() & p.keys())}
        if amount_inr:
            values = {**fund_values, name: fund_values.get(name, 0) + amount_inr}
            ex = exposure({**holdings, name: rows}, values)
            item["effect"] = {
                "top_security_pct_after": max((e["pct"] for e in ex["securities"] if e.get("isin")), default=0.0),
                "effective_stocks_after": concentration(ex, [])["effective_stocks"],
            }
        out.append(item)
    out.sort(key=lambda c: (c["overlap_pct"], c["name"]))
    return {"criterion": SCREEN_CRITERION, "amount_inr": amount_inr, "candidates": out[:limit], "note": SCREEN_NOTE}
