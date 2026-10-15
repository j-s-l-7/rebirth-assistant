"""Proactive nudges (PRD AI-6): the things worth hearing about before you'd think to ask."""

from datetime import date, timedelta

from app.insights.tax import tax_summary
from app.reference.categories import get_category
from app.reference.risk import BUCKET_LABELS
from app.reference.tax import classify_gain
from app.services.analysis import Analysis

SEVERITY_ORDER = {"action": 0, "heads_up": 1, "info": 2}


def _days(d: date, today: date) -> int:
    return (d - today).days


def nudges(a: Analysis) -> list[dict]:
    pv, today = a.pv, a.pv.as_of
    out: list[dict] = []

    if a.risk is None:
        out.append({"type": "risk_profile_missing", "severity": "action", "title": "Complete your risk profile",
                    "body": "Fund suggestions and the Risk fit score need it. It takes about 2 minutes."})
    elif a.risk.valid_till < today:
        out.append({"type": "risk_profile_due", "severity": "action", "title": "Time to refresh your risk profile",
                    "body": f"Your {BUCKET_LABELS[a.risk.bucket]} profile is over a year old. Life changes; your mix should keep up."})

    for h in pv.holdings:
        cat = get_category(h.scheme.category_key)
        for lot in h.lots.open_lots:
            gc = classify_gain(cat.tax_class, lot.buy_date, today)
            gain = lot.units * h.nav - lot.cost
            if gc.term != "LTCG" and gc.long_term_on and 0 <= _days(gc.long_term_on, today) <= 30 and gain > 2000:
                out.append({"type": "lt_conversion_soon", "severity": "heads_up", "amfi_code": h.code, "due_date": gc.long_term_on.isoformat(),
                            "title": f"Hold {h.scheme.name} a few more days before selling",
                            "body": f"Units bought on {lot.buy_date.isoformat()} turn long-term on {gc.long_term_on.isoformat()}. Selling after that date moves ₹{gain:,.0f} of gain to the lower long-term rate."})
            if cat.exit_load_days:
                ends = lot.buy_date + timedelta(days=cat.exit_load_days)
                if 0 <= _days(ends, today) <= 21 and lot.units * h.nav > 10000:
                    out.append({"type": "exit_load_ending", "severity": "info", "amfi_code": h.code, "due_date": ends.isoformat(),
                                "title": f"Exit-load window ends soon on {h.scheme.name}",
                                "body": f"Units bought on {lot.buy_date.isoformat()} come out of the typical {cat.exit_load_pct:g}% exit-load period on {ends.isoformat()} (check the scheme's KIM)."})
            if cat.lock_in_days:
                unlock = lot.buy_date + timedelta(days=cat.lock_in_days)
                if 0 <= _days(unlock, today) <= 30:
                    out.append({"type": "lock_in_ending", "severity": "info", "amfi_code": h.code, "due_date": unlock.isoformat(),
                                "title": f"{h.scheme.name} units unlock soon",
                                "body": f"Units bought on {lot.buy_date.isoformat()} finish their lock-in on {unlock.isoformat()}."})
        if h.sip and h.sip.status == "missed_latest":
            out.append({"type": "sip_missed", "severity": "action", "amfi_code": h.code, "title": f"SIP in {h.scheme.name} missed its last instalment",
                        "body": f"No instalment since {h.sip.last_date.isoformat()}. Check the bank mandate so it doesn't lapse."})
        m = h.extras.get("metrics") or {}
        if m.get("tier") == "exit":
            out.append({"type": "laggard", "severity": "heads_up", "amfi_code": h.code, "title": f"{h.scheme.name} is lagging its peers",
                        "body": "It ranks in the bottom tier of its category on consistency, risk-adjusted return and downside protection. See Recommendations for options."})

    if today.month in (1, 2, 3):
        t = tax_summary(a)
        opp = t["ltcg_harvest_opportunity"]
        if opp > 10000:
            out.append({"type": "ltcg_harvest", "severity": "heads_up", "due_date": f"{today.year}-03-31",
                        "title": "Use this year's tax-free long-term gains",
                        "body": f"₹{opp:,.0f} of long-term equity gains could be booked and reinvested before 31 March without tax, resetting your cost higher. Estimate, not tax advice."})

    if pv.cash_like_share > 0.20:
        out.append({"type": "idle_cash", "severity": "heads_up", "title": "A lot of money is sitting in cash-like funds",
                    "body": f"{pv.cash_like_share * 100:.0f}% of your portfolio is in liquid/overnight-type funds. If it isn't an emergency fund, a gradual transfer can put it to work."})

    for d in a.score["dimensions"]:
        if d["key"] == "balance" and d["status"] == "scored" and d["score"] < 70:
            out.append({"type": "drift", "severity": "heads_up", "title": "Your mix has drifted", "body": d["summary"]})

    return sorted(out, key=lambda n: (SEVERITY_ORDER[n["severity"]], n.get("due_date") or "9999"))
