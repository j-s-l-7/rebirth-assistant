"""Capital-gains rules for mutual fund units (India), as effective-dated configuration.

Budgets change these, so every rule carries the date it applies from (PRD §11.4).
Outputs are *estimates* ("not tax advice"): surcharge, set-off of losses, STT and
indexation on pre-2024 debt sales are not modelled.

Tax classes come from the scheme category (app.reference.categories):
  equity - equity-oriented funds (>=65% domestic equity, incl. arbitrage/BAF/equity savings)
  debt   - "specified mutual funds" under s.50AA (>65% debt & money market)
  other  - everything else: gold/silver, international FoFs, 35-65% equity hybrids
"""

from dataclasses import dataclass
from datetime import date, timedelta

CESS = 0.04  # health & education cess on tax


@dataclass(frozen=True)
class EquityRule:
    effective_from: date
    stcg_rate: float
    ltcg_rate: float
    ltcg_exemption: float  # per financial year, across all equity LTCG
    long_term_after_months: int


EQUITY_RULES = [
    EquityRule(date(2018, 4, 1), stcg_rate=15.0, ltcg_rate=10.0, ltcg_exemption=100_000, long_term_after_months=12),
    EquityRule(date(2024, 7, 23), stcg_rate=20.0, ltcg_rate=12.5, ltcg_exemption=125_000, long_term_after_months=12),
]

# s.50AA: units of debt funds acquired on/after this date are taxed at slab rates
# regardless of holding period.
SPECIFIED_MF_FROM = date(2023, 4, 1)
# From FY 2025-26 the s.50AA definition narrowed to funds with >65% in debt, so gold and
# international funds bought after Apr 2023 regained long-term treatment.
NARROWED_50AA_FROM = date(2025, 4, 1)
NON_EQUITY_LTCG_RATE = 12.5
NON_EQUITY_LT_MONTHS_UNLISTED = 24
NON_EQUITY_LT_MONTHS_LISTED = 12
GRANDFATHER_CUTOFF = date(2018, 1, 31)  # equity units bought on/before this use FMV of 31-Jan-2018


def equity_rule_for(sale_date: date) -> EquityRule:
    rule = EQUITY_RULES[0]
    for r in EQUITY_RULES:
        if sale_date >= r.effective_from:
            rule = r
    return rule


def financial_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY{start}-{str(start + 1)[-2:]}"


def fy_bounds(d: date) -> tuple[date, date]:
    start = d.year if d.month >= 4 else d.year - 1
    return date(start, 4, 1), date(start + 1, 3, 31)


@dataclass
class GainClass:
    term: str  # STCG | LTCG | SLAB
    rate_pct: float | None  # None => slab rate applies
    long_term_on: date | None  # date the lot turns long-term (None if never)
    note: str


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def long_term_from(buy_date: date, months: int) -> date:
    """First sale date that counts as long-term: held for *more than* `months` months."""
    return add_months(buy_date, months) + timedelta(days=1)


def classify_gain(tax_class: str, buy_date: date, sale_date: date, listed: bool = False) -> GainClass:
    """Decide how a lot bought on `buy_date` and sold on `sale_date` is taxed."""
    if tax_class == "equity":
        rule = equity_rule_for(sale_date)
        lt_on = long_term_from(buy_date, rule.long_term_after_months)
        if sale_date >= lt_on:
            return GainClass("LTCG", rule.ltcg_rate, lt_on, f"Equity held over {rule.long_term_after_months} months")
        return GainClass("STCG", rule.stcg_rate, lt_on, f"Equity held {rule.long_term_after_months} months or less")

    if tax_class == "debt":
        if buy_date >= SPECIFIED_MF_FROM:
            return GainClass("SLAB", None, None, "Debt fund units bought on/after 1 Apr 2023 are taxed at your slab rate")
        lt_on = long_term_from(buy_date, NON_EQUITY_LT_MONTHS_UNLISTED)
        if sale_date >= lt_on:
            return GainClass("LTCG", NON_EQUITY_LTCG_RATE, lt_on, "Pre-Apr-2023 debt units held over 24 months")
        return GainClass("SLAB", None, lt_on, "Debt units held 24 months or less are taxed at slab")

    # other: gold / silver / international / mid-equity hybrids
    lt_months = NON_EQUITY_LT_MONTHS_LISTED if listed else NON_EQUITY_LT_MONTHS_UNLISTED
    lt_on = long_term_from(buy_date, lt_months)
    if buy_date >= SPECIFIED_MF_FROM and sale_date < NARROWED_50AA_FROM:
        return GainClass("SLAB", None, None, "Sold before FY2025-26 while treated as a specified MF")
    if sale_date >= lt_on:
        return GainClass("LTCG", NON_EQUITY_LTCG_RATE, lt_on, f"Held over {lt_months} months")
    return GainClass("SLAB", None, lt_on, f"Held {lt_months} months or less: taxed at slab rate")


def effective_rate(gc: GainClass, slab_pct: float) -> float:
    base = gc.rate_pct if gc.rate_pct is not None else slab_pct
    return base * (1 + CESS)
