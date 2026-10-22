"""X-ray maths: hand-computed exposure and overlap cases."""

from dataclasses import dataclass

from pytest import approx

from app.analytics.xray import exposure, pairwise_overlap


@dataclass
class Row:
    security_isin: str | None
    security_name: str
    sector: str | None
    industry: str | None
    asset_class: str
    weight_pct: float


def _by_name(items):
    return {e["name"]: e for e in items}


def test_two_long_only_funds():
    # A = Rs 60,000, B = Rs 40,000, total Rs 1,00,000.
    holdings = {
        "A": [
            Row("INE1", "X Bank", "Financials", "Banks", "equity", 40),
            Row("INE2", "Y Soft", "IT", "Software", "equity", 30),
            Row("INE3", "Z Bank", "Financials", "Banks", "equity", 20),
            Row(None, "Cash", None, None, "cash", 10),
        ],
        "B": [
            Row("INE1", "X Bank", "Financials", "Banks", "equity", 10),
            Row("INE2", "Y Soft", "IT", "Software", "equity", 50),
            Row("INE4", "W Oil", "Energy", "Oil", "equity", 30),
            Row(None, "Cash", None, None, "cash", 10),
        ],
    }
    ex = exposure(holdings, {"A": 60_000, "B": 40_000})
    sec = _by_name(ex["securities"])
    # X: 600*40 + 400*10 = 28,000; Y: 18,000 + 20,000 = 38,000
    assert sec["X Bank"]["value_inr"] == approx(28_000) and sec["X Bank"]["pct"] == approx(28)
    assert sec["Y Soft"]["value_inr"] == approx(38_000) and sec["Y Soft"]["pct"] == approx(38)
    assert sec["Z Bank"]["pct"] == approx(12) and sec["Z Bank"]["funds"] == ["A"]
    assert sec["W Oil"]["pct"] == approx(12) and sec["W Oil"]["funds"] == ["B"]
    assert sec["Cash"]["pct"] == approx(10) and sec["Cash"]["funds"] == ["A", "B"]
    assert ex["securities"][0]["name"] == "Y Soft"
    sectors = {e["name"]: e["pct"] for e in ex["sectors"]}
    assert sectors == approx({"Financials": 40, "IT": 38, "Energy": 12, "Unclassified": 10})
    assert {e["name"]: e["pct"] for e in ex["industries"]}["Banks"] == approx(40)
    assert {e["name"]: e["pct"] for e in ex["asset_classes"]} == approx({"equity": 90, "cash": 10})

    # Shared ISINs X, Y: min(40,10) + min(30,50) = 40; cash has no ISIN so is not a shared security.
    assert pairwise_overlap(holdings) == [
        {"fund_a": "A", "fund_b": "B", "overlap_pct": approx(40), "common_securities": 2}
    ]


def test_short_derivative_nets_in_exposure_and_is_ignored_in_overlap():
    # C (arbitrage) and D, Rs 50,000 each. C holds R long and short (hedge leg) under one ISIN.
    holdings = {
        "C": [
            {"security_isin": "INE5", "security_name": "R Bank", "sector": "Financials",
             "industry": "Banks", "asset_class": "equity", "weight_pct": 60},
            {"security_isin": "INE5", "security_name": "R Bank", "sector": "Financials",
             "industry": "Banks", "asset_class": "derivative", "weight_pct": -60},
            {"security_isin": "INE6", "security_name": "M Bond", "sector": "Financials",
             "industry": "NBFC", "asset_class": "debt", "weight_pct": 30},
            {"security_isin": None, "security_name": "Cash", "sector": None,
             "industry": None, "asset_class": "cash", "weight_pct": 10},
        ],
        "D": [
            {"security_isin": "INE5", "security_name": "R Bank", "sector": "Financials",
             "industry": "Banks", "asset_class": "equity", "weight_pct": 20},
            {"security_isin": "INE7", "security_name": "S Soft", "sector": "IT",
             "industry": "Software", "asset_class": "equity", "weight_pct": 50},
            {"security_isin": "INE6", "security_name": "M Bond", "sector": "Financials",
             "industry": "NBFC", "asset_class": "debt", "weight_pct": 20},
            {"security_isin": "INE8", "security_name": "T Oil", "sector": "Energy",
             "industry": "Oil", "asset_class": "equity", "weight_pct": 10},
        ],
    }
    ex = exposure(holdings, {"C": 50_000, "D": 50_000})
    sec = _by_name(ex["securities"])
    # R: 500*60 - 500*60 + 500*20 = 10,000 -> 10%
    assert sec["R Bank"]["value_inr"] == approx(10_000) and sec["R Bank"]["pct"] == approx(10)
    assert sec["R Bank"]["funds"] == ["C", "D"]
    assert sec["M Bond"]["pct"] == approx(25)
    assert {e["name"]: e["pct"] for e in ex["asset_classes"]} == approx(
        {"equity": 70, "derivative": -30, "debt": 25, "cash": 5}
    )
    assert {e["name"]: e["pct"] for e in ex["sectors"]} == approx(
        {"Financials": 35, "IT": 25, "Energy": 5, "Unclassified": 5}
    )

    # Long weights only: min(60,20) + min(30,20) = 40 (netting R would wrongly give 20).
    assert pairwise_overlap(holdings) == [
        {"fund_a": "C", "fund_b": "D", "overlap_pct": approx(40), "common_securities": 2}
    ]


def test_empty_input():
    assert exposure({}, {}) == {"securities": [], "sectors": [], "industries": [], "asset_classes": []}
    assert pairwise_overlap({}) == []


def test_zero_total_gives_zero_pct():
    holdings = {"A": [Row("INE1", "X Bank", "Financials", "Banks", "equity", 100)]}
    ex = exposure(holdings, {"A": 0})
    assert ex["securities"] == [{"name": "X Bank", "value_inr": 0.0, "funds": ["A"], "pct": 0.0}]
