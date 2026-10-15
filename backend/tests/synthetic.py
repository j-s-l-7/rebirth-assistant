"""Deterministic synthetic market: schemes with random-walk NAVs, so tests run offline."""

import math
import random
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.data.bulk import copy_nav_points, upsert
from app.models import Scheme
from app.reference.categories import BENCHMARK_PROXIES, get_category

START, END = date(2019, 1, 1), date(2026, 10, 14)

# category -> (count, annual drift, annual vol)
PEERS = {
    "large_cap": (5, 0.12, 0.16),
    "flexi_cap": (5, 0.14, 0.17),
    "mid_cap": (4, 0.17, 0.21),
    "small_cap": (4, 0.19, 0.25),
    "large_mid_cap": (4, 0.15, 0.19),
    "elss": (3, 0.14, 0.17),
    "thematic": (2, 0.13, 0.24),
    "corporate_bond": (4, 0.072, 0.02),
    "short_duration": (3, 0.068, 0.018),
    "money_market": (3, 0.065, 0.006),
    "liquid": (2, 0.062, 0.003),
    "gold": (3, 0.10, 0.14),
}
PROXY_DRIFT = {"nifty50": (0.12, 0.16), "nifty500": (0.135, 0.17), "midcap150": (0.17, 0.2), "smallcap250": (0.18, 0.24), "largemidcap250": (0.15, 0.18)}


def business_days(a: date, b: date):
    d = a
    while d <= b:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def walk(code: int, drift: float, vol: float, start_nav: float = 10.0, start: date = START) -> list[tuple[int, date, float]]:
    rng = random.Random(code)
    dt = 1 / 252
    nav, out = start_nav, []
    for d in business_days(start, END):
        nav *= math.exp((drift - vol * vol / 2) * dt + vol * math.sqrt(dt) * rng.gauss(0, 1))
        out.append((code, d, round(nav, 4)))
    return out


def seed_market(db: Session) -> dict[str, list[int]]:
    schemes, points, by_cat = [], [], {}
    code = 900001
    for cat_key, (n, drift, vol) in PEERS.items():
        by_cat[cat_key] = []
        for i in range(n):
            # Spread drifts so peers rank differently; the last scheme in each category lags.
            d = drift + (0.03 - 0.015 * i) * (0.25 if get_category(cat_key).group == "debt" else 1)
            schemes.append({"amfi_code": code, "name": f"Test {get_category(cat_key).label} Fund {i + 1} - Regular Plan - Growth",
                            "amc_name": f"Test AMC {i % 3 + 1}", "raw_category": f"Open Ended Schemes(Test - {cat_key})", "category_key": cat_key,
                            "plan_type": "regular", "option_type": "growth", "isin_growth": f"INFTEST{code % 100000:05d}", "isin_reinvest": None,
                            "latest_nav": None, "latest_nav_date": None, "riskometer": get_category(cat_key).riskometer,
                            "riskometer_source": "category_default", "in_universe": True, "is_benchmark_proxy": False})
            points += walk(code, d, vol)
            by_cat[cat_key].append(code)
            code += 1
    for key, meta in BENCHMARK_PROXIES.items():
        drift, vol = PROXY_DRIFT[key]
        schemes.append({"amfi_code": meta["amfi_code"], "name": meta["label"], "amc_name": "Index AMC", "raw_category": "Index", "category_key": "index_equity",
                        "plan_type": "regular", "option_type": "growth", "isin_growth": None, "isin_reinvest": None, "latest_nav": None,
                        "latest_nav_date": None, "riskometer": "very_high", "riskometer_source": "category_default", "in_universe": True, "is_benchmark_proxy": True})
        points += walk(meta["amfi_code"], drift, vol)
    last = {}
    for c, d, n in points:
        last[c] = (d, n)
    for s in schemes:
        s["latest_nav_date"], s["latest_nav"] = last[s["amfi_code"]]
    upsert(db, Scheme, schemes, ["amfi_code"], [k for k in schemes[0] if k != "amfi_code"])
    db.commit()
    copy_nav_points(db, points)
    db.commit()
    return by_cat


def cas_dict(market: dict[str, list[int]], pan: str | None = None) -> dict:
    """A casparser-shaped dict: SIPs in flexi/mid, a small-cap lumpsum, liquid cash, a laggard large-cap."""
    from app.analytics.navbook import NavBook
    from app.db import SessionLocal

    db = SessionLocal()
    nb = NavBook(db)

    def txns(code: int, plan: list[tuple[date, float]]) -> tuple[list[dict], float]:
        out, bal = [], 0.0
        for d, amt in plan:
            hit = nb.nav_on_or_after(code, d)
            units = round(amt / hit[1], 3)
            bal += units
            out.append({"date": hit[0].isoformat(), "description": "SIP Purchase", "amount": amt, "units": units, "nav": hit[1],
                        "balance": round(bal, 3), "type": "PURCHASE_SIP" if amt < 50000 else "PURCHASE"})
        return out, bal

    def monthly(start: date, amount: float, end: date = date(2026, 10, 5)):
        d, out = start, []
        while d <= end:
            out.append((d, amount))
            d = date(d.year + (d.month // 12), d.month % 12 + 1, d.day)
        return out

    holdings = [
        (market["flexi_cap"][0], monthly(date(2021, 1, 5), 10000)),
        (market["flexi_cap"][1], monthly(date(2022, 6, 10), 5000)),
        (market["flexi_cap"][4], monthly(date(2022, 1, 10), 3000)),
        (market["mid_cap"][0], monthly(date(2022, 1, 7), 5000)),
        (market["small_cap"][0], [(date(2021, 10, 18), 150000)]),
        (market["large_cap"][4], [(date(2020, 3, 16), 200000)]),
        (market["liquid"][0], [(date(2025, 2, 3), 300000)]),
    ]
    schemes = []
    for code, plan in holdings:
        ts, bal = txns(code, plan)
        nav = nb.latest(code)[1]
        schemes.append({"scheme": f"Scheme {code}", "isin": None, "amfi": str(code), "rta_code": "X", "rta": "CAMS", "open": 0,
                        "close": round(bal, 3), "close_calculated": round(bal, 3),
                        "valuation": {"date": nb.latest(code)[0].isoformat(), "nav": nav, "value": round(bal * nav, 2), "cost": sum(a for _, a in plan)},
                        "transactions": ts})
    db.close()
    return {"statement_period": {"from": "2019-01-01", "to": "2026-10-14"}, "cas_type": "DETAILED", "file_type": "CAMS",
            "investor_info": {"name": "TEST", "email": "", "address": "", "mobile": ""},
            "folios": [{"folio": "TEST/01", "amc": "Test AMC 1", "PAN": pan, "schemes": schemes}]}
