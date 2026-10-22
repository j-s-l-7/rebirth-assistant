"""Fund lens: any fund vs its category (offline, synthetic market)."""

from app.analytics.fund_lens import compare_funds, fund_lens, resolve_funds
from app.copilot.tools import ToolContext
from app.data.bulk import copy_nav_points
from sqlalchemy import delete

from app.models import Investor, Scheme, SchemeHolding
from tests.synthetic import walk


def test_fund_is_ranked_against_its_category(db, market):
    lens = fund_lens(db, market["flexi_cap"][0])
    assert lens["peer_set"]["funds"] == len(market["flexi_cap"])
    assert 1 <= lens["rank"]["position"] <= lens["rank"]["of"]
    rows = {r["metric"]: r for r in lens["metrics_vs_category"]}
    assert rows["cagr_3y"]["category_median"] is not None and rows["cagr_3y"]["standing"]
    assert rows["r_squared_3y"]["fund"] is not None  # NAV-based diversification read
    assert set(lens["d_profile"]) == {"performance", "consistency", "risk", "diversification", "asset_mix", "cost"}
    assert lens["d_profile"]["diversification"]["category_rule"].startswith("At least 65% in equity")
    assert lens["top_peers"] and all(p["amfi_code"] != lens["fund"]["amfi_code"] for p in lens["top_peers"])


def test_direct_plan_is_assessed_through_its_regular_sibling(db, market):
    regular = db.get(Scheme, market["mid_cap"][0])
    direct = Scheme(amfi_code=990101, name=regular.name.replace("Regular", "Direct"), amc_name=regular.amc_name, raw_category=regular.raw_category,
                    category_key=regular.category_key, plan_type="direct", option_type="growth", riskometer=regular.riskometer, in_universe=False)
    db.add(direct)
    db.commit()
    copy_nav_points(db, walk(990101, 0.18, 0.21))
    db.commit()
    lens = fund_lens(db, 990101)
    assert lens["fund"]["amfi_code"] == regular.amfi_code
    assert lens["asked_about"]["plan"] == "direct" and "Regular-Growth" in lens["asked_about"]["note"]


def test_compare_and_resolve_by_name(db, market):
    out = compare_funds(db, [market["small_cap"][0], market["small_cap"][1]])
    assert out["same_category"] is True and len(out["funds"]) == 2
    mixed = compare_funds(db, [market["small_cap"][0], market["large_cap"][0]])
    assert mixed["same_category"] is False
    assert resolve_funds(db, "Test Small Cap Fund 2")[0]["amfi_code"] == market["small_cap"][1]


def test_copilot_tool_accepts_a_fund_name(db, market):
    inv = Investor(display_name="Guest")
    db.add(inv)
    db.commit()
    out, ok = ToolContext(db, inv).run("fund_lens", {"fund": "Test Large Cap Fund 3"})
    assert ok and out["fund"]["amfi_code"] == market["large_cap"][2]
    out, ok = ToolContext(db, inv).run("fund_lens", {"fund": "No Such Fund Anywhere"})
    assert not ok and "No mutual fund matches" in out["error"]


def _add_scheme(db, like_code, code, name):
    s = db.get(Scheme, like_code)
    db.add(Scheme(amfi_code=code, name=name, amc_name=s.amc_name, raw_category=s.raw_category, category_key=s.category_key,
                  plan_type="regular", option_type="growth", riskometer=s.riskometer, in_universe=False))
    db.commit()


def test_single_weak_match_is_ambiguous_but_exact_names_resolve(db, market):
    from app.portfolio_service import resolve_one
    code = market["large_cap"][0]
    _add_scheme(db, code, 990201, "Test US Bluechip Equity Fund - Regular Plan - Growth")
    s, err = resolve_one(db, "Test Bluechip Fund")
    assert s is None and err["code"] == "ambiguous" and "Test US Bluechip Equity Fund - Regular Plan - Growth" in err["candidates"]
    out, ok = ToolContext(db, Investor(display_name="G")).run("fund_lens", {"fund": "Test Bluechip Fund"})
    assert not ok and "no clear match" in out["error"]
    name = db.get(Scheme, code).name
    for q in (name, name.replace(" - Regular Plan - Growth", ""), name.replace(" Fund", ""), str(code)):
        assert resolve_one(db, q)[0].amfi_code == code
    assert resolve_one(db, "Test US Bluechip Equity Fund")[0].amfi_code == 990201


def _hold(db, code, month, lines):
    db.add_all(SchemeHolding(amfi_code=code, disclosure_month=month, security_isin=i, security_name=n, sector=sec, asset_class="equity", weight_pct=w)
               for i, n, sec, w in lines)
    db.commit()


def test_lens_top_holdings_sectors_and_compare_overlap(db, market):
    from datetime import date
    a, b, c = market["mid_cap"][:3]
    m = date(2026, 9, 30)
    _hold(db, a, m, [("INE000000001", "Alpha", "Banks", 30), ("INE000000002", "Beta", "IT", 20), (None, "TREPS", None, 5), ("INE000000003", "Gamma", "Banks", 10)])
    _hold(db, b, m, [("INE000000001", "Alpha", "Banks", 10), ("INE000000004", "Delta", "Auto", 40)])
    h = fund_lens(db, a)["d_profile"]["diversification"]["stock_level"]
    assert [x["name"] for x in h["top_holdings"]] == ["Alpha", "Beta", "Gamma"]
    assert h["top_sectors"][0] == {"name": "Banks", "weight_pct": 40.0} and all(s["name"] != "Unclassified" for s in h["top_sectors"])
    out = compare_funds(db, [a, b])
    pair = out["holdings_overlap"]["pairs"][0]
    assert pair["overlap_pct"] == 10.0 and pair["common_securities"] == 1 and out["holdings_overlap"]["disclosure_months"][out["funds"][0]["fund"]] == "2026-09"
    out = compare_funds(db, [a, b, c])
    assert "holdings_overlap" not in out and out["holdings_overlap_missing"] == [db.get(Scheme, c).name]
    db.execute(delete(SchemeHolding).where(SchemeHolding.amfi_code.in_([a, b])))
    db.commit()
