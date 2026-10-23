import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pytest import approx
from sqlalchemy import delete

from app.analytics.xray import BANNED_WORDS, SCREEN_NOTE
from app.copilot.compliance import ACTION_NOTE, post_process, sources_from_tool_calls, untraced_numbers
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


# --------------------------------------------------------------------------- chat tool


def _xray_args(funds):
    return {"holdings": [{"fund": funds[0], "amount_inr": 60000}, {"fund": funds[1], "amount_inr": 40000}],
            "objective": {"max_security_pct": 30},
            "whatif_changes": [{"from_fund": funds[0], "to_fund": funds[2], "amount_inr": 10000}], "screen": True}


def test_xray_tool_returns_compact_result(db, funds):
    from app.copilot.tools import ToolContext, tool_definitions
    from app.models import Investor

    assert "portfolio_xray" in [t["name"] for t in tool_definitions()]
    out, ok = ToolContext(db, Investor()).run("portfolio_xray", _xray_args(funds))
    assert ok and out["disclosure_months"] == {funds[0]: "2026-09", funds[1]: "2026-09"}
    assert len(out["top_securities"]) <= 8 and len(out["top_sectors"]) <= 5 and len(out["top_overlap_pairs"]) <= 3
    assert out["top_securities"][0] == {"name": "Gamma Ltd", "pct": 42.0, "funds": [funds[0]]}
    assert out["objective_checks"][0]["status"] == "exceeds" and "exposures" not in out
    assert out["whatif"]["changes_applied"][0]["applied"] and out["whatif"]["deltas"]["securities"]
    assert out["screen"]["note"] == SCREEN_NOTE and out["screen"]["candidates"][0]["name"] == funds[2]
    assert len(json.dumps(out)) < 4000
    assert ToolContext(db, Investor()).run("portfolio_xray", {"holdings": []})[1] is False


def test_xray_chat_answer_is_rewritten_and_traced(funds, monkeypatch):
    from app.copilot import orchestrator
    from app.models import ChatMessage
    from tests.test_api_chat import Block, FakeStream, _sse

    class Messages:
        def stream(self, **kw):
            last = kw["messages"][-1]
            if isinstance(last["content"], list) and last["content"][0].get("type") == "tool_result":
                d = json.loads(last["content"][0]["content"])
                top, pair = d["top_securities"][0], d["top_overlap_pairs"][0]
                text = (f"Per the {d['disclosure_months'][funds[0]]} disclosures, {top['name']} is {top['pct']:.1f}% of your portfolio "
                        f"and the two funds share {pair['overlap_pct']:.1f}% by weight. You should switch to {funds[2]}.")
                content = [Block(type="text", text=text)]
                return FakeStream(SimpleNamespace(stop_reason="end_turn", content=content, model="claude-fable-5-1", usage=None))
            content = [Block(type="tool_use", id="tu_x", name="portfolio_xray", input=_xray_args(funds))]
            return FakeStream(SimpleNamespace(stop_reason="tool_use", content=content, model="claude-fable-5-1", usage=None))

    class Plain:
        async def create(self, **kw):
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"route":"in_scope","topic":"mf_facts","reason":"x"}')])

    fake = SimpleNamespace(beta=SimpleNamespace(messages=Messages()), messages=Plain())
    monkeypatch.setattr(orchestrator, "get_client", lambda: fake)
    c = TestClient(app)
    inv = c.post("/api/investors", json={"display_name": "Guest"}).json()["id"]
    sid = c.post(f"/api/investors/{inv}/chat/sessions").json()["id"]
    done = _sse(c.post(f"/api/chat/sessions/{sid}/messages", json={"text": "How much do my funds overlap?"}).text)[-1][1]
    assert "switch to" not in done["text"].lower() and "Gamma Ltd is 42.0%" in done["text"] and ACTION_NOTE in done["text"]

    from app.db import SessionLocal

    with SessionLocal() as s:
        g = s.get(ChatMessage, done["message_id"]).guardrail
    assert g["untraced_numbers"] == []  # numbers from the portfolio_xray result count as traced
    assert {f["code"] for f in g["compliance_flags"]} == {"action_language"}
    tool_log = [{"name": "portfolio_xray", "output": {"top_securities": [{"pct": 42.0}]}}]
    assert untraced_numbers("Gamma is 42.0%, not 37.3%.", sources_from_tool_calls(tool_log, "")) == ["37.3%"]


def test_action_language_only_on_xray_turns():
    text = "This is not a recommendation. Exit load is 1%."
    assert post_process(text, {"fund_lens"})[0] == text
    shown, flags, _ = post_process(text, {"portfolio_xray"})
    assert shown.startswith("This is not a recommendation.") and "Exit load" not in shown
    assert [f["match"] for f in flags] == ["Exit"]
