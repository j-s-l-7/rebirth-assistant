"""Fund lens: any fund vs its category (offline, synthetic market)."""

from app.analytics.fund_lens import compare_funds, fund_lens, resolve_funds
from app.copilot.tools import ToolContext
from app.data.bulk import copy_nav_points
from app.models import Investor, Scheme
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
