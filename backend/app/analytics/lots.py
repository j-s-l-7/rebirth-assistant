"""FIFO tax lots from transaction history (PRD PF-6).

Inflows (purchases, SIPs, switch-ins, reinvested dividends, segregated units) open lots;
outflows (redemptions, switch-outs) consume the oldest lots first, which is how capital
gains are computed for mutual fund units in India.
"""

from dataclasses import dataclass, field
from datetime import date

from app.reference.categories import get_category
from app.reference.tax import GRANDFATHER_CUTOFF, classify_gain, effective_rate

IGNORED_TYPES = {"STT_TAX", "STAMP_DUTY_TAX", "TDS_TAX", "DIVIDEND_PAYOUT", "MISC", "UNKNOWN"}
EPS = 1e-6


@dataclass
class Lot:
    buy_date: date
    units: float
    cost: float  # rupees for the remaining units
    txn_type: str

    @property
    def cost_per_unit(self) -> float:
        return self.cost / self.units if self.units > EPS else 0.0


@dataclass
class RealisedGain:
    buy_date: date
    sell_date: date
    units: float
    cost: float
    proceeds: float
    term: str
    rate_pct: float | None

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost


@dataclass
class LotBook:
    open_lots: list[Lot] = field(default_factory=list)
    realised: list[RealisedGain] = field(default_factory=list)
    unmatched_sell_units: float = 0.0  # sells with no lots (partial history)

    @property
    def units(self) -> float:
        return sum(l.units for l in self.open_lots)

    @property
    def invested(self) -> float:
        return sum(l.cost for l in self.open_lots)


def build_lots(txns: list, category_key: str, fmv_2018: float | None = None) -> LotBook:
    """`txns`: objects with txn_date, txn_type, units, amount, nav (any order)."""
    tax_class = get_category(category_key).tax_class
    book = LotBook()
    ordered = sorted(txns, key=lambda t: (t.txn_date, 0 if (t.units or 0) > 0 else 1))
    for t in ordered:
        units = float(t.units or 0)
        if t.txn_type in IGNORED_TYPES or abs(units) < EPS:
            continue
        if units > 0:
            amount = abs(float(t.amount)) if t.amount else units * float(t.nav or 0)
            book.open_lots.append(Lot(t.txn_date, units, amount, t.txn_type))
            continue

        # Outflow: consume FIFO.
        to_sell = -units
        sale_nav = float(t.nav) if t.nav else (abs(float(t.amount)) / to_sell if t.amount else 0.0)
        while to_sell > EPS and book.open_lots:
            lot = book.open_lots[0]
            take = min(lot.units, to_sell)
            cost = lot.cost_per_unit * take
            proceeds = sale_nav * take
            if tax_class == "equity" and fmv_2018 and lot.buy_date <= GRANDFATHER_CUTOFF:
                # s.112A grandfathering: cost = max(actual, min(FMV on 31-Jan-2018, sale value)).
                cost = max(cost, min(fmv_2018 * take, proceeds))
            gc = classify_gain(tax_class, lot.buy_date, t.txn_date)
            book.realised.append(RealisedGain(lot.buy_date, t.txn_date, take, cost, proceeds, gc.term, gc.rate_pct))
            lot.cost -= lot.cost_per_unit * take
            lot.units -= take
            to_sell -= take
            if lot.units <= EPS:
                book.open_lots.pop(0)
        if to_sell > EPS:
            book.unmatched_sell_units += to_sell
    return book


@dataclass
class UnrealisedSplit:
    st_gain: float = 0.0
    lt_gain: float = 0.0
    slab_gain: float = 0.0
    st_value: float = 0.0
    lt_value: float = 0.0
    slab_value: float = 0.0
    est_tax: float = 0.0


def unrealised_split(book: LotBook, category_key: str, nav: float, as_of: date, slab_pct: float) -> UnrealisedSplit:
    tax_class = get_category(category_key).tax_class
    out = UnrealisedSplit()
    for lot in book.open_lots:
        value = lot.units * nav
        gain = value - lot.cost
        gc = classify_gain(tax_class, lot.buy_date, as_of)
        if gc.term == "LTCG":
            out.lt_gain += gain
            out.lt_value += value
        elif gc.term == "STCG":
            out.st_gain += gain
            out.st_value += value
            out.est_tax += max(0.0, gain) * effective_rate(gc, slab_pct) / 100
        else:
            out.slab_gain += gain
            out.slab_value += value
            out.est_tax += max(0.0, gain) * effective_rate(gc, slab_pct) / 100
    return out


def redemption_estimate(book: LotBook, category_key: str, nav: float, as_of: date, slab_pct: float, units: float) -> dict:
    """Simulate selling `units` today: FIFO gain split, exit load and estimated tax (before
    the equity LTCG exemption, which is applied at portfolio level)."""
    cat = get_category(category_key)
    remaining = units
    rows = []
    exit_load = 0.0
    locked_units = 0.0
    for lot in book.open_lots:
        if remaining <= EPS:
            break
        take = min(lot.units, remaining)
        held_days = (as_of - lot.buy_date).days
        if cat.lock_in_days and held_days < cat.lock_in_days:
            locked_units += take
            remaining -= take
            continue
        value = take * nav
        cost = lot.cost_per_unit * take
        gc = classify_gain(cat.tax_class, lot.buy_date, as_of)
        if cat.exit_load_days and held_days < cat.exit_load_days:
            exit_load += value * cat.exit_load_pct / 100
        tax = max(0.0, value - cost) * effective_rate(gc, slab_pct) / 100 if gc.term != "LTCG" else 0.0
        rows.append({"buy_date": lot.buy_date.isoformat(), "units": round(take, 4), "value": round(value, 2), "cost": round(cost, 2),
                     "gain": round(value - cost, 2), "term": gc.term, "est_tax_ex_ltcg": round(tax, 2),
                     "long_term_from": gc.long_term_on.isoformat() if gc.long_term_on else None})
        remaining -= take
    lt_gain = sum(r["gain"] for r in rows if r["term"] == "LTCG")
    return {
        "units_requested": round(units, 4),
        "units_sellable": round(units - locked_units - max(0.0, remaining), 4),
        "units_locked": round(locked_units, 4),
        "value": round(sum(r["value"] for r in rows), 2),
        "gain": round(sum(r["gain"] for r in rows), 2),
        "stcg_gain": round(sum(r["gain"] for r in rows if r["term"] == "STCG"), 2),
        "ltcg_gain": round(lt_gain, 2),
        "slab_gain": round(sum(r["gain"] for r in rows if r["term"] == "SLAB"), 2),
        "est_tax_excluding_ltcg": round(sum(r["est_tax_ex_ltcg"] for r in rows), 2),
        "exit_load": round(exit_load, 2),
        "lots": rows,
    }
