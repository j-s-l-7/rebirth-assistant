"""X-ray maths: hand-computed exposure, overlap, concentration and coverage cases."""

from dataclasses import dataclass
from datetime import date

import pytest
from pytest import approx

from app.analytics.xray import (
    BANNED_WORDS, concentration, coverage, exposure, objective_checks, observations, pairwise_overlap,
)


@dataclass
class Row:
    security_isin: str | None
    security_name: str
    sector: str | None
    industry: str | None
    asset_class: str
    weight_pct: float | None


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
    assert concentration(exposure({}, {}), []) == {
        "top10_pct": 0, "effective_stocks": 0, "hhi": 0, "top_sector_pct": 0, "redundancy_pct": 0
    }
    assert coverage({}, {}, {}, date(2026, 10, 22)) == {
        "covered_value_pct": 0, "funds_without_disclosure": [], "stale_disclosures": []
    }


def test_zero_total_gives_zero_pct():
    holdings = {"A": [Row("INE1", "X Bank", "Financials", "Banks", "equity", 100)]}
    ex = exposure(holdings, {"A": 0})
    assert ex["securities"] == [{"name": "X Bank", "value_inr": 0.0, "funds": ["A"], "isin": "INE1", "pct": 0.0}]


def test_none_weight_row_is_skipped():
    holdings = {
        "A": [Row("INE1", "X Bank", "Financials", "Banks", "equity", 100),
              Row("INE9", "N Co", "IT", "Software", "equity", None)],
        "B": [Row("INE1", "X Bank", "Financials", "Banks", "equity", 50),
              Row("INE9", "N Co", "IT", "Software", "equity", None)],
    }
    ex = exposure(holdings, {"A": 100, "B": 100})
    assert [e["name"] for e in ex["securities"]] == ["X Bank"]
    assert pairwise_overlap(holdings)[0]["common_securities"] == 1


def test_concentration_four_equal_securities():
    # One fund: four stocks at 22.5% each plus 10% cash. Cash (no ISIN) is not a security.
    holdings = {"A": [
        Row("INE1", "S1", "Financials", "Banks", "equity", 22.5),
        Row("INE2", "S2", "Financials", "Banks", "equity", 22.5),
        Row("INE3", "S3", "IT", "Software", "equity", 22.5),
        Row("INE4", "S4", "Energy", "Oil", "equity", 22.5),
        Row(None, "Cash", None, None, "cash", 10),
    ]}
    overlap = [{"overlap_pct": 40}, {"overlap_pct": 20}]
    # shares 0.25 each -> hhi 4 * 0.0625 = 0.25, effective 4; top10 = 90; Financials 45; mean overlap 30.
    assert concentration(exposure(holdings, {"A": 1000}), overlap) == {
        "top10_pct": approx(90), "effective_stocks": approx(4), "hhi": approx(0.25),
        "top_sector_pct": approx(45), "redundancy_pct": approx(30),
    }


def test_concentration_unequal_and_top10_cap():
    secs = [{"name": f"S{i}", "isin": f"INE{i}", "pct": p} for i, p in enumerate([50, 30, 20])]
    c = concentration({"securities": secs, "sectors": []}, [])
    assert c["hhi"] == approx(0.38) and c["effective_stocks"] == approx(1 / 0.38)  # .25 + .09 + .04
    secs = [{"name": f"S{i}", "isin": f"INE{i}", "pct": 8} for i in range(12)]
    assert concentration({"securities": secs, "sectors": []}, [])["top10_pct"] == approx(80)


def test_coverage_missing_and_stale():
    # Today Oct 2026: Aug is fresh (2 months back), Jul is stale. C has no holdings.
    rows = [Row("INE1", "X Bank", "Financials", "Banks", "equity", 100)]
    holdings = {"A": rows, "B": rows}
    months = {"A": "2026-08", "B": "2026-07"}
    assert coverage({"A": 50_000, "B": 30_000, "C": 20_000}, holdings, months, date(2026, 10, 22)) == {
        "covered_value_pct": approx(80),
        "funds_without_disclosure": ["C"],
        "stale_disclosures": [{"fund": "B", "disclosure_month": "2026-07"}],
    }


def _two_fund_exposure():
    # Same book as test_two_long_only_funds: X 28, Y 38, Z 12, W 12, Cash 10; Financials 40, IT 38, Energy 12.
    holdings = {
        "A": [Row("INE1", "X Bank", "Financials", "Banks", "equity", 40),
              Row("INE2", "Y Soft", "IT", "Software", "equity", 30),
              Row("INE3", "Z Bank", "Financials", "Banks", "equity", 20),
              Row(None, "Cash", None, None, "cash", 10)],
        "B": [Row("INE1", "X Bank", "Financials", "Banks", "equity", 10),
              Row("INE2", "Y Soft", "IT", "Software", "equity", 50),
              Row("INE4", "W Oil", "Energy", "Oil", "equity", 30),
              Row(None, "Cash", None, None, "cash", 10)],
    }
    return holdings, exposure(holdings, {"A": 60_000, "B": 40_000})


def test_objective_within():
    _, ex = _two_fund_exposure()
    assert objective_checks(ex, {"max_security_pct": 40, "max_sector_pct": 45}) == [
        {"check": "max_security_pct", "status": "within", "item": "Y Soft", "actual_pct": approx(38),
         "limit_pct": 40, "contributors": ["A", "B"]},
        {"check": "max_sector_pct", "status": "within", "item": "Financials", "actual_pct": approx(40),
         "limit_pct": 45, "contributors": ["A", "B"]},
    ]


def test_objective_exceeds_and_amc_skipped_without_amcs():
    _, ex = _two_fund_exposure()
    checks = objective_checks(ex, {"max_industry_pct": 30, "max_amc_pct": 10})
    assert checks == [{"check": "max_industry_pct", "status": "exceeds", "item": "Banks", "actual_pct": approx(40),
                       "limit_pct": 30, "contributors": ["A", "B"]}]


def test_objective_avoid_present():
    _, ex = _two_fund_exposure()
    assert objective_checks(ex, {"avoid": ["w oil", "Banks", "Nope"]}) == [
        {"check": "avoid", "status": "present", "item": "W Oil", "actual_pct": approx(12), "contributors": ["B"]},
        {"check": "avoid", "status": "present", "item": "Banks", "actual_pct": approx(40),
         "contributors": ["A", "B"]},
    ]


@pytest.mark.parametrize("objective", [None, {}, {"avoid": [], "max_security_pct": None}])
def test_objective_empty(objective):
    assert objective_checks({}, objective) == [{"check": "no_limits_set", "status": "info"}]


def test_observations_threshold_and_numbers():
    holdings, ex = _two_fund_exposure()
    ov = pairwise_overlap(holdings)  # A/B overlap 40
    cov = coverage({"A": 60_000, "B": 40_000}, holdings, {"A": "2026-09", "B": "2026-09"}, date(2026, 10, 1))
    assert observations(ex, ov, concentration(ex, ov), cov) == []
    [obs] = observations(ex, ov, concentration(ex, ov), cov, overlap_threshold_pct=40)
    assert obs["code"] == "high_overlap" and obs["funds"] == ["A", "B"]
    assert "40.0%" in obs["text"] and "2 common securities" in obs["text"] and "EUIN" in obs["text"]


def _all_texts():
    """Every observation and objective-check output the code can produce, keyed by code / status."""
    holdings, ex = _two_fund_exposure()
    ov = pairwise_overlap(holdings)
    cov = coverage({"A": 60_000, "B": 40_000, "C": 10_000}, holdings, {"A": "2026-09", "B": "2026-05"},
                   date(2026, 10, 1))
    out = {o["code"]: o["text"] for o in observations(ex, ov, concentration(ex, ov), cov, overlap_threshold_pct=0)}
    checks = objective_checks(ex, {"max_security_pct": 50, "max_sector_pct": 10, "avoid": ["Y Soft"]})
    checks += objective_checks(ex, None)
    out.update({c["status"]: str(c) for c in checks})
    return out


@pytest.mark.parametrize("code", ["high_overlap", "low_coverage", "stale_disclosure",
                                  "within", "exceeds", "present", "info"])
def test_wording_guard(code):
    text = _all_texts()[code].lower()
    assert [w for w in BANNED_WORDS if w in text] == []
