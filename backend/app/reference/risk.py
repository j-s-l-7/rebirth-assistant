"""Risk profiling, allocation templates and the suitability matrix (PRD §9.1 ONB-4, §11.3, REC-6).

v1 defaults for Jayesh's sign-off (task J-07). Scoring separates *capacity* (can you
afford a fall?) from *willingness* (will you stay invested through one?) and takes the
more cautious of the two, so a strong appetite can't outrun a thin safety net.
"""

from dataclasses import dataclass

QUESTIONNAIRE_VERSION = "rq-v1"

BUCKETS = ["conservative", "moderately_conservative", "moderate", "moderately_aggressive", "aggressive"]
BUCKET_LABELS = {
    "conservative": "Conservative",
    "moderately_conservative": "Moderately Conservative",
    "moderate": "Moderate",
    "moderately_aggressive": "Moderately Aggressive",
    "aggressive": "Aggressive",
}

QUESTIONS: list[dict] = [
    {
        "id": "age",
        "dimension": "capacity",
        "text": "How old are you?",
        "options": [("under_30", "Under 30", 5), ("30_40", "30-40", 4), ("40_50", "40-50", 3), ("50_60", "50-60", 2), ("over_60", "Over 60", 1)],
    },
    {
        "id": "horizon",
        "dimension": "capacity",
        "text": "When will you need most of this money?",
        "options": [("lt1", "Within 1 year", 1), ("1_3", "In 1-3 years", 2), ("3_5", "In 3-5 years", 3), ("5_10", "In 5-10 years", 4), ("gt10", "After 10+ years", 5)],
    },
    {
        "id": "income",
        "dimension": "capacity",
        "text": "How steady is your income?",
        "options": [("uncertain", "Uncertain or irregular", 1), ("somewhat", "Mostly steady", 3), ("very", "Very steady", 5)],
    },
    {
        "id": "emergency",
        "dimension": "capacity",
        "text": "How many months of expenses do you keep aside for emergencies?",
        "options": [("none", "None yet", 1), ("lt3", "Less than 3 months", 2), ("3_6", "3-6 months", 4), ("gt6", "More than 6 months", 5)],
    },
    {
        "id": "dependents",
        "dimension": "capacity",
        "text": "How many people depend on your income?",
        "options": [("3plus", "3 or more", 1), ("1_2", "1-2", 3), ("none", "None", 5)],
    },
    {
        "id": "drawdown",
        "dimension": "willingness",
        "text": "Your investments fall 20% in a few months. What do you do?",
        "options": [("sell_all", "Sell everything", 1), ("sell_some", "Sell some", 2), ("hold", "Hold and wait", 3), ("buy_some", "Invest a little more", 4), ("buy_more", "Invest a lot more", 5)],
    },
    {
        "id": "goal",
        "dimension": "willingness",
        "text": "What matters most for this money?",
        "options": [("protect", "Protecting what I have", 1), ("income", "Regular income", 2), ("balanced", "Balanced growth", 3), ("growth", "Long-term growth", 4), ("max_growth", "Maximum growth, I can take swings", 5)],
    },
    {
        "id": "experience",
        "dimension": "willingness",
        "text": "How long have you invested in equity funds or stocks?",
        "options": [("none", "Never", 1), ("lt2", "Less than 2 years", 2), ("2_5", "2-5 years", 3), ("gt5", "5+ years, through at least one market fall", 5)],
    },
]

HORIZON_YEARS = {"lt1": 1, "1_3": 2, "3_5": 4, "5_10": 7, "gt10": 12}


def _bucket_from_score(score: float) -> str:
    if score < 30:
        return "conservative"
    if score < 45:
        return "moderately_conservative"
    if score < 62:
        return "moderate"
    if score < 78:
        return "moderately_aggressive"
    return "aggressive"


@dataclass
class RiskResult:
    score: int
    bucket: str
    capacity_score: int
    willingness_score: int
    horizon_years: int
    reasons: list[str]


def score_answers(answers: dict[str, str]) -> RiskResult:
    by_dim: dict[str, list[tuple[int, int]]] = {"capacity": [], "willingness": []}
    for q in QUESTIONS:
        choice = answers.get(q["id"])
        opts = {o[0]: o[2] for o in q["options"]}
        if choice not in opts:
            raise ValueError(f"Missing or invalid answer for '{q['id']}'")
        by_dim[q["dimension"]].append((opts[choice], 5))

    def pct(items):
        got = sum(v for v, _ in items)
        lo = len(items)
        hi = sum(m for _, m in items)
        return round(100 * (got - lo) / (hi - lo))

    capacity, willingness = pct(by_dim["capacity"]), pct(by_dim["willingness"])
    cap_b, will_b = _bucket_from_score(capacity), _bucket_from_score(willingness)
    bucket = BUCKETS[min(BUCKETS.index(cap_b), BUCKETS.index(will_b))]
    reasons = [
        f"Capacity to take risk: {BUCKET_LABELS[cap_b]} ({capacity}/100)",
        f"Willingness to take risk: {BUCKET_LABELS[will_b]} ({willingness}/100)",
        "We use the more cautious of the two.",
    ]

    horizon = HORIZON_YEARS[answers["horizon"]]
    cap_for_horizon = "conservative" if horizon <= 1 else "moderately_conservative" if horizon <= 3 else None
    if cap_for_horizon and BUCKETS.index(bucket) > BUCKETS.index(cap_for_horizon):
        bucket = cap_for_horizon
        reasons.append(f"Capped at {BUCKET_LABELS[bucket]} because the money is needed within {horizon} year(s).")
    if answers["drawdown"] == "sell_all" and BUCKETS.index(bucket) > BUCKETS.index("moderate"):
        bucket = "moderate"
        reasons.append("Capped at Moderate because you'd sell everything in a 20% fall.")

    return RiskResult(round((capacity + willingness) / 2), bucket, capacity, willingness, horizon, reasons)


# --------------------------------------------------------------------------- allocation templates

@dataclass(frozen=True)
class Template:
    target: dict[str, float]  # % of portfolio by asset class
    bands: dict[str, tuple[float, float]]
    equity_mix: dict[str, float]  # % of the equity sleeve by category
    debt_mix: dict[str, float]
    gold_mix: dict[str, float]


TEMPLATES: dict[str, Template] = {
    "conservative": Template(
        {"equity": 20, "debt": 75, "gold": 5},
        {"equity": (10, 25), "debt": (65, 90), "gold": (0, 10)},
        {"large_cap": 100},
        {"corporate_bond": 35, "short_duration": 35, "money_market": 30},
        {"gold": 100},
    ),
    "moderately_conservative": Template(
        {"equity": 35, "debt": 57, "gold": 8},
        {"equity": (25, 40), "debt": (50, 70), "gold": (5, 12)},
        {"large_cap": 60, "flexi_cap": 40},
        {"corporate_bond": 40, "short_duration": 35, "money_market": 25},
        {"gold": 100},
    ),
    "moderate": Template(
        {"equity": 50, "debt": 40, "gold": 10},
        {"equity": (40, 60), "debt": (30, 50), "gold": (5, 15)},
        {"flexi_cap": 45, "large_cap": 35, "large_mid_cap": 20},
        {"corporate_bond": 40, "short_duration": 40, "money_market": 20},
        {"gold": 100},
    ),
    "moderately_aggressive": Template(
        {"equity": 70, "debt": 20, "gold": 10},
        {"equity": (60, 80), "debt": (10, 30), "gold": (5, 15)},
        {"flexi_cap": 40, "large_cap": 25, "mid_cap": 20, "large_mid_cap": 15},
        {"corporate_bond": 50, "short_duration": 30, "money_market": 20},
        {"gold": 100},
    ),
    "aggressive": Template(
        {"equity": 85, "debt": 8, "gold": 7},
        {"equity": (75, 95), "debt": (0, 20), "gold": (0, 10)},
        {"flexi_cap": 35, "mid_cap": 25, "small_cap": 20, "large_cap": 20},
        {"corporate_bond": 50, "money_market": 50},
        {"gold": 100},
    ),
}

SMALL_CAP_MIN_HORIZON_YEARS = 7

# Category-based suitability: which canonical categories each bucket may be recommended.
# Cumulative - each bucket inherits the one below it.
_ALLOWED_INCREMENTS: dict[str, set[str]] = {
    "conservative": {
        "overnight", "liquid", "money_market", "ultra_short", "low_duration", "short_duration",
        "corporate_bond", "banking_psu", "gilt", "gilt_10y", "floater", "index_debt", "etf_debt",
        "arbitrage", "conservative_hybrid", "equity_savings", "large_cap", "index_equity", "gold",
    },
    "moderately_conservative": {
        "dynamic_asset_allocation", "multi_asset", "balanced_hybrid", "dynamic_bond",
        "medium_duration", "flexi_cap", "etf_equity", "elss",
    },
    "moderate": {
        "large_mid_cap", "multi_cap", "focused", "value", "contra", "dividend_yield",
        "aggressive_hybrid", "intl_fof", "medium_long_duration", "long_duration", "silver",
        "fof_domestic", "retirement", "children", "life_cycle",
    },
    "moderately_aggressive": {"mid_cap", "credit_risk"},
    "aggressive": {"small_cap", "sectoral", "thematic"},
}


def allowed_categories(bucket: str) -> set[str]:
    allowed: set[str] = set()
    for b in BUCKETS[: BUCKETS.index(bucket) + 1]:
        allowed |= _ALLOWED_INCREMENTS[b]
    return allowed


def suitability(bucket: str, category_key: str, horizon_years: int | None = None) -> dict:
    allowed = category_key in allowed_categories(bucket)
    reason = None
    if not allowed:
        reason = f"{category_key.replace('_', ' ').title()} funds sit above a {BUCKET_LABELS[bucket]} risk profile"
    elif category_key == "small_cap" and (horizon_years or 0) < SMALL_CAP_MIN_HORIZON_YEARS:
        allowed = False
        reason = f"Small-cap funds need a horizon of {SMALL_CAP_MIN_HORIZON_YEARS}+ years"
    return {"pass": allowed, "investor_bucket": bucket, "reason": reason}
