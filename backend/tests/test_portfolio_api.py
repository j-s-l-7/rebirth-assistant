from datetime import date

import pytest
from fastapi.testclient import TestClient
from pytest import approx
from sqlalchemy import delete

from app.analytics.xray import BANNED_WORDS, SCREEN_NOTE
from app.main import app
from app.models import Scheme, SchemeHolding

MONTH = date(2026, 9, 30)
# (isin, name, sector, weight) per fund; A and B share INE1 and INE2, C shares nothing with them.
ROWS = {
    0: [("INE000000001", "Alpha Ltd", "Financials", 10.0), ("INE000000002", "Beta Ltd", "Energy", 20.0),
        ("INE000000003", "Gamma Ltd", "Energy", 70.0)],
    1: [("INE000000001", "Alpha Ltd", "Financials", 30.0), ("INE000000002", "Beta Ltd", "Energy", 10.0),
        ("INE000000004", "Delta Ltd", "Materials", 60.0)],
    2: [("INE000000009", "Omega Ltd", "Health Care", 100.0)],
}


@pytest.fixture(scope="module")
def funds(market):
    from app.db import SessionLocal

    db = SessionLocal()
    codes = market["small_cap"]
    db.add_all(SchemeHolding(amfi_code=codes[i], disclosure_month=MONTH, security_isin=isin, security_name=name,
                             sector=sector, asset_class="equity", weight_pct=w)
               for i, rows in ROWS.items() for isin, name, sector, w in rows)
    db.commit()
    names = [db.get(Scheme, c).name for c in codes]
    try:
        yield names
    finally:
        db.execute(delete(SchemeHolding).where(SchemeHolding.amfi_code.in_(codes)))
        db.commit()
        db.close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _strings(obj, key=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, key)
    elif isinstance(obj, str):
        yield obj


def _assert_clean(payload):
    for s in _strings(payload):
        text = s.replace(SCREEN_NOTE, "").lower()
        assert not [w for w in BANNED_WORDS if w in text], s


def test_xray_two_funds(client, funds):
    r = client.post("/api/portfolio/xray", json={"holdings": [{"fund": funds[0][:21], "amount_inr": 60000},
                                                              {"fund": funds[1], "amount_inr": 40000}],
                                                 "objective": {"max_security_pct": 30}})
    assert r.status_code == 200
    out = r.json()
    assert [h["fund"] for h in out["holdings"]] == funds[:2]
    assert out["holdings"][0]["weight_pct"] == approx(60) and out["holdings"][0]["valuation_method"] == "amount"
    assert out["disclosure_months"] == {funds[0]: "2026-09", funds[1]: "2026-09"}
    assert out["overlap"][0]["overlap_pct"] == approx(20) and out["overlap"][0]["common_securities"] == 2
    gamma = next(e for e in out["exposures"]["securities"] if e["name"] == "Gamma Ltd")
    assert gamma["pct"] == approx(42)
    assert out["exposures"]["amcs"] and out["coverage"]["covered_value_pct"] == approx(100)
    assert out["objective_checks"][0]["status"] == "exceeds"
    assert "Based on latest monthly disclosure per fund" in out["assumptions"]
    assert out["errors"] == [] and out["missing_details"] == [] and out["reference"] is None
    _assert_clean(out)


def test_xray_units_unknown_and_missing_disclosure(client, funds):
    r = client.post("/api/portfolio/xray", json={"holdings": [
        {"fund": funds[0], "units": 100}, {"fund": "Zzqx Nonexistent Qwerty"}, {"fund": funds[3], "amount_inr": 1000}]})
    out = r.json()
    assert out["holdings"][0]["valuation_method"] == "units_x_nav"
    assert out["errors"][0]["fund"] == "Zzqx Nonexistent Qwerty" and out["errors"][0]["code"] == "not_found"
    assert out["missing_details"] == [{"fund": funds[3], "need": "portfolio disclosure not loaded"}]
    assert out["coverage"]["funds_without_disclosure"] == [funds[3]]
    _assert_clean(out)


def test_simulate_applies_valid_and_rejects_invalid_moves(client, funds):
    r = client.post("/api/portfolio/simulate", json={
        "holdings": [{"fund": funds[0], "amount_inr": 50000}, {"fund": funds[1], "amount_inr": 50000}],
        "changes": [{"from_fund": funds[0], "to_fund": funds[2], "amount_inr": 10000},
                    {"from_fund": funds[1], "to_fund": funds[2], "amount_inr": 99999999}]})
    out = r.json()
    assert [c["applied"] for c in out["changes_applied"]] == [True, False]
    assert out["deltas"]["securities"]
    _assert_clean(out)


def test_screen_lists_low_overlap_candidates_with_note(client, funds):
    r = client.post("/api/portfolio/screen", json={"holdings": [{"fund": funds[0], "amount_inr": 50000}],
                                                   "amount_inr": 10000})
    out = r.json()
    assert out["note"] == SCREEN_NOTE
    names = [c["name"] for c in out["candidates"]]
    assert funds[0] not in names and funds[3] not in names and names[0] == funds[2]
    c = out["candidates"][0]
    assert c["overlap_pct"] == 0 and c["disclosure_month"] == "2026-09" and c["rank"]["of"] == 4 and "effect" in c
    _assert_clean(out)
    assert client.post("/api/portfolio/screen", json={"holdings": [{"fund": funds[0], "amount_inr": 1}],
                                                      "category": "nope"}).status_code == 422


def test_fund_search(client, funds):
    out = client.get("/api/funds/search", params={"q": funds[0][:21]}).json()
    assert out[0]["name"] == funds[0] and set(out[0]) == {"name", "amfi_code", "category"} and len(out) <= 10
