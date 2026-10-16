"""AMFI scheme-details master: parsing quirks (trimmed real rows) and the idempotent sync."""

import csv
import io
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app.data import scheme_details
from app.data.scheme_details import parse_min_amount, parse_scheme_details, split_isins, sync_scheme_details
from app.models import SchemeDetail

FIXTURE = Path(__file__).parent / "fixtures" / "scheme_details" / "scheme_data_sample.csv"
TEXT = FIXTURE.read_bytes().decode("utf-8")


@pytest.fixture(scope="module")
def parsed() -> dict[int, dict]:
    return {r["amfi_code"]: r for r in parse_scheme_details(TEXT)}


@pytest.fixture(autouse=True)
def clean_table(db):
    db.execute(delete(SchemeDetail))
    db.commit()
    yield
    db.rollback()
    db.execute(delete(SchemeDetail))
    db.commit()


def test_every_row_is_kept_with_the_untouched_source_row(parsed):
    source = list(csv.reader(io.StringIO(TEXT, newline="")))
    header, body = source[0], source[1:]
    assert len(parsed) == len(body) == 33
    for row in body:
        assert parsed[int(row[1])]["raw"] == dict(zip(header, row))
    raw = parsed[100033]["raw"]
    assert list(raw)[8] == " Closure Date"  # the source's leading space survives in raw only
    assert parsed[154018]["raw"]["Scheme Minimum Amount"] == "  During NFO  Rs. 100/- and any amount thereafter"


def test_text_fields_and_dates(parsed):
    r = parsed[100034]
    assert r["amc_full_name"] == "Aditya Birla Sun Life AMC Limited"
    assert r["scheme_base_name"] == "Aditya Birla Sun Life Large & Mid Cap Fund"
    assert r["nav_name"].endswith("-Regular - IDCW")
    assert (r["scheme_type"], r["scheme_category"]) == ("Open Ended", "Equity Scheme - Large & Mid Cap Fund")
    assert r["launch_date"] == date(1995, 2, 24) and r["closure_date"] == date(1995, 2, 24)
    assert parsed[100046]["closure_date"] == date(1997, 6, 7)  # NFO close, days after launch
    assert parsed[100123]["closure_date"] is None
    assert parsed[118804]["scheme_type"] == "Interval Fund"


def test_blank_and_placeholder_dates_are_null(parsed):
    assert parsed[100373]["launch_date"] is None and parsed[100373]["min_amount"] is None  # blank row
    assert parsed[108558]["launch_date"] is None and parsed[108558]["closure_date"] is None  # 01-Jan-1900 placeholder
    assert parsed[108558]["raw"]["Launch Date"] == "01-Jan-1900"
    assert parsed[154551]["launch_date"] == date(2026, 8, 3)  # future launch dates are real


def test_name_with_literal_double_quotes_parses_as_one_row(parsed):
    assert parsed[103652]["nav_name"] == 'JM FMF SERIES II - YEARLY "A"-Dividend Plan'
    assert parsed[103652]["min_amount"] == 5000.0


@pytest.mark.parametrize(
    "code, isins",
    [
        (100033, ["INF209K01165"]),
        (100034, ["INF209K01157", "INF209K01CE5"]),  # two ISINs glued together
        (118804, ["INF204K01B81"]),  # trailing "-"
        (127615, ["INF017Q01011"]),  # trailing "NA"
        (129286, ["INF613Q01033"]),  # trailing "Redeemed"
        (152713, ["INF179KC1IM3"]),  # trailing exchange ticker
        (133790, ["INF178L01CE0"]),  # trailing space
        (133793, ["INF178L01CC4", "INF178L01CB6"]),  # space-separated pair
        (100373, []),
    ],
)
def test_isins_are_extracted_not_sliced(parsed, code, isins):
    assert parsed[code]["isins"] == isins


def test_isin_shared_by_two_codes_is_kept_on_both(parsed):
    assert parsed[131346]["isins"] == parsed[131539]["isins"] == ["INF204KA1US6"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("5000", 5000.0),
        ("5000.00", 5000.0),
        ("5,000", 5000.0),
        ("", None),
        ("0", None),
        ("REFER SID", None),
        ("#@", None),
        ("Rs. 500/- and in multiples of Re. 1/- thereafter", 500.0),
        ("5000/-", 5000.0),
        ("5000 and in multiples of Rs.1 thereafter", 5000.0),  # the leading amount, not the later multiple
        ("Rs 5000 and in multiple of Rs 10", 5000.0),
        ("Rs5000 & in multiples of Rs1 thereafter during NFO", 5000.0),
        ("NF1- Rs. 5000 and in multiples of Re.1", 5000.0),  # "NF1" is a label
        ("Rs. 5 000/-", 5000.0),  # thousands typed with a space
        ("1)Growth-Rs.15000 2)Dividend-Rs. 100000/-", 15000.0),  # "1)" is a list marker
        ("  During NFO  Rs. 100/- and any amount thereafter", 100.0),
        ("Retail  Rs.5000 Institutional  Rs.1 Crore", 5000.0),
        ("PlanA Rs.50 lakhs; PlanB Rs.25000 (MD&QD)", 5_000_000.0),
        ("1 crore", 10_000_000.0),
        ("Rs. 20 Crores", 200_000_000.0),
        ("Rs.1 Lakh (RP)", 100_000.0),
        ("buy/sell units of Scheme in round lot of 1 unit", None),  # units, not rupees
        ("Minimum no of Units (Creation Units) - 10000 unit", None),
    ],
)
def test_min_amount(text, expected):
    assert parse_min_amount(text) == expected


def test_split_isins():
    assert split_isins("INF209K01157INF209K01CE5") == ["INF209K01157", "INF209K01CE5"]
    assert split_isins(" - ") == [] and split_isins(None) == []


def test_header_and_row_validation():
    assert parse_scheme_details("") == []
    with pytest.raises(ValueError, match="Unrecognised"):
        parse_scheme_details("Foo,Bar\r\n1,2\r\n")
    header = TEXT.splitlines()[0]
    bad = f"{header}\r\nAMC,notanumber,x,y,z,w,5000,01-Jan-2020,,\r\nAMC,1,only,three\r\n"
    assert parse_scheme_details(bad) == []  # a non-numeric code and a short row are skipped, not guessed at


def test_sync_stores_every_row_and_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(scheme_details, "fetch_scheme_details", lambda: TEXT)
    first = sync_scheme_details(db)
    assert first["rows"] == first["new"] == 33 and first["updated"] == 0 and first["duplicate_codes"] == 0
    assert first["in_scheme_master"] + first["details_only"] == 33
    assert first["with_isin"] == 29 and first["with_launch_date"] == 29  # 4 rows have no ISIN; 3 no launch date + the 1900 placeholder
    assert db.scalar(select(func.count()).select_from(SchemeDetail)) == 33

    row = db.get(SchemeDetail, 100034)
    assert row.isins == ["INF209K01157", "INF209K01CE5"] and row.launch_date == date(1995, 2, 24)
    assert row.raw["Code"] == "100034" and row.min_amount == 5000.0
    stamp = row.updated_at

    second = sync_scheme_details(db)
    assert second["new"] == 0 and second["updated"] == 33
    assert db.scalar(select(func.count()).select_from(SchemeDetail)) == 33
    db.expire_all()
    assert db.get(SchemeDetail, 100034).updated_at >= stamp


def test_sync_updates_changed_rows_and_never_wipes_on_empty_download(db, monkeypatch):
    monkeypatch.setattr(scheme_details, "fetch_scheme_details", lambda: TEXT)
    sync_scheme_details(db)
    changed = TEXT.replace("5000.00,28-Mar-2000", "1000.00,28-Mar-2001")  # UTI Nifty 50 Index Fund
    monkeypatch.setattr(scheme_details, "fetch_scheme_details", lambda: changed)
    sync_scheme_details(db)
    db.expire_all()
    row = db.get(SchemeDetail, 100822)
    assert row.min_amount == 1000.0 and row.launch_date == date(2001, 3, 28)

    monkeypatch.setattr(scheme_details, "fetch_scheme_details", lambda: "")
    with pytest.raises(RuntimeError, match="empty"):
        sync_scheme_details(db)
    assert db.scalar(select(func.count()).select_from(SchemeDetail)) == 33


def test_repeated_code_keeps_the_last_row(db, monkeypatch):
    lines = TEXT.splitlines()
    dup = "\r\n".join([*lines, lines[1].replace("5000,24-Feb-1995", "7000,24-Feb-1995")]) + "\r\n"
    monkeypatch.setattr(scheme_details, "fetch_scheme_details", lambda: dup)
    out = sync_scheme_details(db)
    assert out["rows"] == 34 and out["duplicate_codes"] == 1
    assert db.get(SchemeDetail, 100033).min_amount == 7000.0
