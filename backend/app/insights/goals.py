"""Goal-based SIP maths (AMFI FAQ Q4: goal-based MF suggestions are within an MFD's remit).

Return assumptions are illustrative ranges, never predictions; outputs always carry them.
"""

from datetime import date

from app.reference.risk import TEMPLATES

ASSUMED_RETURNS = {  # annual, (low, base, high)
    "equity": (0.09, 0.11, 0.13),
    "debt": (0.06, 0.0675, 0.075),
    "gold": (0.06, 0.08, 0.10),
}
ASSUMPTION_NOTE = "Illustrative assumptions, not predictions. Mutual fund returns are not guaranteed."


def blended_returns(bucket: str) -> tuple[float, float, float]:
    target = TEMPLATES[bucket].target
    out = []
    for i in range(3):
        out.append(sum(target[a] / 100 * ASSUMED_RETURNS[a][i] for a in target))
    return out[0], out[1], out[2]


def _fv_lumpsum(pv: float, r: float, months: int) -> float:
    return pv * (1 + r) ** (months / 12)


def _fv_sip(sip: float, r: float, months: int) -> float:
    rm = (1 + r) ** (1 / 12) - 1
    if rm == 0:
        return sip * months
    return sip * (((1 + rm) ** months - 1) / rm) * (1 + rm)


def required_sip(target: float, r: float, months: int, existing: float = 0.0) -> float:
    gap = target - _fv_lumpsum(existing, r, months)
    if gap <= 0:
        return 0.0
    unit = _fv_sip(1.0, r, months)
    return gap / unit if unit else gap


def months_until(target_date: date, today: date | None = None) -> int:
    today = today or date.today()
    return max(1, (target_date.year - today.year) * 12 + (target_date.month - today.month))


def goal_plan(target_amount: float, target_date: date, bucket: str, existing: float = 0.0, monthly_sip: float = 0.0,
              inflation: float | None = None, today: date | None = None) -> dict:
    months = months_until(target_date, today)
    years = months / 12
    future_target = target_amount * (1 + inflation) ** years if inflation else target_amount
    lo, base, hi = blended_returns(bucket)
    scenarios = {}
    for name, r in (("cautious", lo), ("base", base), ("optimistic", hi)):
        corpus = _fv_lumpsum(existing, r, months) + _fv_sip(monthly_sip, r, months)
        scenarios[name] = {
            "assumed_return_pct": round(r * 100, 2),
            "projected_corpus": round(corpus, 0),
            "shortfall": round(max(0.0, future_target - corpus), 0),
            "sip_needed_for_target": round(required_sip(future_target, r, months, existing), 0),
        }
    return {
        "months": months,
        "target_today_rupees": round(target_amount, 0),
        "target_future_rupees": round(future_target, 0),
        "inflation_pct": round(inflation * 100, 2) if inflation else None,
        "existing_corpus": round(existing, 0),
        "current_monthly_sip": round(monthly_sip, 0),
        "risk_mix": TEMPLATES[bucket].target,
        "scenarios": scenarios,
        "note": ASSUMPTION_NOTE,
        "horizon_warning": "Goals under 3 years away are better served by debt funds than equity." if years < 3 else None,
    }
