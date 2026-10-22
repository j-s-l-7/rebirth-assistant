"""Portfolio X-ray maths: look-through exposure, pairwise overlap, concentration, coverage. Pure functions, no DB or HTTP.

`holdings` maps fund name -> rows shaped like `SchemeHolding` (dicts or objects with security_isin,
security_name, sector, industry, asset_class, weight_pct). `weight_pct` is % of net assets and may be
negative (short derivative legs); exposure nets them; None counts as 0. `fund_values` maps fund name -> rupees held.
"""

from collections import defaultdict
from itertools import combinations

UNCLASSIFIED = "Unclassified"


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


def pairwise_overlap(holdings):
    """O(a,b) = sum_s min(w_a(s), w_b(s)) over securities both funds hold long, keyed by ISIN.

    Only positive weights count; lines without an ISIN (cash, TREPS) are not securities here.
    common_securities is the count of shared ISINs. Sorted by overlap_pct, largest first."""
    longs = {}
    for fund, rows in holdings.items():
        w = defaultdict(float)
        for row in rows:
            isin, weight = _get(row, "security_isin"), _get(row, "weight_pct")
            if isin and (weight or 0) > 0:
                w[isin] += weight
        longs[fund] = w
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
