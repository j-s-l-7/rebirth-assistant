"""XIRR: the annualised internal rate of return of dated cashflows.

Convention: money invested is negative, money received (redemptions, current value) positive.
"""

from datetime import date


def _npv(rate: float, flows: list[tuple[float, float]]) -> float:
    return sum(cf / (1.0 + rate) ** t for t, cf in flows)


def _dnpv(rate: float, flows: list[tuple[float, float]]) -> float:
    return sum(-t * cf / (1.0 + rate) ** (t + 1) for t, cf in flows)


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    flows = [(d, float(a)) for d, a in cashflows if a]
    if len(flows) < 2 or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    flows.sort(key=lambda x: x[0])
    t0 = flows[0][0]
    tf = [((d - t0).days / 365.0, a) for d, a in flows]
    if tf[-1][0] <= 0:
        return None

    # Newton-Raphson from a sensible start, then bracketed bisection as a fallback.
    rate = 0.1
    for _ in range(100):
        try:
            f, df = _npv(rate, tf), _dnpv(rate, tf)
        except (OverflowError, ZeroDivisionError):
            break
        if df == 0:
            break
        nxt = rate - f / df
        if nxt <= -0.9999:
            nxt = (rate - 0.9999) / 2
        if abs(nxt - rate) < 1e-9:
            return nxt
        rate = nxt

    lo, hi = -0.9999, 10.0
    try:
        f_lo, f_hi = _npv(lo, tf), _npv(hi, tf)
    except OverflowError:
        return None
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = _npv(mid, tf)
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def cagr(start_value: float, end_value: float, years: float) -> float | None:
    if start_value <= 0 or end_value <= 0 or years <= 0:
        return None
    return (end_value / start_value) ** (1 / years) - 1
