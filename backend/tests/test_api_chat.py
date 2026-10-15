"""API flow plus the copilot loop against a scripted fake of the Anthropic client."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.copilot import orchestrator
from app.ingest.cas import import_cas_data
from app.main import app
from app.models import Investor
from tests.synthetic import cas_dict


class Block(SimpleNamespace):
    def to_dict(self, mode="python"):
        return {k: v for k, v in vars(self).items()}


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for b in self.message.content:
                if b.type == "text":
                    yield SimpleNamespace(type="text", text=b.text)
                elif b.type == "tool_use":
                    yield SimpleNamespace(type="content_block_start", content_block=b)
        return gen()

    async def get_final_message(self):
        return self.message


class FakeClient:
    """Round 1: think + call get_portfolio_overview. Round 2: answer citing tool numbers."""

    def __init__(self):
        self.requests = []
        outer = self

        class Messages:
            def stream(self, **kw):
                outer.requests.append(json.loads(json.dumps(kw["messages"], default=str)))
                last = kw["messages"][-1]
                if last["role"] == "user" and isinstance(last["content"], list) and last["content"][0].get("type") == "tool_result":
                    data = json.loads(last["content"][0]["content"])
                    text = f"Your portfolio is worth ₹{data['current_value']:,.0f} as of {data['as_of']}, with an XIRR of {data['xirr_pct']}%."
                    content = [Block(type="thinking", thinking="", signature="sig-2"), Block(type="text", text=text)]
                    return FakeStream(SimpleNamespace(stop_reason="end_turn", content=content, model="claude-fable-5-1", usage=None))
                content = [Block(type="thinking", thinking="", signature="sig-1"),
                           Block(type="tool_use", id=f"tu_{len(outer.requests)}", name="get_portfolio_overview", input={})]
                return FakeStream(SimpleNamespace(stop_reason="tool_use", content=content, model="claude-fable-5-1", usage=None))

        class Beta:
            messages = Messages()

        class Plain:
            async def create(self, **kw):
                return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"route":"in_scope","topic":"mf_portfolio","reason":"portfolio question"}')])

        self.beta = Beta()
        self.messages = Plain()


def _sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for chunk in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_api_and_copilot_turns(db, market, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(orchestrator, "get_client", lambda: fake)
    client = TestClient(app)

    r = client.post("/api/investors", json={"display_name": "Asha", "pan": "AAAAA1111A"})
    assert r.status_code == 200
    inv_id = r.json()["id"]
    assert r.json()["pan_masked"] == "AAAAA••••A"
    import_cas_data(db, db.get(Investor, inv_id), cas_dict(market, pan="AAAAA1111A"))

    q = client.get("/api/risk/questionnaire").json()
    answers = {qq["id"]: qq["options"][2 if len(qq["options"]) > 2 else 1]["id"] for qq in q["questions"]}
    rp = client.post(f"/api/investors/{inv_id}/risk-profile", json={"answers": answers})
    assert rp.status_code == 200 and rp.json()["bucket"]

    dash = client.get(f"/api/investors/{inv_id}/dashboard").json()
    assert dash["overview"]["holdings_count"] == 7 and dash["score"]["score"] is not None

    reco = client.post(f"/api/investors/{inv_id}/recommendations/run").json()
    assert reco["cards"]

    sid = client.post(f"/api/investors/{inv_id}/chat/sessions").json()["id"]
    events = _sse(client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "How is my portfolio doing?"}).text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "status" and kinds[-1] == "done"
    done = events[-1][1]
    assert done["text"].startswith("Your portfolio is worth ₹")
    assert "mf_market_risk" in done["disclosures"]

    # Second turn: the first turn's transcript must be replayed verbatim (append-only).
    _sse(client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "And now?"}).text)
    first_turn = fake.requests[1]  # full history at the end of turn 1 (after tool results)
    second_turn_start = fake.requests[2]
    assert second_turn_start[: len(first_turn)] == first_turn[: len(first_turn)]
    assert second_turn_start[len(first_turn)]["role"] == "assistant"  # turn-1 final answer, thinking block intact
    assert second_turn_start[len(first_turn)]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-2"}

    msgs = client.get(f"/api/chat/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
