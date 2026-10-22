"""Portfolio X-ray maths: look-through exposure and pairwise overlap. Pure functions, no DB or HTTP.

`holdings` maps fund name -> rows shaped like `SchemeHolding` (dicts or objects with security_isin,
security_name, sector, industry, asset_class, weight_pct). `weight_pct` is % of net assets and may be
negative (short derivative legs); exposure nets them. `fund_values` maps fund name -> rupees held.
"""

from collections import defaultdict
from itertools import combinations

UNCLASSIFIED = "Unclassified"


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def exposure(holdings, fund_values):
    """E(s) = sum_f v_f * w_f(s) / 100; pct = E(s) / total * 100, grouped four ways.

    Securities key on ISIN (falling back to the name for lines without one, e.g. cash). Funds without a
    value are skipped. Each list is sorted by value_inr, largest first."""
    total = sum(fund_values.values())
    groups = {"securities": {}, "sectors": {}, "industries": {}, "asset_classes": {}}
    for fund, rows in holdings.items():
        value = fund_values.get(fund)
        if value is None:
            continue
        for row in rows:
            name = _get(row, "security_name")
            keys = {
                "securities": (_get(row, "security_isin") or name, name),
                "sectors": (_get(row, "sector") or UNCLASSIFIED,) * 2,
                "industries": (_get(row, "industry") or UNCLASSIFIED,) * 2,
                "asset_classes": (_get(row, "asset_class") or UNCLASSIFIED,) * 2,
            }
            amount = value * _get(row, "weight_pct") / 100
            for group, (key, label) in keys.items():
                entry = groups[group].setdefault(key, {"name": label, "value_inr": 0.0, "funds": []})
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
            if isin and weight > 0:
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
