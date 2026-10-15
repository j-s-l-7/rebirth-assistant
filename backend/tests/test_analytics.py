from dataclasses import dataclass
from datetime import date

import pytest

from app.analytics.lots import build_lots, redemption_estimate
from app.analytics.sip import detect_sip
from app.analytics.xirr import xirr


@dataclass
class T:
    txn_date: date
    txn_type: str
    units: float
    amount: float
    nav: float | None = None


def test_xirr_simple_doubling():
    r = xirr([(date(2020, 1, 1), -100_000), (date(2022, 1, 1), 121_000)])
    assert r == pytest.approx(0.10, abs=1e-3)


def test_xirr_rejects_one_sided_flows():
    assert xirr([(date(2020, 1, 1), -1), (date(2021, 1, 1), -1)]) is None


def test_fifo_lots_and_realised_terms():
    txns = [
        T(date(2023, 1, 10), "PURCHASE", 100, 1000, 10),
        T(date(2024, 6, 10), "PURCHASE", 50, 1000, 20),
        T(date(2025, 3, 1), "REDEMPTION", -120, -3000, 25),
    ]
    book = build_lots(txns, "flexi_cap")
    assert book.units == pytest.approx(30)
    assert book.invested == pytest.approx(600)  # 30 units left from the 20/unit lot
    terms = [(r.units, r.term) for r in book.realised]
    assert terms == [(100, "LTCG"), (20, "STCG")]
    assert book.realised[0].gain == pytest.approx(1500)


def test_redemption_estimate_flags_short_term_units():
    txns = [T(date(2026, 6, 1), "PURCHASE", 100, 1000, 10)]
    book = build_lots(txns, "flexi_cap")
    est = redemption_estimate(book, "flexi_cap", 12.0, date(2026, 10, 1), 30.0, 100)
    assert est["stcg_gain"] == pytest.approx(200)
    assert est["exit_load"] == pytest.approx(12.0)  # 1% within 365 days
    assert est["lots"][0]["long_term_from"] == "2027-06-02"


def test_elss_lock_in_blocks_units():
    book = build_lots([T(date(2025, 1, 1), "PURCHASE", 10, 100, 10)], "elss")
    est = redemption_estimate(book, "elss", 12.0, date(2026, 10, 1), 30.0, 10)
    assert est["units_locked"] == 10 and est["units_sellable"] == 0


def test_sip_detection_counts_misses():
    txns = [T(date(2025, m, 5), "PURCHASE_SIP", 10, 5000) for m in range(1, 13) if m not in (4, 5)]
    txns += [T(date(2026, m, 5), "PURCHASE_SIP", 10, 5000) for m in range(1, 10)]
    sip = detect_sip(txns, date(2026, 10, 2))
    assert sip.amount == 5000
    assert sip.status == "active"
    assert sip.missed_12m == 0  # Apr/May 2025 fall outside the last-12-months window
    stale = detect_sip(txns, date(2026, 12, 20))
    assert stale.status == "stopped"
