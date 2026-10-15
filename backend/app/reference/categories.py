"""Canonical scheme categories.

AMFI's NAV files carry both the 2017 SEBI category labels ("Equity Scheme - Large Cap
Fund") and the 2026 relabelled ones ("Equity Schemes - Large Cap Fund", "Income/Debt
Oriented Schemes - Short Term Fund", ...). Everything downstream (allocation, tax,
riskometer defaults, suitability, peer groups) keys off the canonical `key` here.

Defaults marked "typical" (riskometer, exit load) are category-level approximations
used until per-scheme SID/KIM data is loaded; the UI and copilot label them as such.
"""

import re
from dataclasses import dataclass, field

RISK_LEVELS = ["low", "low_to_moderate", "moderate", "moderately_high", "high", "very_high"]
RISK_LABELS = {
    "low": "Low",
    "low_to_moderate": "Low to Moderate",
    "moderate": "Moderate",
    "moderately_high": "Moderately High",
    "high": "High",
    "very_high": "Very High",
}


def risk_rank(level: str) -> int:
    return RISK_LEVELS.index(level) + 1


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    group: str  # equity | hybrid | debt | commodity | international | solution | other
    # Look-through asset mix used for allocation (weights sum to 1).
    mix: dict[str, float] = field(default_factory=dict)
    riskometer: str = "very_high"
    tax_class: str = "equity"  # equity | debt | other  (see app.reference.tax)
    exit_load_pct: float = 0.0
    exit_load_days: int = 0
    lock_in_days: int = 0
    benchmark: str | None = None  # key into BENCHMARK_PROXIES
    peer_group: bool = True  # meaningful to rank schemes against each other


E = {"equity": 1.0}
D = {"debt": 1.0}

_CATALOG: list[Category] = [
    # Equity
    Category("large_cap", "Large Cap", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty50"),
    Category("large_mid_cap", "Large & Mid Cap", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="largemidcap250"),
    Category("mid_cap", "Mid Cap", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="midcap150"),
    Category("small_cap", "Small Cap", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="smallcap250"),
    Category("multi_cap", "Multi Cap", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500"),
    Category("flexi_cap", "Flexi Cap", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500"),
    Category("focused", "Focused", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500"),
    Category("value", "Value", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500"),
    Category("contra", "Contra", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500"),
    Category("dividend_yield", "Dividend Yield", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500"),
    Category("elss", "ELSS (Tax Saver)", "equity", E, lock_in_days=3 * 365, benchmark="nifty500"),
    Category("sectoral", "Sectoral", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500", peer_group=False),
    Category("thematic", "Thematic", "equity", E, exit_load_pct=1.0, exit_load_days=365, benchmark="nifty500", peer_group=False),
    Category("index_equity", "Equity Index Fund", "equity", E, peer_group=False),
    Category("etf_equity", "Equity ETF", "equity", E, peer_group=False),
    # International (taxed as non-equity; counted as equity in allocation)
    Category("intl_fof", "International FoF / ETF", "international", {"equity": 1.0}, tax_class="other", exit_load_pct=1.0, exit_load_days=365, peer_group=False),
    # Hybrid
    Category("aggressive_hybrid", "Aggressive Hybrid", "hybrid", {"equity": 0.75, "debt": 0.25}, exit_load_pct=1.0, exit_load_days=365),
    Category("balanced_hybrid", "Balanced Hybrid", "hybrid", {"equity": 0.5, "debt": 0.5}, riskometer="high", tax_class="other", exit_load_pct=1.0, exit_load_days=365),
    Category("conservative_hybrid", "Conservative Hybrid", "hybrid", {"equity": 0.2, "debt": 0.8}, riskometer="moderately_high", tax_class="debt", exit_load_pct=1.0, exit_load_days=365),
    Category("dynamic_asset_allocation", "Balanced Advantage / Dynamic Asset Allocation", "hybrid", {"equity": 0.5, "debt": 0.5}, exit_load_pct=1.0, exit_load_days=365),
    Category("multi_asset", "Multi Asset Allocation", "hybrid", {"equity": 0.55, "debt": 0.25, "gold": 0.20}, tax_class="other", exit_load_pct=1.0, exit_load_days=365),
    Category("arbitrage", "Arbitrage", "hybrid", D, riskometer="low", exit_load_pct=0.25, exit_load_days=30),
    Category("equity_savings", "Equity Savings", "hybrid", {"equity": 0.35, "debt": 0.65}, riskometer="moderately_high", exit_load_pct=1.0, exit_load_days=365),
    # Debt (gains on units bought from 1 Apr 2023 are slab-taxed: see app.reference.tax)
    Category("overnight", "Overnight", "debt", D, riskometer="low", tax_class="debt"),
    Category("liquid", "Liquid", "debt", D, riskometer="low_to_moderate", tax_class="debt", exit_load_pct=0.007, exit_load_days=7),
    Category("ultra_short", "Ultra Short Duration", "debt", D, riskometer="low_to_moderate", tax_class="debt"),
    Category("low_duration", "Low Duration", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("money_market", "Money Market", "debt", D, riskometer="low_to_moderate", tax_class="debt"),
    Category("short_duration", "Short Duration", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("medium_duration", "Medium Duration", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("medium_long_duration", "Medium to Long Duration", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("long_duration", "Long Duration", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("dynamic_bond", "Dynamic Bond", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("corporate_bond", "Corporate Bond", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("credit_risk", "Credit Risk", "debt", D, riskometer="high", tax_class="debt", exit_load_pct=1.0, exit_load_days=365),
    Category("banking_psu", "Banking & PSU", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("gilt", "Gilt", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("gilt_10y", "Gilt (10-year Constant Maturity)", "debt", D, riskometer="moderate", tax_class="debt"),
    Category("floater", "Floater", "debt", D, riskometer="low_to_moderate", tax_class="debt"),
    Category("index_debt", "Debt Index / Target Maturity", "debt", D, riskometer="low_to_moderate", tax_class="debt", peer_group=False),
    Category("etf_debt", "Debt ETF", "debt", D, riskometer="low_to_moderate", tax_class="debt", peer_group=False),
    Category("other_debt", "Other Debt", "debt", D, riskometer="moderate", tax_class="debt", peer_group=False),
    # Commodities
    Category("gold", "Gold", "commodity", {"gold": 1.0}, riskometer="high", tax_class="other"),
    Category("silver", "Silver", "commodity", {"gold": 1.0}, tax_class="other", peer_group=False),
    # Solution-oriented / life-cycle
    Category("retirement", "Retirement", "solution", {"equity": 0.7, "debt": 0.3}, lock_in_days=5 * 365, peer_group=False),
    Category("children", "Children's", "solution", {"equity": 0.7, "debt": 0.3}, lock_in_days=5 * 365, peer_group=False),
    Category("life_cycle", "Life Cycle", "solution", {"equity": 0.5, "debt": 0.5}, riskometer="high", tax_class="other", peer_group=False),
    Category("fof_domestic", "Domestic Fund of Funds", "other", {"equity": 0.5, "debt": 0.5}, riskometer="high", tax_class="other", peer_group=False),
    Category("other", "Other", "other", {"equity": 0.5, "debt": 0.5}, riskometer="high", tax_class="other", peer_group=False),
]

CATEGORIES: dict[str, Category] = {c.key: c for c in _CATALOG}

# Regular-plan Growth index funds used as yardsticks (AMFI codes, verified Oct 2026).
BENCHMARK_PROXIES: dict[str, dict] = {
    "nifty50": {"amfi_code": 100822, "label": "Nifty 50 (UTI Nifty 50 Index Fund)"},
    "nifty500": {"amfi_code": 147626, "label": "Nifty 500 (Motilal Oswal Nifty 500 Index Fund)"},
    "midcap150": {"amfi_code": 147621, "label": "Nifty Midcap 150 (Motilal Oswal Index Fund)"},
    "smallcap250": {"amfi_code": 147624, "label": "Nifty Smallcap 250 (Motilal Oswal Index Fund)"},
    "largemidcap250": {"amfi_code": 149341, "label": "Nifty LargeMidcap 250 (Edelweiss Index Fund)"},
}


def get_category(key: str | None) -> Category:
    return CATEGORIES.get(key or "other", CATEGORIES["other"])


# --------------------------------------------------------------------------- normalisation

_DEBT_NAME = re.compile(
    r"\b(gilt|g-?sec|sdl|bond|crisil|t-?bill|target maturity|debt|money market|liquid|corporate|psu|1d rate|overnight)\b",
    re.I,
)
_GOLD_NAME = re.compile(r"\bgold\b", re.I)
_SILVER_NAME = re.compile(r"\bsilver\b", re.I)

# Ordered: first match wins. Patterns run on the lower-cased raw category text.
_RULES: list[tuple[str, str]] = [
    (r"retirement", "retirement"),
    (r"child", "children"),
    (r"life cycle", "life_cycle"),
    (r"large ?(&|and) ?mid", "large_mid_cap"),
    (r"large cap", "large_cap"),
    (r"mid cap", "mid_cap"),
    (r"small cap", "small_cap"),
    (r"multi cap", "multi_cap"),
    (r"flexi cap", "flexi_cap"),
    (r"focused", "focused"),
    (r"contra", "contra"),
    (r"dividend yield", "dividend_yield"),
    (r"elss", "elss"),
    (r"aggressive hybrid", "aggressive_hybrid"),
    (r"balanced hybrid", "balanced_hybrid"),
    (r"conservative hybrid", "conservative_hybrid"),
    (r"balanced advantage|dynamic asset allocation", "dynamic_asset_allocation"),
    (r"multi asset", "multi_asset"),
    (r"arbitrage", "arbitrage"),
    (r"equity savings", "equity_savings"),
    (r"overnight", "overnight"),
    (r"liquid", "liquid"),
    (r"ultra short to short", "low_duration"),
    (r"ultra short", "ultra_short"),
    (r"low duration", "low_duration"),
    (r"money market", "money_market"),
    (r"short duration|short term fund", "short_duration"),
    (r"medium to long", "medium_long_duration"),
    (r"medium duration|medium term", "medium_duration"),
    (r"long duration|long term fund", "long_duration"),
    (r"dynamic bond|dynamic term", "dynamic_bond"),
    (r"corporate bond", "corporate_bond"),
    (r"credit risk", "credit_risk"),
    (r"banking and psu", "banking_psu"),
    (r"constant maturity", "gilt_10y"),
    (r"gilt", "gilt"),
    (r"floater|floating", "floater"),
    (r"gold etf", "gold"),
    (r"silver etf", "silver"),
    (r"overseas|international", "intl_fof"),
    (r"debt etf", "etf_debt"),
    (r"equity etf", "etf_equity"),
    (r"hybrid etf|index funds - hybrid", "balanced_hybrid"),
    (r"index funds - debt", "index_debt"),
    (r"index funds - equity", "index_equity"),
    (r"equity schemes? - value", "value"),
    (r"sectoral/ ?thematic|thematic", "thematic"),
    (r"equity schemes? - sectoral", "sectoral"),
    (r"fixed term|interval|income|debt", "other_debt"),
]


def normalise_category(raw_category: str, scheme_name: str = "") -> str:
    """Map an AMFI category header (+ scheme name for mixed buckets) to a canonical key."""
    raw = raw_category.lower()
    name = scheme_name or ""

    # Mixed buckets where the scheme name decides.
    if re.search(r"index funds\)?$|other scheme - index funds", raw):
        return "index_debt" if _DEBT_NAME.search(name) else "index_equity"
    if re.search(r"other\s+etfs?|other etf", raw):
        if _GOLD_NAME.search(name):
            return "gold"
        if _SILVER_NAME.search(name):
            return "silver"
        return "etf_debt" if _DEBT_NAME.search(name) else "etf_equity"
    if "fof domestic" in raw or ("fund of funds" in raw and "overseas" not in raw):
        if _GOLD_NAME.search(name):
            return "gold"
        if _SILVER_NAME.search(name):
            return "silver"
        if re.search(r"multi.?asset|asset allocat", name, re.I):
            return "multi_asset"
        if _DEBT_NAME.search(name):
            return "other_debt"
        return "fof_domestic"
    if "gold" in raw and "etf" in raw:
        return "gold"

    for pattern, key in _RULES:
        if re.search(pattern, raw):
            return key
    return "other"


def normalise_plan(raw_plan: str, scheme_name: str) -> str:
    text = f"{raw_plan} {scheme_name}".lower()
    return "direct" if "direct" in text else "regular"


def normalise_option(raw_option: str, scheme_name: str) -> str:
    text = (raw_option or "").lower()
    if not text.strip():
        text = scheme_name.lower()
    if re.search(r"idcw|dividend|payout|reinvest|income distribution", text):
        return "idcw"
    if re.search(r"growth|cumulative", text):
        return "growth"
    if "bonus" in text:
        return "bonus"
    return "growth" if "growth" in scheme_name.lower() else "other"


EXCLUDED_NAME = re.compile(r"unclaimed|investor education|segregated|interval", re.I)
