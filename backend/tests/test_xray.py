"""X-ray maths: hand-computed exposure, overlap, concentration and coverage cases."""

from dataclasses import dataclass
from datetime import date

import pytest
from pytest import approx

from app.analytics.xray import (
    BANNED_WORDS, SCREEN_NOTE, concentration, coverage, exposure, low_overlap_screen, objective_checks, observations,
    pairwise_overlap, simulate_changes,
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
    months = {"A": "2026-09", "B": "2026-08"}
    cov = coverage({"A": 60_000, "B": 40_000}, holdings, months, date(2026, 10, 1))
    assert observations(ov, cov, months) == []
    [obs] = observations(ov, cov, months, overlap_threshold_pct=40)
    assert obs["code"] == "high_overlap" and obs["funds"] == ["A", "B"]
    assert "40.0%" in obs["text"] and "2 common securities" in obs["text"] and "EUIN" in obs["text"]
    assert "A (holdings as of 2026-09) and B (holdings as of 2026-08)" in obs["text"]


def _all_texts():
    """Every observation and objective-check output the code can produce, keyed by code / status."""
    holdings, ex = _two_fund_exposure()
    ov = pairwise_overlap(holdings)
    months = {"A": "2026-09", "B": "2026-05"}
    cov = coverage({"A": 60_000, "B": 40_000, "C": 10_000}, holdings, months, date(2026, 10, 1))
    out = {o["code"]: o["text"] for o in observations(ov, cov, months, overlap_threshold_pct=0)}
    checks = objective_checks(ex, {"max_security_pct": 50, "max_sector_pct": 10, "avoid": ["Y Soft"]})
    checks += objective_checks(ex, None)
    out.update({c["status"]: str(c) for c in checks})
    bad = [{"from_fund": "A", "to_fund": "B", "amount_inr": 0}, {"from_fund": "A", "to_fund": "A", "amount_inr": 1},
           {"from_fund": "Q", "to_fund": "B", "amount_inr": 1}, {"from_fund": "A", "to_fund": "Q", "amount_inr": 1},
           {"from_fund": "A", "to_fund": "B", "amount_inr": 10**9}]
    sim = simulate_changes(holdings, {"A": 60_000, "B": 40_000}, bad)
    out["simulate_reasons"] = " ".join(c["reason"] for c in sim["changes_applied"])
    screen = low_overlap_screen(holdings, {"A": 60_000, "B": 40_000}, {})
    out["screen"] = screen["criterion"] + " " + screen["note"]
    return out


@pytest.mark.parametrize("code", ["high_overlap", "low_coverage", "stale_disclosure",
                                  "within", "exceeds", "present", "info", "simulate_reasons", "screen"])
def test_wording_guard(code):
    text = _all_texts()[code].lower().replace("not a recommendation", "")  # the screen note's own disclaimer
    assert [w for w in BANNED_WORDS if w in text] == []


def test_simulate_moving_half_of_a_into_b():
    # A 60k -> 30k, B 40k -> 70k. X: 300*40 + 700*10 = 19,000; Y: 9,000 + 35,000 = 44,000; Z: 6,000; W: 21,000.
    holdings, _ = _two_fund_exposure()
    move = {"from_fund": "A", "to_fund": "B", "amount_inr": 30_000}
    out = simulate_changes(holdings, {"A": 60_000, "B": 40_000}, [move])
    assert out["changes_applied"] == [{**move, "applied": True, "reason": None}]
    after = out["after"]
    assert {e["name"]: e["pct"] for e in after["exposures"]["securities"]} == approx(
        {"X Bank": 19, "Y Soft": 44, "Z Bank": 6, "W Oil": 21, "Cash": 10})
    assert {e["name"]: e["pct"] for e in after["exposures"]["sectors"]} == approx(
        {"Financials": 25, "IT": 44, "Energy": 21, "Unclassified": 10})
    assert after["overlap"] == out["before"]["overlap"]  # overlap is between funds' weights, not rupees
    assert after["concentration"]["hhi"] == approx(2774 / 8100)  # (44^2 + 21^2 + 19^2 + 6^2) / 90^2
    deltas = {d["name"]: (d["before_pct"], d["after_pct"], d["delta_pct"]) for d in out["deltas"]["securities"]}
    assert deltas == {"X Bank": approx((28, 19, -9)), "W Oil": approx((12, 21, 9)),
                      "Y Soft": approx((38, 44, 6)), "Z Bank": approx((12, 6, -6))}  # Cash unchanged, left out
    assert [d["name"] for d in out["deltas"]["sectors"]] == ["Financials", "Energy", "IT"]  # -15, +9, +6


def test_simulate_amount_above_holding_not_applied():
    holdings, _ = _two_fund_exposure()
    move = {"from_fund": "A", "to_fund": "B", "amount_inr": 70_000}
    out = simulate_changes(holdings, {"A": 60_000, "B": 40_000}, [move])
    [c] = out["changes_applied"]
    assert c["applied"] is False and "60,000" in c["reason"]
    assert out["after"] == out["before"] and out["deltas"] == {"securities": [], "sectors": [], "industries": []}


def test_low_overlap_screen_order_and_effect():
    # Portfolio long pcts: X 28, Y 38, Z 12, W 12 (cash excluded).
    holdings, _ = _two_fund_exposure()
    cands = {
        "C1": [Row("INE1", "X Bank", "Financials", "Banks", "equity", 50),
               Row("INE9", "V Pharma", "Health", "Pharma", "equity", 50)],  # min(50,28) = 28
        "C2": [Row("INE4", "W Oil", "Energy", "Oil", "equity", 10),
               Row("INE9", "V Pharma", "Health", "Pharma", "equity", 90)],  # min(10,12) = 10
        "C3": [Row("INE2", "Y Soft", "IT", "Software", "equity", 60),
               Row("INE3", "Z Bank", "Financials", "Banks", "equity", 40)],  # 38 + 12 = 50
        "C4": [],  # no disclosure: left out
    }
    out = low_overlap_screen(holdings, {"A": 60_000, "B": 40_000}, cands, amount_inr=100_000, limit=2)
    assert out["criterion"] == "lowest overlap with your current look-through holdings"
    assert out["note"] == SCREEN_NOTE == (
        "Screen on one criterion (overlap), not a recommendation. It ignores returns, risk, cost and your goals.")
    assert [(c["name"], c["overlap_pct"]) for c in out["candidates"]] == [("C2", approx(10)), ("C1", approx(28))]
    # +100k into C2 (total 200k): V 90,000 = 45%; Y 19, X 14, W (12,000 + 10,000) 11, Z 6.
    assert out["candidates"][0]["effect"] == {"top_security_pct_after": approx(45),
                                              "effective_stocks_after": approx(9025 / 2739)}
    no_amount = low_overlap_screen(holdings, {"A": 60_000, "B": 40_000}, cands)
    assert [c["name"] for c in no_amount["candidates"]] == ["C2", "C1", "C3"]
    assert "effect" not in no_amount["candidates"][0] and no_amount["amount_inr"] is None


def test_objective_avoid_matches_leading_whole_words_only():
    _, ex = _two_fund_exposure()
    assert [c["item"] for c in objective_checks(ex, {"avoid": ["W"]})] == ["W Oil"]
    assert objective_checks(ex, {"avoid": ["Ban", "Oi"]}) == []
