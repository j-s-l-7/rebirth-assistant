"""Deterministic tools the copilot calls (PRD AI-2: the LLM explains, the engines decide).

Tools are bound to the signed-in investor by the orchestrator - the model never passes
an investor id, so it can't reach anyone else's data. Inputs are validated with pydantic
before running (tool inputs stream eagerly and are not server-validated).
"""

import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.analytics.fund_lens import compare_funds, fund_lens, resolve_funds
from app.analytics.lots import redemption_estimate
from app.config import get_settings
from app.docs.search import search_documents
from app.insights.goals import goal_plan
from app.insights.nudges import nudges
from app.insights.tax import tax_summary
from app.models import Escalation, Investor, Scheme
from app.reco.engine import run_recommendations
from app.reference.categories import get_category
from app.reference.risk import BUCKET_LABELS, TEMPLATES, allowed_categories
from app.services.analysis import Analysis, analyse


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(_In):
    pass


class HoldingsInput(_In):
    sort_by: Literal["value", "gain", "xirr"] = "value"


class FindInput(_In):
    query: str = Field(min_length=2, max_length=120)


class FundInput(_In):
    fund: str = Field(min_length=2, max_length=120, description="Fund name (e.g. 'Parag Parikh Flexi Cap') or AMFI scheme code")


class DocSearchInput(_In):
    query: str = Field(min_length=3, max_length=200)


class CompareInput(_In):
    funds: list[str] = Field(min_length=2, max_length=4, description="2-4 fund names or AMFI scheme codes")


class RedemptionInput(_In):
    amfi_code: int
    amount_inr: float | None = Field(default=None, gt=0)
    units: float | None = Field(default=None, gt=0)
    all_units: bool = False


class GoalInput(_In):
    target_amount_inr: float = Field(gt=0)
    target_date: date
    existing_corpus_inr: float = Field(default=0, ge=0)
    monthly_sip_inr: float = Field(default=0, ge=0)
    adjust_for_inflation: bool = True


class ExpertInput(_In):
    reason: str = Field(min_length=3, max_length=1000)
    category: Literal["complaint", "wants_human", "large_redemption", "suitability_doubt", "distress", "other"] = "other"


def _schema(model: type[BaseModel]) -> dict:
    s = model.model_json_schema()
    s.pop("title", None)
    for p in s.get("properties", {}).values():
        p.pop("title", None)
    s["additionalProperties"] = False
    return s


TOOL_SPECS: list[tuple[str, str, type[BaseModel], str]] = [
    ("fund_lens", "Everything about one mutual fund relative to the other funds in its SEBI category: its rank among category peers, each metric (1/3/5-year returns, consistency, Sortino, volatility, worst fall, downside capture, expense ratio, index-likeness) beside the category median and quartiles with a plain-language standing, a D-profile (performance, consistency, risk, diversification, asset mix, cost), facts (AUM, launch date, minimum investment where loaded) and the category's top funds. Accepts a fund name or AMFI code; returns the matched fund and close alternatives.", FundInput, "Comparing the fund with its category"),
    ("compare_funds", "Side-by-side comparison of 2-4 mutual funds (names or AMFI codes): each fund's category rank and key metrics with the category median and standing. Says whether they share a category (ranks across categories aren't comparable).", CompareInput, "Comparing the funds"),
    ("search_documents", "Search the full text of stored official documents (AMFI Code of Ethics, AMFI master circular and FAQs for distributors, SEBI FAQs, AMFI monthly industry reports). Returns passages with title, page numbers, date and URL. Use for questions about rules, disclosures or industry data; quote briefly and cite title and page.", DocSearchInput, "Searching official documents"),
    ("find_funds", "Search mutual fund schemes by name. Returns matching schemes with AMFI code, plan, option and category; Regular-Growth plans in the peer set first.", FindInput, "Finding the fund"),
    ("get_portfolio_overview", "Portfolio totals: current value, invested, gain, XIRR, day change, look-through asset allocation (equity/debt/gold), category/AMC/plan split, active SIPs, gains realised this financial year, and the NAV date the values use.", NoInput, "Reading your portfolio"),
    ("get_holdings", "Every fund the investor holds with value, invested amount, gain, XIRR, weight, category, riskometer, SIP status, peer-ranking tier, performance vs its benchmark on the investor's own cashflows, typical exit load, lock-in and suitability for their risk profile.", HoldingsInput, "Going through your funds"),
    ("get_ra_score", "The RA Score (0-100) with its six dimensions (discipline, balance, risk fit, diversification, performance, efficiency): each with score, one-line summary, inputs and what would raise it.", NoInput, "Checking your RA Score"),
    ("get_tax_summary", "Capital gains for the current financial year: realised short/long-term gains, the Rs 1.25 lakh equity LTCG exemption used and remaining, unrealised gains split by tax treatment, the LTCG harvesting opportunity, and lots turning long-term in the next 60 days. Estimates, not tax advice.", NoInput, "Working through your tax position"),
    ("estimate_redemption", "Simulate redeeming part or all of one holding today (FIFO): value, gain split by short/long-term, estimated tax, exit load, locked units and the date pending units turn long-term. Give amount_inr, units, or all_units=true.", RedemptionInput, "Estimating the redemption"),
    ("run_recommendations", "Run the recommendation engine on the investor's current portfolio and risk profile. Returns suitability-checked action cards (switch, stop/redirect SIP, start SIP, STP, review) with rationale, tax/exit-load estimates and priority. The only source of scheme suggestions.", NoInput, "Running the recommendation engine"),
    ("get_alerts", "Proactive alerts: lots turning long-term soon, exit-load windows ending, lock-ins ending, missed SIPs, laggards, idle cash, drift, LTCG harvesting window, risk profile due.", NoInput, "Checking alerts"),
    ("get_risk_profile", "The investor's risk profile: bucket, horizon, capacity and willingness scores, target allocation bands and which fund categories suit them.", NoInput, "Reading your risk profile"),
    ("goal_projection", "Goal-based mutual fund SIP maths: monthly SIP needed and projected corpus under cautious/base/optimistic return assumptions, using the investor's risk-profile mix. Outputs are illustrative assumptions, not predictions.", GoalInput, "Projecting your goal"),
    ("get_overlap", "Stock overlap between the investor's equity funds (from AMC portfolio disclosures when available, otherwise same-category duplicates) and sector exposure.", NoInput, "Checking fund overlap"),
    ("request_expert", "Hand the conversation to a human expert (EUIN holder). Use when the investor asks for a person, complains, sounds distressed, wants a large redemption, or suitability is unclear. Free of charge; reply within 1 business day.", ExpertInput, "Bringing in a human expert"),
]

TOOL_LABELS = {name: label for name, _, _, label in TOOL_SPECS}
_INPUTS = {name: model for name, _, model, _ in TOOL_SPECS}


def tool_definitions() -> list[dict]:
    """Deterministic order and content: the tool list is part of the cached, thinking-bound prefix."""
    return [{"name": name, "description": desc, "input_schema": _schema(model), "eager_input_streaming": True} for name, desc, model, _ in TOOL_SPECS]


class ToolContext:
    """Per-turn state: one DB session, the bound investor and a lazily built Analysis."""

    def __init__(self, db: Session, investor: Investor, session_id: str | None = None):
        self.db, self.investor, self.session_id = db, investor, session_id
        self._analysis: Analysis | None = None
        self.cards: list[dict] = []
        self.reco_run_id: str | None = None
        self.escalation_id: str | None = None

    @property
    def analysis(self) -> Analysis:
        if self._analysis is None:
            self._analysis = analyse(self.db, self.investor)
        return self._analysis

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
    def _no_holdings(self) -> dict | None:
        if not self.analysis.pv.holdings:
            return {"holdings": 0, "message": "No holdings imported yet. The investor can import a CAS statement (or load the demo portfolio) from the Import screen."}
        return None

    def _t_get_portfolio_overview(self, _: NoInput) -> dict:
        return self._no_holdings() or self.analysis.pv.overview()

    def _t_get_holdings(self, a: HoldingsInput) -> dict:
        if empty := self._no_holdings():
            return empty
        hs = [h.summary() for h in self.analysis.pv.holdings]
        key = {"value": "value", "gain": "gain", "xirr": "xirr_pct"}[a.sort_by]
        hs.sort(key=lambda h: -(h.get(key) or float("-inf")))
        for h in hs:
            h.pop("folios", None)
        return {"as_of": self.analysis.as_of.isoformat(), "holdings": hs}

    def _t_get_ra_score(self, _: NoInput) -> dict:
        return self._no_holdings() or self.analysis.score

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
        lens["held_by_investor"] = code in {h.code for h in self.analysis.pv.holdings}
        return lens

    def _t_compare_funds(self, a: CompareInput) -> dict:
        return compare_funds(self.db, [self._resolve(f)[0] for f in a.funds])

    def _t_get_tax_summary(self, _: NoInput) -> dict:
        return self._no_holdings() or tax_summary(self.analysis)

    def _t_estimate_redemption(self, a: RedemptionInput) -> dict:
        h = next((h for h in self.analysis.pv.holdings if h.code == a.amfi_code), None)
        if h is None:
            raise LookupError("The investor doesn't hold that scheme")
        if a.all_units:
            units = h.units
        elif a.units:
            units = min(a.units, h.units)
        elif a.amount_inr:
            units = min(a.amount_inr / h.nav, h.units)
        else:
            return {"error": "Give amount_inr, units or all_units"}
        slab = self.investor.tax_slab_pct or get_settings().default_tax_slab_pct
        est = redemption_estimate(h.lots, h.scheme.category_key, h.nav, self.analysis.as_of, slab, units)
        est.update({"scheme": h.scheme.name, "nav": h.nav, "nav_date": h.nav_date.isoformat() if h.nav_date else None,
                    "share_of_portfolio_pct": round(est["value"] / self.analysis.pv.value * 100, 2) if self.analysis.pv.value else None,
                    "ltcg_exemption_remaining_this_fy": tax_summary(self.analysis)["equity_ltcg_exemption"]["remaining"],
                    "label": "Estimate, not tax advice"})
        est["lots"] = est["lots"][:12]
        return est

    def _t_run_recommendations(self, _: NoInput) -> dict:
        if empty := self._no_holdings():
            return empty
        run = run_recommendations(self.db, self.analysis, trigger="chat")
        self.reco_run_id = run.id
        self.cards = [i.card for i in sorted(run.items, key=lambda i: i.priority)]
        return {"run_id": run.id, "model_version": run.model_version, "cards": self.cards,
                "note": "These cards are shown to the investor under your reply."}

    def _t_get_alerts(self, _: NoInput) -> dict:
        return {"alerts": nudges(self.analysis)}

    def _t_get_risk_profile(self, _: NoInput) -> dict:
        r = self.analysis.risk
        if r is None:
            return {"risk_profile": None, "message": "No risk profile yet. The investor can take the 2-minute quiz from the Profile screen."}
        t = TEMPLATES[r.bucket]
        return {
            "bucket": BUCKET_LABELS[r.bucket], "score": r.score, "horizon_years": r.horizon_years,
            "taken_on": r.created_at.date().isoformat(), "valid_till": r.valid_till.isoformat(),
            "target_allocation_pct": t.target, "bands_pct": {k: list(v) for k, v in t.bands.items()},
            "suitable_categories": sorted(get_category(c).label for c in allowed_categories(r.bucket)),
        }

    def _t_goal_projection(self, a: GoalInput) -> dict:
        r = self.analysis.risk
        bucket = r.bucket if r else "moderate"
        out = goal_plan(a.target_amount_inr, a.target_date, bucket, a.existing_corpus_inr, a.monthly_sip_inr,
                        inflation=0.06 if a.adjust_for_inflation else None, today=date.today())
        out["risk_bucket_used"] = BUCKET_LABELS[bucket] + ("" if r else " (default; no risk profile yet)")
        return out

    def _t_get_overlap(self, _: NoInput) -> dict:
        return self._no_holdings() or self.analysis.overlap

    def _t_request_expert(self, a: ExpertInput) -> dict:
        esc = Escalation(investor_id=self.investor.id, session_id=self.session_id, category=a.category, reason=a.reason)
        self.db.add(esc)
        self.db.commit()
        self.escalation_id = esc.id
        return {"ticket_id": esc.id, "status": "open", "reply_sla": "1 business day", "cost": "free"}
