"""AMC monthly portfolio workbooks: layouts, parser rules, linking and idempotent loading.

Fixtures (tests/fixtures/portfolios) are trimmed copies of real September 2026 disclosures: Nippon India (index sheet +
one sheet per scheme, ISIN before name, weights as fractions, .xlsx bytes under an .xls name), PPFAS (name before ISIN,
foreign equity, REITs, a derivative table after the grand total) and DSP (two schemes stacked in one sheet, saved as a
real .xls). Everything else is built in memory.
"""

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import delete, func, select

from app.data import portfolios as pf
from app.models import Scheme, SchemeHolding

FX = Path(__file__).parent / "fixtures" / "portfolios"
NIPPON = (FX / "nippon_2026_09_sample.xls").read_bytes()
PPFAS = (FX / "ppfas_2026_09_sample.xlsx").read_bytes()
DSP = (FX / "dsp_2026_09_stacked_sample.xls").read_bytes()
TEST_CODES = 870000  # Scheme rows created here use amfi_codes 870000..870999
SEP = date(2026, 9, 30)

HEADER = ["Name of the Instrument", "ISIN", "Industry / Rating", "Quantity", "Market Value (Rs. in Lakhs)", "% to Net Assets"]


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def one(rows: list[list], name="Zephyr Flexi Cap Fund", header=HEADER, top=("Portfolio as on September 30, 2026",)) -> list[pf.ParsedScheme]:
    return pf.parse_workbook(xlsx({"S1": [[name], list(top), header, *rows]}))


def by_name(schemes):
    return {s.name: s for s in schemes}


# --------------------------------------------------------------------------- real layouts


def test_nippon_index_plus_sheet_per_scheme_with_fractions():
    s = by_name(pf.parse_workbook(NIPPON, "NIMF-MONTHLY-PORTFOLIO-30-Sep-26.xls"))
    assert set(s) == {"CPSE ETF", "Nippon India ETF Gold BeES"}  # the index sheet has no table
    cpse = s["CPSE ETF"]
    assert (cpse.month, cpse.quarantine, cpse.weight_sum) == (SEP, None, 100.0)
    ntpc = next(h for h in cpse.holdings if h.isin == "INE733E01010")
    assert (ntpc.name, ntpc.industry, ntpc.sector, ntpc.asset_class) == ("NTPC Limited", "Power", "Power", "equity")
    assert ntpc.weight_pct > 1  # fractions became %
    assert ntpc.market_value_inr == pytest.approx(_cell_value(NIPPON, "CC", "INE733E01010") * 1e5)  # Rs. in Lacs
    assert {h.asset_class for h in s["Nippon India ETF Gold BeES"].holdings} == {"gold", "cash"}


def _cell_value(content: bytes, sheet: str, isin: str) -> float:
    rows = dict(pf.read_sheets(content))[sheet]
    row = next(r for r in rows if isin in r)
    return row[5]


def test_ppfas_name_before_isin_with_foreign_reit_and_trailing_derivative_table():
    (s,) = pf.parse_workbook(PPFAS)
    assert (s.name, s.month, s.quarantine, s.weight_sum) == ("Parag Parikh Flexi Cap Fund", SEP, None, 100.0)
    cls = {h.asset_class for h in s.holdings}
    assert {"equity", "foreign_equity", "reit_invit", "money_market", "mf_units", "cash"} <= cls
    assert "derivative" not in cls  # the derivative detail after GRAND TOTAL is not read
    alphabet = next(h for h in s.holdings if h.isin == "US02079K3059")
    assert alphabet.asset_class == "foreign_equity" and alphabet.sector is None
    cd = next(h for h in s.holdings if h.isin == "INE476A16F78")
    assert (cd.asset_class, cd.rating, cd.industry) == ("money_market", "CRISIL A1+", None)
    assert next(h for h in s.holdings if h.name == "TRP_011026").asset_class == "cash"  # under "Reverse Repo / TREPS"


def test_dsp_two_schemes_stacked_in_one_xls_sheet():
    s = by_name(pf.parse_workbook(DSP))
    assert set(s) == {"DSP Gold ETF", "DSP Nifty Private Bank ETF"}
    assert all(x.month == SEP and x.quarantine is None and x.sheet == "Portfolios" for x in s.values())
    assert len(s["DSP Nifty Private Bank ETF"].holdings) == 12
    gold = s["DSP Gold ETF"]
    assert gold.holdings[0].asset_class == "gold" and gold.holdings[0].isin is None  # IDIA... fails the check digit
    assert gold.issues == ["invalid ISIN IDIA00190068 for GOLD"]


def test_zip_of_workbooks_is_read_member_by_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a/ppfas.xlsx", PPFAS)
        z.writestr("b/dsp.xls", DSP)
        z.writestr("readme.txt", "ignored")
    assert [n for n, _ in pf.workbooks(buf.getvalue(), "x.zip")] == ["a/ppfas.xlsx", "b/dsp.xls"]
    assert pf.workbooks(PPFAS, "p.xlsx") == [("p.xlsx", PPFAS)]  # an .xlsx is a zip too, but not a bundle


# --------------------------------------------------------------------------- parser rules


def test_isin_format_and_check_digit():
    assert pf.isin_ok("INE040A01034") and pf.isin_ok("US02079K3059") and pf.isin_ok("IN002025Z294")
    assert not pf.isin_ok("INE040A01035")  # wrong check digit
    assert not pf.isin_ok("IDIA00190068") and not pf.isin_ok("INE040A0103") and not pf.isin_ok("ine040a01034")


def test_numbers_written_as_text():
    assert pf._number("$0.00%") == 0.0 and pf._number("1,234.5") == 1234.5 and pf._number("(2.5)") == -2.5
    assert pf._number("NIL") is None and pf._number("*") is None and pf._number(None) is None and pf._number(3) == 3.0


@pytest.mark.parametrize("text", ["Monthly Portfolio Statement as on September 30,2026", "Portfolio as on 30th Sep 2026",
                                  "as on 30-09-2026", "NIMF-MONTHLY-PORTFOLIO-30-Sep-26.xls", datetime(2026, 9, 30)])
def test_disclosure_date_formats(text):
    assert pf.find_date([text]) == SEP


def test_scheme_name_drops_the_scheme_description():
    assert pf.scheme_name("Nippon India Nifty 500 Momentum \n50 Index Fund(An open-ended scheme replicating)") == \
        "Nippon India Nifty 500 Momentum 50 Index Fund"
    assert pf.scheme_name("Nippon India Quarterly Interval Fund-Series 2 (A Debt Oriented Interval Scheme)") == \
        "Nippon India Quarterly Interval Fund-Series 2"
    assert pf.scheme_name("Scheme Name: Zephyr Gilt Fund") == "Zephyr Gilt Fund"


def test_industry_spellings_normalise_and_roll_up_to_one_sector():
    for spelling in ("IT - Software", "IT-Software", "it software", "Software"):
        assert pf.normalise_industry(spelling) == ("IT - Software", "Information Technology")
    assert pf.normalise_industry("Non-Ferrous Metals") == ("Non - Ferrous Metals", "Metals & Mining")
    assert pf.normalise_industry("Pharmaceuticals") == ("Pharmaceuticals & Biotechnology", "Healthcare")
    assert pf.normalise_industry("Computer Software: Prepackaged Software ##") == ("Computer Software: Prepackaged Software", None)
    assert pf.normalise_industry("  ") == (None, None)


@pytest.mark.parametrize("heading,cls", [
    ("Equity & Equity related", "equity"), ("Arbitrage", "equity"), ("Equity & Equity related Foreign Investments", "foreign_equity"),
    ("Money Market Instruments", "money_market"), ("Treasury Bill", "money_market"), ("Reverse Repo / TREPS", "cash"),
    ("Debt Instruments", "debt"), ("Government Securities", "debt"), ("Units of Real Estate Investment Trust (REITs)", "reit_invit"),
    ("(b) Reits", "reit_invit"), ("Mutual Fund Units", "mf_units"), ("Exchange Traded Funds", "mf_units"), ("Gold", "gold"),
    ("Index Futures", "derivative"), ("Alternative Investment Funds (AIF)", "other"), ("Margin Fixed Deposit", "cash"),
    ("(a) Listed / awaiting listing on Stock Exchanges", None), ("OTHERS", None),
])
def test_section_headings_map_to_asset_classes(heading, cls):
    assert pf.section_class(heading) == cls


def test_header_synonyms_and_value_units():
    cols = pf._header_columns(["Sr. No.", "Name of Instrument", "ISIN", "Rating/Industry", "Quantity", "Market value (Rs. In crore)", "% to NAV"])
    assert cols == {"name": 1, "isin": 2, "industry": 3, "rating": 3, "quantity": 4, "value": 5, "value_unit": 10**7, "weight": 6}
    assert pf._header_columns(["ISIN", "Issuer Name", "Market/Fair Value ( Rs. in Lacs)", "% of Net Assets"])["value_unit"] == 10**5
    assert pf._header_columns(["Name of the Instrument", "Long / (Short)", "Quantity", "% to AUM"]) is None  # no ISIN: not a holdings table


def test_subtotals_are_skipped_and_the_table_stops_at_the_grand_total():
    (s,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 600, 60], ["Sub Total", None, None, None, 600, 60],
                ["Net Current Assets", None, None, None, 400, 40], ["GRAND TOTAL", None, None, None, 1000, 100],
                ["Reliance Industries Limited", "INE002A01018", "Petroleum Products", 1, 1, 5]])
    assert [h.name for h in s.holdings] == ["HDFC Bank Limited", "Net Current Assets"]
    assert s.holdings[1].asset_class == "cash" and s.weight_sum == 100


def test_fractions_summing_to_one_become_percent():
    (s,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 600, 0.6], ["TREPS", None, None, None, 400, 0.4]])
    assert [h.weight_pct for h in s.holdings] == [60, 40] and s.quarantine is None


def test_repeated_isins_are_aggregated():
    (s,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 300, 30], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 300, 30],
                ["TREPS", None, None, None, 400, 40]])
    assert len(s.holdings) == 2
    assert (s.holdings[0].weight_pct, s.holdings[0].quantity, s.holdings[0].market_value_inr) == (60, 20, 600 * 1e5)


def test_bad_weight_sum_is_quarantined_with_the_reason():
    (s,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 600, 60]])
    assert s.quarantine == "weights sum to 60.00%, not 100 +/- 3"
    assert s.holdings[0].weight_pct == 60  # kept as disclosed


def test_missing_date_is_quarantined():
    (s,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 1000, 100]], top=("Monthly portfolio",))
    assert s.quarantine == "no disclosure date found"


def test_invalid_isin_is_dropped_and_reported_and_text_weights_without_a_number_are_reported():
    (s,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01035", "Banks", 10, 1000, 100], ["Tiny Limited", "INE002A01018", "Banks", 1, 0, "*"]])
    assert s.holdings[0].isin is None and len(s.holdings) == 1
    assert s.issues == ["invalid ISIN INE040A01035 for HDFC Bank Limited", "no numeric weight for Tiny Limited (INE002A01018): '*'"]


def test_combined_industry_rating_column_is_split():
    (s,) = one([["Debt Instruments"], ["7.1% GOI 2034", "IN0020240019", "SOVEREIGN", 1, 500, 50],
                ["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 500, 50]])
    gsec, bank = s.holdings
    assert (gsec.rating, gsec.industry, gsec.asset_class) == ("SOVEREIGN", None, "debt")
    assert (bank.rating, bank.industry, bank.sector) == (None, "Banks", "Financial Services")


def test_derivatives_count_toward_100_whichever_way_the_amc_totals_them():
    hedge = ["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 700, 70], ["HDFC Bank Limited Oct26", None, "Stock Futures", -10, -700, -70]
    (beside,) = one([*hedge, ["TREPS", None, None, None, 300, 30]])  # short leg outside the net-asset total
    (inside,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 900, 90], ["NIFTY Oct26", None, "Index Futures", 1, 50, 5],
                     ["Net Receivables/Payables", None, None, None, 50, 5]])  # long future inside the total
    assert beside.quarantine is None and inside.quarantine is None
    fut = beside.holdings[1]
    assert (fut.asset_class, fut.weight_pct, fut.industry) == ("derivative", -70, None)  # short stays negative


def test_unstated_value_unit_is_reported_not_guessed():
    (s,) = one([["Equity"], ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 1000, 100]],
               header=["Name of the Instrument", "ISIN", "Industry", "Quantity", "Market Value", "% to NAV"])
    assert s.holdings[0].market_value_inr is None and s.issues == ["market value unit not stated; values not stored"]


def test_note_tables_after_a_scheme_are_not_schemes():
    rows = [["Zephyr Flexi Cap Fund"], ["Portfolio as on September 30, 2026"], HEADER, ["HDFC Bank Limited", "INE040A01034", "Banks", 10, 1000, 100],
            ["GRAND TOTAL"], ["In case of the below securities the fund has ignored prices provided by valuation agencies under the "
                              "circular dated March 22, 2019 for detailed rationale see the website of the fund"], HEADER,
            ["Some Bond", "INE040A01034", "CRISIL D", 1, 0, 0], ["GRAND TOTAL"],
            ["(3) Plan/Option wise per unit Net Asset Values (NAV) are as follows"], HEADER, ["Some Bond", "INE040A01034", "CRISIL D", 1, 0, 0],
            ["GRAND TOTAL"], ["Plan/Option"], HEADER, ["Some Bond", "INE040A01034", "CRISIL D", 1, 0, 0]]
    assert [s.name for s in pf.parse_workbook(xlsx({"S": rows}))] == ["Zephyr Flexi Cap Fund"]


# --------------------------------------------------------------------------- linking and loading


@pytest.fixture
def schemes(db):
    def wipe():
        db.rollback()
        db.execute(delete(SchemeHolding).where(SchemeHolding.amfi_code.between(TEST_CODES, TEST_CODES + 999)))
        db.execute(delete(Scheme).where(Scheme.amfi_code.between(TEST_CODES, TEST_CODES + 999)))
        db.commit()

    wipe()
    for code, plan, option in ((1, "regular", "idcw"), (2, "direct", "growth"), (3, "regular", "growth")):
        db.add(Scheme(amfi_code=TEST_CODES + code, name="Parag Parikh Flexi Cap Fund", amc_name="PPFAS Mutual Fund", raw_category="Test",
                      category_key="flexi_cap", plan_type=plan, option_type=option, riskometer="very_high"))
    db.add(Scheme(amfi_code=TEST_CODES + 4, name="DSP Gold ETF", amc_name="DSP Mutual Fund", raw_category="Test", category_key="gold",
                  plan_type="direct", option_type="other", riskometer="high"))
    db.commit()
    yield
    wipe()


def test_load_links_to_regular_growth_and_reports_unmatched_names(db, schemes):
    out = pf.load_portfolios(db, PPFAS, "https://amc.ppfas.com/x.xlsx")
    assert (out["schemes_parsed"], out["linked"], out["quarantined"], out["unlinked"], out["months"]) == (1, 1, 0, 0, ["2026-09-30"])
    stored = db.scalars(select(SchemeHolding).where(SchemeHolding.amfi_code == TEST_CODES + 3)).all()
    assert len(stored) == out["rows"] == 102
    assert {h.source_url for h in stored} == {"https://amc.ppfas.com/x.xlsx"} and {h.disclosure_month for h in stored} == {SEP}

    dsp = pf.load_portfolios(db, DSP, "dsp.xls")
    assert dsp["linked"] == 0 and {u["reason"] for u in dsp["unlinked_schemes"]} == {
        "matched, but the scheme has no Regular-Growth plan", "no AMFI scheme matches the name"}


def test_reloading_replaces_the_month_instead_of_duplicating(db, schemes):
    pf.load_portfolios(db, PPFAS, "a")
    pf.load_portfolios(db, PPFAS, "b")
    q = select(func.count()).select_from(SchemeHolding).where(SchemeHolding.amfi_code == TEST_CODES + 3)
    assert db.scalar(q) == 102
    assert set(db.scalars(select(SchemeHolding.source_url).where(SchemeHolding.amfi_code == TEST_CODES + 3))) == {"b"}
