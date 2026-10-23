"""AMFI TER: parsing (JSON + xlsx, both layouts), change-point compression, storage, linking, reads.

Fixtures are trimmed copies of real AMFI payloads (tests/fixtures/ter); everything else is synthetic.
"""

import contextlib
import io
import itertools
import json
import zipfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, func, select

from app import http
from app.data import ter
from app.models import Scheme, SchemeDetail, SchemeLink, SchemeTer

FX = Path(__file__).parent / "fixtures" / "ter"
SEP_XLSX = (FX / "ter_2026_09_sample.xlsx").read_bytes()
NOV_XLSX = (FX / "ter_2023_11_sample.xlsx").read_bytes()
TEST_CODES = 880000  # Scheme rows created here use amfi_codes in TEST_CODES..TEST_CODES+999 (the synthetic market starts at 900001)
TEST_RANGE = (TEST_CODES, TEST_CODES + 999)


def _json(name: str) -> dict:
    return json.loads((FX / name).read_text())


def api_row(code, day, name="Alpha Equity Fund", r=2.0, d=1.0, cat="Equity Schemes - Flexi Cap Fund", **over) -> dict:
    """A row shaped like AMFI's JSON (new layout); components add up to the totals."""
    row = {
        "NSDLSchemeCode": code, "Scheme_Name": name, "SchemeType_Desc": "Open Ended", "SchemeCat_Desc": cat,
        "TER_Year": "2026-2027", "TER_Date": f"{day.isoformat()}T00:00:00.000Z",
        "R_BER": f"{r - 0.4:.4f}", "R_BrokerageCost": "0.0300", "R_TransactionCost": "0.0000", "R_StatutoryLevies": "0.3700", "R_TER": f"{r:.4f}",
        "D_BER": f"{d - 0.4:.4f}", "D_BrokerageCost": "0.0300", "D_TransactionCost": "0.0000", "D_StatutoryLevies": "0.3700", "D_TER": f"{d:.4f}",
        "MF_ID": 7, "Month": f"{day.month:02d}-{day.year}",
    }
    return {**row, **over}


def days(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def dump(db) -> list[tuple]:
    cols = [SchemeTer.nsdl_code, SchemeTer.effective_from, *(getattr(SchemeTer, c) for c in ter.VALUE_COLS)]
    return sorted(db.execute(select(*cols)).all(), key=lambda r: (r[0], r[1]))


def count(db, model=SchemeTer) -> int:
    return db.scalar(select(func.count()).select_from(model))


def add_scheme(db, code, name, plan="regular", option="growth", category="ter_test_cat", in_universe=True):
    db.add(Scheme(amfi_code=code, name=name, amc_name="Zephyr Mutual Fund", raw_category="Test", category_key=category, plan_type=plan,
                  option_type=option, riskometer="very_high", in_universe=in_universe))


@pytest.fixture(autouse=True)
def clean(db):
    def wipe():
        db.rollback()
        db.execute(delete(SchemeTer))
        db.execute(delete(SchemeLink).where((SchemeLink.source == "ter") | SchemeLink.amfi_code.between(*TEST_RANGE)))
        db.execute(delete(SchemeDetail))
        db.execute(delete(Scheme).where(Scheme.amfi_code.between(*TEST_RANGE)))
        db.commit()

    wipe()
    yield
    wipe()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(http, "_sleep", lambda seconds: None)


# --------------------------------------------------------------------------- parsing


def test_parse_json_new_layout():
    recs = ter.parse_ter_rows(_json("ter_api_2026_09_page.json")["data"])
    assert len(recs) == 6
    r = recs[0]
    assert r["nsdl_code"] == "360O/O/H/BHF/23/07/0007" and r["mf_id"] == 62 and r["effective_from"] == date(2026, 9, 1)
    assert (r["scheme_name"], r["scheme_type"], r["scheme_category"]) == ("360 ONE Balanced Hybrid Fund", "Open Ended", "Hybrid Schemes - Balanced Hybrid Fund")
    assert (r["r_ber"], r["r_brokerage"], r["r_transaction"], r["r_statutory"], r["r_ter"]) == (1.78, 0.03, 0.0, 0.38, 2.19)
    assert (r["d_ber"], r["d_brokerage"], r["d_transaction"], r["d_statutory"], r["d_ter"]) == (0.45, 0.03, 0.0, 0.14, 0.62)
    assert r["r_6a_b"] is None and r["r_6a_c"] is None
    assert [x["effective_from"] for x in recs] == [date(2026, 9, d) for d in range(1, 7)]


def test_parse_json_old_layout_maps_what_has_a_home():
    page = _json("ter_api_2023_11_page.json")["data"]
    r, raw = ter.parse_ter_rows(page)[0], page[0]
    assert raw["NSDLSchemeCode"] is None and r["nsdl_code"] == ter.synthetic_code("360 ONE Balanced Hybrid Fund")
    assert r["r_ber"] == float(raw["R_BaseTER"]) == 1.95 and r["r_statutory"] == float(raw["R_GST"]) == 0.06 and r["r_ter"] == 2.06
    assert r["d_ber"] == 0.48 and r["d_ter"] == 0.59
    assert r["r_brokerage"] is None and r["r_transaction"] is None  # not disclosed in that layout
    assert (r["r_6a_b"], r["r_6a_c"], r["d_6a_c"]) == (0.0, 0.05, 0.05)  # parsed even though scheme_ter has no column for them
    # ...so they are stored and compared only if the model has those columns (it does not today)
    assert ("r_6a_b" in ter.VALUE_COLS) == ("r_6a_b" in SchemeTer.__table__.columns)


def test_parse_cleans_values_and_drops_unusable_rows():
    good = api_row("X/1", date(2026, 9, 1), name="  Spacey   Fund  ")
    rows = [
        good,
        api_row("X/2", date(2026, 9, 1), name=""),
        api_row("X/3", date(2026, 9, 1), TER_Date="not a date"),
        api_row("X/4", date(2026, 9, 1), R_BER="", R_TER=None, D_BER="NA", D_TER="NaN", MF_ID=None),
        api_row("X/5", date(2026, 9, 1), name="N" * 400),
    ]
    recs = {r["nsdl_code"]: r for r in ter.parse_ter_rows(rows)}
    assert set(recs) == {"X/1", "X/4", "X/5"}
    assert recs["X/1"]["scheme_name"] == "Spacey   Fund"
    assert (recs["X/4"]["r_ber"], recs["X/4"]["r_ter"], recs["X/4"]["d_ber"], recs["X/4"]["d_ter"], recs["X/4"]["mf_id"]) == (None, None, None, None, None)
    assert len(recs["X/5"]["scheme_name"]) == 255


def test_date_formats():
    for value in ("2026-09-01T00:00:00.000Z", "2026-09-01", date(2026, 9, 1), 46266.0):
        assert ter.parse_ter_rows([api_row("X/1", date(2026, 9, 1), TER_Date=value)])[0]["effective_from"] == date(2026, 9, 1)


def test_missing_nsdl_codes_are_resolved_by_name_else_get_a_stable_key():
    coded = [api_row("A/1", date(2026, 9, 1), name="Alpha Fund"), api_row("A/2", date(2026, 9, 1), name="Twin Fund"), api_row("A/3", date(2026, 9, 1), name="TWIN  fund")]
    cmap = ter.build_code_map(coded)
    assert cmap == {"alpha fund": "A/1"}  # a name shared by two codes is ambiguous and left out
    old = [api_row(None, date(2023, 11, 1), name="Alpha  Fund"), api_row("", date(2023, 11, 1), name="Gone Fund")]
    recs = ter.parse_ter_rows(old, cmap)
    assert recs[0]["nsdl_code"] == "A/1"
    assert recs[1]["nsdl_code"] == ter.synthetic_code("gone FUND") and recs[1]["nsdl_code"].startswith("NA/") and len(recs[1]["nsdl_code"]) <= 40


# --------------------------------------------------------------------------- xlsx


def _without_mf(recs):
    return [{k: v for k, v in r.items() if k != "mf_id"} for r in recs]


def test_xlsx_new_layout_matches_the_json_api():
    rows = ter.read_xlsx_rows(SEP_XLSX)
    assert len(rows) == 163 and all(r["TER_Date"].startswith("2026-09-") for r in rows)  # the 12 disclaimer rows are gone
    assert "MF_ID" not in rows[0] and "NSDLSchemeCode" in rows[0]
    first = [r for r in rows if r["Scheme_Name"] == "360 ONE Balanced Hybrid Fund"][:6]
    from_xlsx = ter.parse_ter_rows(first)
    from_json = ter.parse_ter_rows(_json("ter_api_2026_09_page.json")["data"])
    assert _without_mf(from_xlsx) == _without_mf(from_json)  # same scheme-days, same values, to the last decimal
    assert {r["mf_id"] for r in from_xlsx} == {None} and {r["mf_id"] for r in from_json} == {62}


def test_xlsx_old_layout_matches_the_json_api():
    rows = ter.read_xlsx_rows(NOV_XLSX)
    assert len(rows) == 90 and "R_BaseTER" in rows[0] and "R_6A_B" in rows[0] and "R_BER" not in rows[0]
    first = [r for r in rows if r["Scheme_Name"] == "360 ONE Balanced Hybrid Fund"][:4]
    assert _without_mf(ter.parse_ter_rows(first)) == _without_mf(ter.parse_ter_rows(_json("ter_api_2023_11_page.json")["data"]))


def _mini_xlsx(sheet_rows: list[list], shared: bool = False) -> bytes:
    """A tiny workbook: strings inline (or via sharedStrings) and numbers as numbers."""
    strings: list[str] = []
    xml_rows = []
    for r, row in enumerate(sheet_rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{'ABCDEFGHIJKLMNO'[c]}{r}"
            if v is None:
                continue
            if isinstance(v, str) and shared:
                strings.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            elif isinstance(v, str):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
        if shared:
            z.writestr("xl/sharedStrings.xml", f'<sst {ns}>{"".join(f"<si><t>{s}</t></si>" for s in strings)}</sst>')
    return buf.getvalue()


@pytest.mark.parametrize("shared", [False, True])
def test_xlsx_reader_handles_inline_and_shared_strings_and_blank_cells(shared):
    header = ["NSDL Scheme Code", "Scheme Name", "Scheme Type", "Scheme Category", "TER Date", "Regular Plan - Total TER (%)", "Direct Plan - Total TER (%)"]
    content = _mini_xlsx([header, ["N/1", "Mini Fund", "Open Ended", "Cat", 46266, 1.5, None], [None, "Mini Fund", "Open Ended", "Cat", 46267, 1.5, 0.5], ["Disclaimer :"]], shared)
    rows = ter.read_xlsx_rows(content)
    assert [r["TER_Date"] for r in rows] == ["2026-09-01", "2026-09-02"]
    assert rows[0] == {"NSDLSchemeCode": "N/1", "Scheme_Name": "Mini Fund", "SchemeType_Desc": "Open Ended", "SchemeCat_Desc": "Cat", "TER_Date": "2026-09-01", "R_TER": 1.5}
    assert "NSDLSchemeCode" not in rows[1] and rows[1]["D_TER"] == 0.5


def test_xlsx_reader_rejects_an_unknown_workbook():
    with pytest.raises(ValueError, match="Unrecognised TER workbook"):
        ter.read_xlsx_rows(_mini_xlsx([["Foo", "Bar"], ["x", 1]]))


# --------------------------------------------------------------------------- change points


def recs_for(code, values: dict[date, float], name="Alpha Equity Fund"):
    return ter.parse_ter_rows([api_row(code, d, name, r=v) for d, v in values.items()])


def test_change_points_keep_only_changes_and_ignore_gaps():
    d = date(2026, 9, 1)
    series = {d: 2.0, d + timedelta(1): 2.0, d + timedelta(5): 2.0, d + timedelta(6): 2.5, d + timedelta(7): 2.5, d + timedelta(9): 2.0}  # gap days carry the value
    out = ter.change_points(recs_for("A/1", series))
    assert [(r["effective_from"], r["r_ter"]) for r in out] == [(d, 2.0), (d + timedelta(6), 2.5), (d + timedelta(9), 2.0)]


def test_change_points_input_order_and_interleaving_do_not_matter():
    a = recs_for("A/1", {date(2026, 9, 1): 2.0, date(2026, 9, 2): 2.0, date(2026, 9, 3): 2.1})
    b = recs_for("B/1", {date(2026, 9, 1): 1.0, date(2026, 9, 2): 1.2, date(2026, 9, 3): 1.2}, name="Beta Fund")
    mixed = [a[2], b[1], a[0], b[2], b[0], a[1]]
    out = ter.change_points(mixed)
    assert [(r["nsdl_code"], r["effective_from"].day) for r in out] == [("A/1", 1), ("A/1", 3), ("B/1", 1), ("B/1", 2)]


def test_change_points_same_day_twice_keeps_the_last():
    d = date(2026, 9, 1)
    out = ter.change_points(recs_for("A/1", {d: 2.0}) + recs_for("A/1", {d: 2.2}))
    assert [r["r_ter"] for r in out] == [2.2]


def test_change_points_month_boundary_uses_the_value_in_effect_before():
    first = recs_for("A/1", {date(2026, 10, 1): 2.0})
    same = {"A/1": ter._key(recs_for("A/1", {date(2026, 9, 30): 2.0})[0])}
    other = {"A/1": ter._key(recs_for("A/1", {date(2026, 9, 30): 1.9})[0])}
    assert ter.change_points(first) and ter.change_points(first, other)
    assert ter.change_points(first, same) == []


def test_a_rename_or_recategorisation_is_a_change():
    a = recs_for("A/1", {date(2026, 9, 1): 2.0}, name="Old Name Fund")
    b = recs_for("A/1", {date(2026, 9, 2): 2.0}, name="New Name Fund")
    c = ter.parse_ter_rows([api_row("A/1", date(2026, 9, 3), "New Name Fund", cat="Another Category")])
    assert len(ter.change_points(a + b + c)) == 3


# --------------------------------------------------------------------------- storing months


def month_rows(month: str) -> list[dict]:
    """Synthetic daily AMFI rows for Aug/Sep/Oct 2026 covering every boundary situation."""
    m, y = (int(x) for x in month.split("-"))
    first, last = ter.month_window(month)
    rows = []
    for d in days(first, last):
        if d > date(2026, 10, 14):
            break
        v = {
            "T/A": 2.0,  # never changes: one row in total
            "T/B": 2.0 if d < date(2026, 9, 1) else 2.5,  # changes exactly on a month boundary
            "T/C": 2.2 if date(2026, 9, 15) <= d < date(2026, 10, 1) else 2.0,  # changes back on a month boundary
            "T/G": 3.0 if d < date(2026, 10, 10) else 3.1,  # changes late in the last month
        }
        for code, r in v.items():
            rows.append(api_row(code, d, f"{code} Fund", r=r))
        if d >= date(2026, 9, 10):  # a scheme launched mid-month
            rows.append(api_row("T/D", d, "T/D Fund", r=1.0))
        if d <= date(2026, 8, 20):  # a scheme that stops being disclosed
            rows.append(api_row("T/E", d, "T/E Fund", r=1.5))
        if d.weekday() < 5:  # business-day-only publisher (1 Aug 2026 is a Saturday)
            rows.append(api_row("T/F", d, "T/F Fund", r=0.8))
    return rows


def store(db, month: str, rows: list[dict] | None = None) -> dict:
    return ter.store_month(db, month, ter.parse_ter_rows(month_rows(month) if rows is None else rows))


def test_store_month_is_idempotent(db):
    first = store(db, "09-2026")
    assert first["inserted"] == first["change_points"] > 0 and first["deleted"] == 0
    before = dump(db)
    again = store(db, "09-2026")
    assert (again["inserted"], again["updated"], again["deleted"]) == (0, 0, 0) and again["unchanged"] == first["change_points"]
    assert dump(db) == before


MONTHS = ["08-2026", "09-2026", "10-2026"]


def test_months_stored_in_any_order_give_the_same_rows(db):
    """store_month on its own handles every order except one that ends with September, where October's
    elided first row can only be restored by re-storing October (what sync_ter does; see below)."""
    results = {}
    for order in itertools.permutations(MONTHS):
        if order[-1] == "09-2026":
            continue
        db.execute(delete(SchemeTer))
        db.commit()
        for m in order:
            store(db, m)
        results[order] = dump(db)
    assert len(results) == 4 and len({tuple(v) for v in results.values()}) == 1
    rows = next(iter(results.values()))
    assert dict(Counter(r[0] for r in rows)) == {"T/A": 1, "T/B": 2, "T/C": 3, "T/D": 1, "T/E": 1, "T/F": 1, "T/G": 2}


def test_month_boundary_rows_appear_only_when_the_value_changes(db):
    for m in ("08-2026", "09-2026", "10-2026"):
        store(db, m)
    rows = {(r[0], r[1]): r for r in dump(db)}
    assert ("T/A", date(2026, 9, 1)) not in rows and ("T/A", date(2026, 10, 1)) not in rows  # unchanged across boundaries
    assert ("T/B", date(2026, 9, 1)) in rows  # changed on the boundary
    assert ("T/C", date(2026, 10, 1)) in rows and ("T/C", date(2026, 9, 15)) in rows
    assert ("T/D", date(2026, 9, 10)) in rows  # first record of a new scheme
    assert ("T/F", date(2026, 8, 3)) in rows and len([k for k in rows if k[0] == "T/F"]) == 1  # weekends/gaps add nothing


def test_stored_change_points_reconstruct_every_daily_value(db):
    for m in ("10-2026", "08-2026", "09-2026", "10-2026"):  # September after both neighbours, then October refreshed
        store(db, m)
    cps = {}
    for row in dump(db):
        cps.setdefault(row[0], []).append(row)
    for m in ("08-2026", "09-2026", "10-2026"):
        for rec in ter.parse_ter_rows(month_rows(m)):
            history = [r for r in cps[rec["nsdl_code"]] if r[1] <= rec["effective_from"]]
            assert history[-1][2:] == ter._key(rec), (rec["nsdl_code"], rec["effective_from"])
    assert count(db) == 11  # against ~200 daily rows


def test_a_revised_month_replaces_its_rows(db):
    store(db, "09-2026")
    rows = month_rows("09-2026")
    revised = [r for r in rows if r["NSDLSchemeCode"] != "T/D"]  # the scheme vanishes from the month
    for r in revised:
        if r["NSDLSchemeCode"] == "T/A" and r["TER_Date"] >= "2026-09-20":
            r["R_TER"] = "2.1000"  # a correction from the 20th
    out = store(db, "09-2026", revised)
    assert out["inserted"] == 1 and out["deleted"] >= 1
    assert not [r for r in dump(db) if r[0] == "T/D"]
    assert [r[1] for r in dump(db) if r[0] == "T/A"] == [date(2026, 9, 1), date(2026, 9, 20)]


def test_a_growing_current_month_only_adds_new_change_points(db):
    rows = month_rows("10-2026")
    early = [r for r in rows if r["TER_Date"] < "2026-10-08"]
    first = store(db, "10-2026", early)
    grown = store(db, "10-2026", rows)  # a later sync sees more days, including the change on the 10th
    assert grown["inserted"] == 1 and grown["deleted"] == 0 and grown["unchanged"] == first["change_points"]


def test_rows_outside_the_month_are_ignored(db):
    rows = [api_row("S/1", date(2026, 9, 30)), api_row("S/1", date(2026, 10, 1), r=9.9)]
    out = store(db, "09-2026", rows)
    assert out["out_of_window"] == 1 and count(db) == 1


# --------------------------------------------------------------------------- months and fetching


def month_client(extra=None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        year = request.url.params["year"]
        path = FX / f"months_{year}.json"
        body = json.loads(path.read_text()) if path.exists() else []
        if year == "2026-2027":
            body = [*body, *(extra or [])]
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_list_months_unions_the_financial_years_and_drops_the_future():
    today = date(2026, 10, 16)
    client = month_client([{"MonthYear": "November-2026", "MonthNumber": "11-2026"}])
    got = ter.list_months(client, 12, today=today)
    assert got == [f"{(10 - i - 1) % 12 + 1:02d}-{2026 if 10 - i > 0 else 2025}" for i in range(12)]
    wide = ter.list_months(client, 100, today=today)  # includes the ragged strays of the real listing
    assert "11-2026" not in wide and wide == sorted(wide, key=ter.month_sort_key, reverse=True) and len(wide) == len(set(wide))
    assert wide[0] == "10-2026" and "10-2025" in wide


def data_client(total_by_call: list[int], content: bytes = SEP_XLSX) -> tuple[httpx.Client, list]:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        calls.append(dict(p))
        if p.get("excel") == "true":
            return httpx.Response(200, content=content)
        total = total_by_call[min(len([c for c in calls if c.get("excel") != "true"]) - 1, len(total_by_call) - 1)]
        return httpx.Response(200, json={"data": [], "meta": {"page": 1, "pageSize": 1, "total": total}})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_month_rows_checks_the_row_count_against_amfi_total():
    client, calls = data_client([163])
    assert len(ter.fetch_month_rows(client, "09-2026")) == 163
    assert calls[0]["excel"] == "true" and calls[0]["Month"] == "09-2026" and calls[0]["MF_ID"] == "All"

    client, calls = data_client([999, 163])  # first answer disagrees, the retry agrees
    assert len(ter.fetch_month_rows(client, "09-2026")) == 163 and sum(1 for c in calls if c.get("excel") == "true") == 2

    client, _ = data_client([999])
    with pytest.raises(RuntimeError, match="month 09-2026"):
        ter.fetch_month_rows(client, "09-2026")


# --------------------------------------------------------------------------- sync (mocked network)


def patch_network(monkeypatch, sources: dict[str, bytes], failing=()):
    wanted = sorted(sources, key=ter.month_sort_key, reverse=True)

    def fetch(client, month):
        if month in failing:
            raise RuntimeError(f"AMFI TER month {month} failed after 3 attempts")
        return ter.read_xlsx_rows(sources[month])

    monkeypatch.setattr(ter, "http_client", lambda: contextlib.nullcontext())
    monkeypatch.setattr(ter, "list_months", lambda client, months=36, today=None: wanted[:months])
    monkeypatch.setattr(ter, "fetch_month_rows", fetch)


def seed_360(db):
    add_scheme(db, TEST_CODES + 101, "360 ONE Balanced Hybrid Fund - Regular Plan - Growth")
    add_scheme(db, TEST_CODES + 102, "360 ONE Balanced Hybrid Fund - Direct Plan - Growth", plan="direct")
    add_scheme(db, TEST_CODES + 103, "Zerodha Silver ETF FoF - Regular Plan - Growth")  # a Direct-only scheme: Regular columns are 0
    add_scheme(db, TEST_CODES + 104, "Zerodha Silver ETF FoF - Direct Plan - Growth", plan="direct")
    db.commit()


def test_sync_ter_end_to_end(db, monkeypatch):
    seed_360(db)
    patch_network(monkeypatch, {"11-2023": NOV_XLSX, "09-2026": SEP_XLSX})
    progress = []
    out = ter.sync_ter(db, months=2, progress=lambda *a: progress.append(a))

    assert out["months"] == ["11-2023", "09-2026"] and out["failed_months"] == {} and out["boundary_months_refreshed"] == []
    assert [(p[0], p[1], p[2], p[3]) for p in progress] == [(1, 2, "11-2023", 90), (2, 2, "09-2026", 163)]
    assert out["daily_rows_read"] == 253 and out["stored_rows"] == out["inserted"] == count(db) < 20
    # 360 ONE has a name-only series in 2023: it joined the coded series of 2026 instead of getting a key of its own.
    assert out["stored_schemes"] == 6 + 2
    series = [r for r in dump(db) if r[0] == "360O/O/H/BHF/23/07/0007"]
    assert [r[1] for r in series] == [date(2023, 11, 1), date(2026, 9, 1)]
    old = db.scalars(select(SchemeTer).where(SchemeTer.effective_from == date(2023, 11, 1), SchemeTer.nsdl_code == "360O/O/H/BHF/23/07/0007")).one()
    assert (old.r_ber, old.r_statutory, old.r_ter, old.r_brokerage, old.r_transaction) == (1.95, 0.06, 2.06, None, None)
    assert out["links"]["matched"] == 2 and out["links"]["amfi_codes_linked"] == 4 and out["links"]["unmatched"] == 6

    got = ter.latest_ter(db, {TEST_CODES + i for i in (101, 102, 103, 104)} | {1})
    assert set(got) == {TEST_CODES + 101, TEST_CODES + 102, TEST_CODES + 104}  # 103: Regular plan of a Direct-only scheme
    assert got[TEST_CODES + 101] == {"ter": 2.19, "ber": 1.78, "brokerage": 0.03, "transaction": 0.0, "statutory": 0.38, "plan": "regular",
                                     "effective_from": date(2026, 9, 1), "nsdl_code": "360O/O/H/BHF/23/07/0007"}
    assert got[TEST_CODES + 102]["ter"] == 0.62 and got[TEST_CODES + 102]["plan"] == "direct" and got[TEST_CODES + 104]["ter"] == 0.21

    again = ter.sync_ter(db, months=2)  # a repeat changes nothing
    assert (again["inserted"], again["updated"], again["deleted"]) == (0, 0, 0) and count(db) == out["stored_rows"]


def test_sync_ter_reconstructs_every_daily_row_from_the_change_points(db, monkeypatch):
    patch_network(monkeypatch, {"11-2023": NOV_XLSX, "09-2026": SEP_XLSX})
    ter.sync_ter(db, months=2)
    cmap = ter.build_code_map(ter.read_xlsx_rows(SEP_XLSX))
    cps = {}
    for row in dump(db):
        cps.setdefault(row[0], []).append(row)
    daily = ter.parse_ter_rows(ter.read_xlsx_rows(NOV_XLSX), cmap) + ter.parse_ter_rows(ter.read_xlsx_rows(SEP_XLSX), cmap)
    assert len(daily) == 253
    for rec in daily:
        history = [r for r in cps[rec["nsdl_code"]] if r[1] <= rec["effective_from"]]
        assert history[-1][2:] == ter._key(rec), (rec["nsdl_code"], rec["effective_from"])


def test_sync_ter_reports_a_failed_month_and_keeps_going(db, monkeypatch):
    patch_network(monkeypatch, {"11-2023": NOV_XLSX, "09-2026": SEP_XLSX}, failing={"11-2023"})
    out = ter.sync_ter(db, months=2)
    assert out["months"] == ["09-2026"] and list(out["failed_months"]) == ["11-2023"]
    assert count(db) > 0 and not [r for r in dump(db) if r[1].year == 2023]


def test_an_empty_month_never_wipes_stored_data(db, monkeypatch):
    patch_network(monkeypatch, {"11-2023": NOV_XLSX, "09-2026": SEP_XLSX})
    ter.sync_ter(db, months=2)
    before = dump(db)
    monkeypatch.setattr(ter, "fetch_month_rows", lambda client, month: [])  # AMFI answers, but with nothing
    out = ter.sync_ter(db, months=2)
    assert out["empty_months"] == ["11-2023", "09-2026"] and out["months"] == [] and dump(db) == before


def test_a_failing_month_is_attempted_once_per_run(db, monkeypatch):
    patch_network(monkeypatch, {"09-2026": SEP_XLSX})
    attempts = []

    def failing(client, month):
        attempts.append(month)
        raise RuntimeError("boom")

    monkeypatch.setattr(ter, "fetch_month_rows", failing)
    out = ter.sync_ter(db, months=1)
    assert attempts == ["09-2026"] and list(out["failed_months"]) == ["09-2026"] and count(db) == 0


@pytest.mark.parametrize("order", list(itertools.permutations(MONTHS)))
def test_one_month_per_sync_in_any_order_gives_the_canonical_rows(db, monkeypatch, order):
    """Gap-fill / out-of-order runs: sync_ter refreshes the next stored month so no boundary row goes stale."""
    monkeypatch.setattr(ter, "http_client", lambda: contextlib.nullcontext())
    monkeypatch.setattr(ter, "fetch_month_rows", lambda client, month: month_rows(month))
    for m in order:
        monkeypatch.setattr(ter, "list_months", lambda client, months=36, today=None, m=m: [m])
        ter.sync_ter(db, months=1)
    got = dump(db)
    db.execute(delete(SchemeTer))
    db.commit()
    for m in MONTHS:  # chronological
        store(db, m)
    assert got == dump(db)


def test_sync_refreshes_the_next_stored_month(db, monkeypatch):
    for m in ("08-2026", "10-2026"):
        store(db, m)  # September was missing when these were synced
    monkeypatch.setattr(ter, "http_client", lambda: contextlib.nullcontext())
    monkeypatch.setattr(ter, "list_months", lambda client, months=36, today=None: ["09-2026"])
    monkeypatch.setattr(ter, "fetch_month_rows", lambda client, month: month_rows(month))
    out = ter.sync_ter(db, months=1)
    assert out["boundary_months_refreshed"] == ["10-2026"]
    assert ("T/C", date(2026, 10, 1)) in {(r[0], r[1]) for r in dump(db)}  # the row that had been elided without September


# --------------------------------------------------------------------------- linking


def seed_ter(db, entries, month="09-2026", day=date(2026, 9, 1)):
    """entries: (nsdl_code, scheme name, regular TER, direct TER)."""
    ter.store_month(db, month, ter.parse_ter_rows([api_row(c, day, n, r=r, d=d) for c, n, r, d in entries]))


def test_link_ter_codes(db):
    add_scheme(db, TEST_CODES + 1, "Zephyr Flexi Cap Fund - Regular Plan - Growth")
    add_scheme(db, TEST_CODES + 2, "Zephyr Flexi Cap Fund - Direct Plan - Growth", plan="direct")
    add_scheme(db, TEST_CODES + 3, "Zephyr Flexi Cap Fund - Regular Plan - IDCW", option="idcw")
    add_scheme(db, TEST_CODES + 4, "Zephyr Value Fund - Growth")
    add_scheme(db, TEST_CODES + 5, "Zephyr Nifty 50 Index Fund - Regular Growth")
    add_scheme(db, TEST_CODES + 6, "Zephyr ELSS Tax Saver Fund - Growth")
    add_scheme(db, TEST_CODES + 7, "Zephyr Master Equity Fund - Growth")
    db.add(SchemeDetail(amfi_code=TEST_CODES + 7, scheme_base_name="Zephyr Master Equity Plan", raw={}))
    db.commit()
    seed_ter(db, [
        ("Z/1", "Zephyr Flexi Cap Fund", 2.0, 1.0),
        ("Z/2", "Zephyr Value Fund (scheme has Two segregated portfolios)", 2.1, 1.1),
        ("Z/3", "Zephyr NIFTY50 Index Fund", 0.5, 0.2),
        ("Z/4", "Zephyr ELSS Tax Saver Nifty 50 Index Fund", 0.6, 0.2),  # a different scheme whose words are a superset
        ("Z/5", "Zephyr Master Equity Plan", 2.2, 1.2),
        ("Z/6", "Zephyr Totally Unknown Fund", 2.3, 1.3),
    ])
    out = ter.link_ter_codes(db)
    links = {k.amfi_code - TEST_CODES: k for k in db.scalars(select(SchemeLink).where(SchemeLink.source == "ter", SchemeLink.amfi_code.between(*TEST_RANGE)))}

    assert {k: v.external_key for k, v in links.items()} == {1: "Z/1", 2: "Z/1", 3: "Z/1", 4: "Z/2", 5: "Z/3", 7: "Z/5"}
    assert (links[1].method, links[1].score) == ("exact", 100.0)
    assert (links[4].method, links[4].score) == ("fuzzy", 98.0)  # matched once the parenthetical note was dropped
    assert (links[5].method, links[5].score) == ("fuzzy", 99.0)  # "NIFTY50" vs "Nifty 50"
    assert (links[7].method, links[7].score) == ("exact", 100.0)  # via the scheme master's base name
    assert 6 not in links  # the ELSS fund is not the Nifty 50 ELSS index fund
    assert sorted(out["unmatched_examples"]) == ["Zephyr ELSS Tax Saver Nifty 50 Index Fund", "Zephyr Totally Unknown Fund"]
    assert out["ter_schemes"] == 6 and out["matched"] == 4 and out["unmatched"] == 2 and out["shadowed"] == 0

    before = db.scalars(select(SchemeLink.amfi_code).where(SchemeLink.source == "ter")).all()
    assert ter.link_ter_codes(db)["stale_links_removed"] == 0
    assert db.scalars(select(SchemeLink.amfi_code).where(SchemeLink.source == "ter")).all() == before


def test_an_amfi_code_claimed_twice_goes_to_the_exact_then_latest_scheme(db):
    add_scheme(db, TEST_CODES + 1, "Zephyr Gold ETF - Growth")
    add_scheme(db, TEST_CODES + 2, "Zephyr Gold ETF Fund of Fund - Growth")
    db.commit()
    seed_ter(db, [("G/1", "Zephyr Gold ETF - IDCW", 0.3, 0.1)], day=date(2026, 9, 1))
    seed_ter(db, [("G/2", "Zephyr Gold ETF - Growth", 0.35, 0.15)], month="10-2026", day=date(2026, 10, 2))
    out = ter.link_ter_codes(db)
    link = db.get(SchemeLink, (TEST_CODES + 1, "ter"))
    assert link.external_key == "G/2"  # same normalised name and score: the more recently disclosed wins
    assert out["shadowed"] == 1 and out["shadowed_examples"] == ["Zephyr Gold ETF - IDCW"]


def test_stale_links_are_removed_when_a_scheme_no_longer_matches(db):
    add_scheme(db, TEST_CODES + 1, "Zephyr Flexi Cap Fund - Growth")
    db.commit()
    seed_ter(db, [("Z/1", "Zephyr Flexi Cap Fund", 2.0, 1.0)])
    ter.link_ter_codes(db)
    assert db.get(SchemeLink, (TEST_CODES + 1, "ter")) is not None
    db.execute(delete(SchemeTer))
    seed_ter(db, [("Z/1", "Zephyr Renamed Beyond Recognition", 2.0, 1.0)])
    assert ter.link_ter_codes(db)["stale_links_removed"] == 1
    assert db.get(SchemeLink, (TEST_CODES + 1, "ter")) is None


def test_links_of_other_sources_are_left_alone(db):
    add_scheme(db, TEST_CODES + 1, "Zephyr Flexi Cap Fund - Growth")
    db.add(SchemeLink(amfi_code=TEST_CODES + 1, source="performance", external_key="PERF-1", method="exact", score=100.0))
    db.commit()
    seed_ter(db, [("Z/1", "Zephyr Flexi Cap Fund", 2.0, 1.0)])
    ter.link_ter_codes(db)
    assert db.get(SchemeLink, (TEST_CODES + 1, "performance")).external_key == "PERF-1"
    assert db.get(SchemeLink, (TEST_CODES + 1, "ter")).external_key == "Z/1"


# --------------------------------------------------------------------------- reads


def test_latest_ter_uses_the_latest_row_and_the_plan_of_each_code(db):
    add_scheme(db, TEST_CODES + 1, "Zephyr Flexi Cap Fund - Regular Plan - Growth")
    add_scheme(db, TEST_CODES + 2, "Zephyr Flexi Cap Fund - Direct Plan - Growth", plan="direct")
    add_scheme(db, TEST_CODES + 3, "Zephyr Orphan Fund - Growth")
    db.commit()
    rows = [api_row("Z/1", d, "Zephyr Flexi Cap Fund", r=2.0 if d < date(2026, 9, 15) else 2.4, d=1.0 if d < date(2026, 9, 15) else 1.3) for d in days(date(2026, 9, 1), date(2026, 9, 30))]
    ter.store_month(db, "09-2026", ter.parse_ter_rows(rows))
    ter.link_ter_codes(db)
    got = ter.latest_ter(db, {TEST_CODES + 1, TEST_CODES + 2, TEST_CODES + 3, 123})
    assert set(got) == {TEST_CODES + 1, TEST_CODES + 2}
    reg, direct = got[TEST_CODES + 1], got[TEST_CODES + 2]
    assert (reg["plan"], reg["ter"], reg["ber"], reg["brokerage"], reg["transaction"], reg["statutory"]) == ("regular", 2.4, 2.0, 0.03, 0.0, 0.37)
    assert (direct["plan"], direct["ter"], direct["ber"]) == ("direct", 1.3, 0.9)
    assert reg["effective_from"] == direct["effective_from"] == date(2026, 9, 15) and reg["nsdl_code"] == "Z/1"
    assert ter.latest_ter(db, set()) == {}


def test_latest_ter_for_one_plan_costs_every_code_alike_and_skips_absent_plans(db):
    add_scheme(db, TEST_CODES + 1, "Zephyr Solo Fund - Regular Plan - Growth")
    add_scheme(db, TEST_CODES + 2, "Zephyr Solo Fund - Direct Plan - Growth", plan="direct")
    add_scheme(db, TEST_CODES + 3, "Zephyr Free Fund - Regular Plan - Growth")
    db.commit()
    seed_ter(db, [("S/1", "Zephyr Solo Fund", 1.8, 0.9), ("S/2", "Zephyr Free Fund", 0.0, 0.7)])  # R_TER 0 = no such plan
    ter.link_ter_codes(db)
    got = ter.latest_ter(db, {TEST_CODES + 1, TEST_CODES + 2, TEST_CODES + 3}, plan="regular")
    assert {c: v["ter"] for c, v in got.items()} == {TEST_CODES + 1: 1.8, TEST_CODES + 2: 1.8}  # never a 0% "free" fund
    assert set(ter.current_ter_rows(db, {TEST_CODES + 1, TEST_CODES + 3, 123})) == {TEST_CODES + 1, TEST_CODES + 3}
