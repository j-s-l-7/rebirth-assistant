"""Capital-gains view for the current financial year (estimates, not tax advice)."""

from app.config import get_settings
from app.reference.categories import get_category
from app.reference.tax import CESS, EQUITY_RULES, classify_gain, financial_year, fy_bounds
from app.services.analysis import Analysis


def tax_summary(a: Analysis) -> dict:
    pv = a.pv
    slab = a.investor.tax_slab_pct or get_settings().default_tax_slab_pct
    rule = EQUITY_RULES[-1]
    fy_start, fy_end = fy_bounds(pv.as_of)
    exempt_left = max(0.0, rule.ltcg_exemption - pv.realised_fy.get("equity_LTCG", 0.0))

    unrealised = {"STCG": 0.0, "LTCG": 0.0, "SLAB": 0.0}
    equity_lt_harvestable = 0.0
    upcoming = []
    for h in pv.holdings:
        u = h.unrealised
        unrealised["STCG"] += u.st_gain
        unrealised["LTCG"] += u.lt_gain
        unrealised["SLAB"] += u.slab_gain
        tax_class = get_category(h.scheme.category_key).tax_class
        for lot in h.lots.open_lots:
            gc = classify_gain(tax_class, lot.buy_date, pv.as_of)
            gain = lot.units * h.nav - lot.cost
            if tax_class == "equity" and gc.term == "LTCG" and gain > 0:
                equity_lt_harvestable += gain
            if gc.term != "LTCG" and gc.long_term_on and 0 <= (gc.long_term_on - pv.as_of).days <= 60 and gain > 0:
                upcoming.append({"amfi_code": h.code, "scheme": h.scheme.name, "long_term_from": gc.long_term_on.isoformat(),
                                 "units": round(lot.units, 4), "unrealised_gain": round(gain, 2)})

    realised = pv.realised_fy
    est_tax_realised = (
        max(0.0, realised["STCG"]) * rule.stcg_rate / 100
        + max(0.0, realised["equity_LTCG"] - rule.ltcg_exemption) * rule.ltcg_rate / 100
        + max(0.0, realised["LTCG"] - realised["equity_LTCG"]) * 12.5 / 100
        + max(0.0, realised["SLAB"]) * slab / 100
    ) * (1 + CESS)
    return {
        "financial_year": financial_year(pv.as_of),
        "fy_window": [fy_start.isoformat(), fy_end.isoformat()],
        "realised_this_fy": {
            "short_term_equity_gain": round(realised["STCG"], 2),
            "long_term_gain": round(realised["LTCG"], 2),
            "of_which_equity_long_term": round(realised["equity_LTCG"], 2),
            "slab_taxed_gain": round(realised["SLAB"], 2),
            "estimated_tax": round(est_tax_realised, 2),
        },
        "equity_ltcg_exemption": {"limit": rule.ltcg_exemption, "used": round(min(rule.ltcg_exemption, realised["equity_LTCG"]), 2), "remaining": round(exempt_left, 2)},
        "unrealised_if_sold_today": {
            "short_term_gain": round(unrealised["STCG"], 2),
            "long_term_gain": round(unrealised["LTCG"], 2),
            "slab_taxed_gain": round(unrealised["SLAB"], 2),
        },
        "ltcg_harvest_opportunity": round(min(exempt_left, equity_lt_harvestable), 2),
        "lots_turning_long_term_in_60_days": sorted(upcoming, key=lambda x: x["long_term_from"]),
        "rates_used": {"equity_stcg_pct": rule.stcg_rate, "equity_ltcg_pct": rule.ltcg_rate, "non_equity_ltcg_pct": 12.5,
                       "slab_pct_assumed": slab, "cess_pct": CESS * 100, "surcharge": "not modelled"},
        "label": "Estimate from your transactions using FIFO; not tax advice. Consult a CA for filing.",
    }
