from datetime import date

import pytest

from app.data.amfi import iter_rows
from app.reference.categories import normalise_category, normalise_option, normalise_plan


@pytest.mark.parametrize(
    "raw,name,expected",
    [
        ("Open Ended Schemes(Equity Scheme - Large & Mid Cap Fund)", "X", "large_mid_cap"),
        ("Open Ended Schemes(Equity Schemes - Large Cap Fund)", "X", "large_cap"),
        ("Open Ended Schemes(Equity Schemes - ELSS- Tax Saver Fund)", "X", "elss"),
        ("Open Ended Schemes(Income/Debt Oriented Schemes - Ultra Short to Short Term Fund)", "X", "low_duration"),
        ("Open Ended Schemes(Income/Debt Oriented Schemes - Medium to Long Term Fund)", "X", "medium_long_duration"),
        ("Open Ended Schemes(Income/Debt Oriented Schemes - 10-year Constant Maturity Gilt Fund)", "X", "gilt_10y"),
        ("Open Ended Schemes(Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage)", "X", "dynamic_asset_allocation"),
        ("Open Ended Schemes(Other Scheme - Index Funds)", "UTI Nifty 50 Index Fund", "index_equity"),
        ("Open Ended Schemes(Other Scheme - Index Funds)", "Bharat Bond Index Fund April 2030", "index_debt"),
        ("Open Ended Schemes(Other Scheme - FoF Domestic)", "HDFC Gold ETF Fund of Fund", "gold"),
        ("Open Ended Schemes(Other Scheme - FoF Overseas)", "X", "intl_fof"),
        ("Open Ended Schemes(Exchange Traded Funds (ETFs) - Silver ETF)", "X", "silver"),
        ("Open Ended Schemes(Solution Oriented Scheme - Retirement Fund)", "X", "retirement"),
    ],
)
def test_normalise_category(raw, name, expected):
    assert normalise_category(raw, name) == expected


def test_plan_and_option():
    assert normalise_plan("Regular Plan", "") == "regular"
    assert normalise_plan("", "Axis Fund - Direct Plan - Growth") == "direct"
    assert normalise_option("GROWTH Option", "") == "growth"
    assert normalise_option("ANNUAL IDCW Option", "") == "idcw"
    assert normalise_option("Cumulative", "") == "growth"


def test_amfi_parser_handles_sections_and_columns():
    text = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

PPFAS Mutual Fund

122640;INF879O01027;-;Parag Parikh Flexi Cap Fund;Regular Plan;Growth;81.3640;14-Oct-2026
"""
    rows = list(iter_rows(text.splitlines()))
    assert len(rows) == 1
    r = rows[0]
    assert (r.amfi_code, r.amc_name, r.isin_growth, r.nav, r.nav_date) == (122640, "PPFAS Mutual Fund", "INF879O01027", 81.364, date(2026, 10, 14))
    assert normalise_category(r.raw_category, r.name) == "flexi_cap"
