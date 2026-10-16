"""Deterministic tools the copilot calls (PRD AI-2: the LLM explains, the engines decide).

Every number in an answer comes from these tools. Inputs are validated with pydantic
before running (tool inputs stream eagerly and are not server-validated).
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.analytics.fund_lens import compare_funds, fund_lens, resolve_funds
from app.docs.search import search_documents
from app.models import Escalation, Investor, Scheme


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FindInput(_In):
    query: str = Field(min_length=2, max_length=120)


class FundInput(_In):
    fund: str = Field(min_length=2, max_length=120, description="Fund name (e.g. 'Parag Parikh Flexi Cap') or AMFI scheme code")


class DocSearchInput(_In):
    query: str = Field(min_length=3, max_length=200)


class CompareInput(_In):
    funds: list[str] = Field(min_length=2, max_length=4, description="2-4 fund names or AMFI scheme codes")


class ExpertInput(_In):
    reason: str = Field(min_length=3, max_length=1000)
    category: Literal["complaint", "wants_human", "suitability_doubt", "distress", "other"] = "other"


def _schema(model: type[BaseModel]) -> dict:
    s = model.model_json_schema()
    s.pop("title", None)
    for p in s.get("properties", {}).values():
        p.pop("title", None)
    s["additionalProperties"] = False
    return s


TOOL_SPECS: list[tuple[str, str, type[BaseModel], str]] = [
    ("fund_lens", "Everything about one mutual fund relative to the other funds in its SEBI category: its rank among category peers, each metric (1/3/5-year returns, consistency, Sortino, volatility, worst fall, downside capture, expense ratio, index-likeness) beside the category median and quartiles with a plain-language standing, a D-profile (performance, consistency, risk, diversification, asset mix, cost), facts (launch date, minimum investment where loaded) and the category's top funds. Accepts a fund name or AMFI code; returns the matched fund and close alternatives.", FundInput, "Comparing the fund with its category"),
    ("compare_funds", "Side-by-side comparison of 2-4 mutual funds (names or AMFI codes): each fund's category rank and key metrics with the category median and standing. Says whether they share a category (ranks across categories aren't comparable).", CompareInput, "Comparing the funds"),
    ("search_documents", "Search the full text of stored official documents (AMFI Code of Ethics, AMFI master circular and FAQs for distributors, SEBI FAQs, AMFI monthly industry reports). Returns passages with title, page numbers, date and URL. Use for questions about rules, disclosures or industry data; quote briefly and cite title and page.", DocSearchInput, "Searching official documents"),
    ("find_funds", "Search mutual fund schemes by name. Returns matching schemes with AMFI code, plan, option and category; Regular-Growth plans in the peer set first.", FindInput, "Finding the fund"),
    ("request_expert", "Hand the conversation to a human expert (EUIN holder). Use when the person asks for a human, complains, sounds distressed, or suitability is unclear. Free of charge; reply within 1 business day.", ExpertInput, "Bringing in a human expert"),
]

TOOL_LABELS = {name: label for name, _, _, label in TOOL_SPECS}
_INPUTS = {name: model for name, _, model, _ in TOOL_SPECS}


def tool_definitions() -> list[dict]:
    """Deterministic order and content: the tool list is part of the cached, thinking-bound prefix."""
    return [{"name": name, "description": desc, "input_schema": _schema(model), "eager_input_streaming": True} for name, desc, model, _ in TOOL_SPECS]


class ToolContext:
    """Per-turn state: one DB session and the chat's investor (owner of escalations)."""

    def __init__(self, db: Session, investor: Investor, session_id: str | None = None):
        self.db, self.investor, self.session_id = db, investor, session_id
        self.escalation_id: str | None = None

    def run(self, name: str, raw_input) -> tuple[dict, bool]:
        model = _INPUTS.get(name)
        if model is None:
            return {"error": f"Unknown tool {name}"}, False
        try:
            args = model.model_validate(raw_input if isinstance(raw_input, dict) else {})
        except ValidationError as exc:
            return {"error": "invalid_input", "details": json.loads(exc.json(include_url=False))}, False
        try:
            return getattr(self, f"_t_{name}")(args), True
        except LookupError as exc:
            return {"error": str(exc)}, False

    # ------------------------------------------------------------------ tools
    def _resolve(self, fund: str) -> tuple[int, list[dict]]:
        """A name or AMFI code -> (code, close alternatives)."""
        text = fund.strip()
        if text.isdigit() and self.db.get(Scheme, int(text)):
            return int(text), []
        matches = resolve_funds(self.db, text)
        if not matches:
            raise LookupError(f"No mutual fund matches '{fund}'. Try find_funds with a shorter name.")
        return matches[0]["amfi_code"], [m for m in matches[1:4] if m["name"] != matches[0]["name"]]

    def _t_search_documents(self, a: DocSearchInput) -> dict:
        results = search_documents(self.db, a.query, limit=5)
        return {"results": results} if results else {"results": [], "message": "No stored document matches; answer from general knowledge only if it's not a factual claim, or say you don't have the source."}

    def _t_find_funds(self, a: FindInput) -> dict:
        return {"matches": resolve_funds(self.db, a.query, limit=8)}

    def _t_fund_lens(self, a: FundInput) -> dict:
        code, alternatives = self._resolve(a.fund)
        lens = fund_lens(self.db, code)
        lens["matched_query"] = a.fund
        lens["other_matches"] = alternatives
        return lens

    def _t_compare_funds(self, a: CompareInput) -> dict:
        return compare_funds(self.db, [self._resolve(f)[0] for f in a.funds])

    def _t_request_expert(self, a: ExpertInput) -> dict:
        esc = Escalation(investor_id=self.investor.id, session_id=self.session_id, category=a.category, reason=a.reason)
        self.db.add(esc)
        self.db.commit()
        self.escalation_id = esc.id
        return {"ticket_id": esc.id, "status": "open", "reply_sla": "1 business day", "cost": "free"}
