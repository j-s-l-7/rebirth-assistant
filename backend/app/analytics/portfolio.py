"""Portfolio view: per-scheme and portfolio-level numbers every other feature reads from."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.lots import LotBook, UnrealisedSplit, build_lots, unrealised_split
from app.analytics.navbook import NavBook
from app.analytics.sip import SipInfo, detect_sip
from app.analytics.xirr import xirr
from app.config import get_settings
from app.models import Folio, Holding, Investor, Scheme, Txn
from app.reference.categories import RISK_LABELS, get_category
from app.reference.tax import GRANDFATHER_CUTOFF, fy_bounds

INFLOW = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER", "GIFT_IN"}
OUTFLOW = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER", "GIFT_OUT"}
CASH_LIKE = {"overnight", "liquid", "money_market", "arbitrage", "ultra_short"}


def external_cashflows(txns: list) -> list[tuple[date, float]]:
    """Investor-perspective flows: money in is negative, money out positive."""
    flows = []
    for t in txns:
        amt = abs(float(t.amount or 0))
        if not amt:
            continue
        if t.txn_type in INFLOW:
            flows.append((t.txn_date, -amt))
        elif t.txn_type in OUTFLOW or t.txn_type == "DIVIDEND_PAYOUT":
            flows.append((t.txn_date, amt))
        elif t.txn_type == "REVERSAL":  # a reversed purchase returns money; a reversed redemption takes it back
            flows.append((t.txn_date, amt if (t.units or 0) < 0 else -amt))
    return flows


@dataclass
class HoldingView:
    scheme: Scheme
    units: float
    nav: float
    nav_date: date | None
    prev_nav: float | None
    value: float
    invested: float
    folios: list[str]
    txns: list
    lots: LotBook
    xirr: float | None
    first_invested: date | None
    sip: SipInfo | None
    unrealised: UnrealisedSplit
    weight: float = 0.0
    extras: dict = field(default_factory=dict)  # performance, suitability, tier ... filled later

    @property
    def code(self) -> int:
        return self.scheme.amfi_code

    @property
    def category(self):
        return get_category(self.scheme.category_key)

    @property
    def gain(self) -> float:
        return self.value - self.invested

    @property
    def gain_pct(self) -> float | None:
        return self.gain / self.invested if self.invested > 0 else None

    def summary(self) -> dict:
        cat = self.category
        return {
            "amfi_code": self.code,
            "name": self.scheme.name,
            "amc": self.scheme.amc_name,
            "category": cat.label,
            "category_key": cat.key,
            "asset_group": cat.group,
            "plan": self.scheme.plan_type,
            "option": self.scheme.option_type,
            "riskometer": RISK_LABELS.get(self.scheme.riskometer, self.scheme.riskometer),
            "riskometer_source": self.scheme.riskometer_source,
            "units": round(self.units, 4),
            "nav": round(self.nav, 4),
            "nav_date": self.nav_date.isoformat() if self.nav_date else None,
            "value": round(self.value, 2),
            "invested": round(self.invested, 2),
            "gain": round(self.gain, 2),
            "gain_pct": round(self.gain_pct * 100, 2) if self.gain_pct is not None else None,
            "xirr_pct": round(self.xirr * 100, 2) if self.xirr is not None else None,
            "weight_pct": round(self.weight * 100, 2),
            "first_invested": self.first_invested.isoformat() if self.first_invested else None,
            "folios": self.folios,
            "sip": self.sip.as_dict() if self.sip else None,
            **self.extras,
        }


@dataclass
class PortfolioView:
    investor_id: str
    as_of: date
    data_as_of: date | None
    holdings: list[HoldingView]
    value: float
    invested: float
    xirr: float | None
    day_change: float
    allocation: dict[str, float]  # asset class -> share of value (look-through)
    cash_like_share: float
    realised_fy: dict[str, float]
    all_txns: list
    has_transactions: bool

    @property
    def gain(self) -> float:
        return self.value - self.invested

    def by(self, key) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for h in self.holdings:
            out[key(h)] += h.value
        total = self.value or 1.0
        return {k: round(v / total * 100, 2) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}

    def overview(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "nav_data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "current_value": round(self.value, 2),
            "invested": round(self.invested, 2),
            "gain": round(self.gain, 2),
            "gain_pct": round(self.gain / self.invested * 100, 2) if self.invested else None,
            "xirr_pct": round(self.xirr * 100, 2) if self.xirr is not None else None,
            "day_change": round(self.day_change, 2),
            "holdings_count": len(self.holdings),
            "allocation_pct": {k: round(v * 100, 2) for k, v in self.allocation.items()},
            "cash_like_pct": round(self.cash_like_share * 100, 2),
            "by_category_pct": self.by(lambda h: h.category.label),
            "by_amc_pct": self.by(lambda h: h.scheme.amc_name),
            "by_plan_pct": self.by(lambda h: h.scheme.plan_type),
            "realised_this_fy": {k: round(v, 2) for k, v in self.realised_fy.items()},
            "active_sips": sum(1 for h in self.holdings if h.sip and h.sip.status != "stopped"),
            "monthly_sip_total": round(sum(h.sip.amount for h in self.holdings if h.sip and h.sip.status != "stopped"), 2),
        }


def build_portfolio(db: Session, investor: Investor, navbook: NavBook | None = None, as_of: date | None = None) -> PortfolioView:
    navbook = navbook or NavBook(db)
    slab = investor.tax_slab_pct or get_settings().default_tax_slab_pct
    folios = db.scalars(select(Folio).where(Folio.investor_id == investor.id)).all()
    folio_ids = [f.id for f in folios]
    folio_no = {f.id: f.folio_no for f in folios}
    holdings = db.scalars(select(Holding).where(Holding.folio_id.in_(folio_ids))).all() if folio_ids else []
    txns = db.scalars(select(Txn).where(Txn.folio_id.in_(folio_ids)).order_by(Txn.txn_date)).all() if folio_ids else []

    by_code_txns: dict[int, list] = defaultdict(list)
    for t in txns:
        by_code_txns[t.amfi_code].append(t)
    by_code_hold: dict[int, list[Holding]] = defaultdict(list)
    for h in holdings:
        by_code_hold[h.amfi_code].append(h)

    codes = set(by_code_hold) | set(by_code_txns)
    schemes = {s.amfi_code: s for s in db.scalars(select(Scheme).where(Scheme.amfi_code.in_(codes)))} if codes else {}
    navbook.preload(codes)
    latest_dates = [navbook.latest(c)[0] for c in codes if navbook.latest(c)]
    as_of = as_of or (max(latest_dates) if latest_dates else date.today())
    fy_start, fy_end = fy_bounds(as_of)

    views: list[HoldingView] = []
    realised_fy = {"STCG": 0.0, "LTCG": 0.0, "SLAB": 0.0, "equity_LTCG": 0.0}
    portfolio_flows: list[tuple[date, float]] = []
    alloc: dict[str, float] = defaultdict(float)
    cash_like = day_change = 0.0

    for code in sorted(codes):
        scheme = schemes.get(code)
        if scheme is None:
            continue
        stxns = by_code_txns.get(code, [])
        hrows = by_code_hold.get(code, [])
        units = sum(h.units or 0 for h in hrows) if hrows else None

        fmv = navbook.nav_on(code, GRANDFATHER_CUTOFF) if any(t.txn_date <= GRANDFATHER_CUTOFF for t in stxns) else None
        book = build_lots(stxns, scheme.category_key, fmv_2018=fmv)
        if units is None:
            units = book.units
        for r in book.realised:
            if fy_start <= r.sell_date <= fy_end:
                realised_fy[r.term] += r.gain
                if r.term == "LTCG" and get_category(scheme.category_key).tax_class == "equity":
                    realised_fy["equity_LTCG"] += r.gain
        portfolio_flows.extend(external_cashflows(stxns))

        if units <= 1e-4:
            continue
        latest = navbook.latest(code)
        stmt_nav = (hrows[0].statement_value / hrows[0].units) if hrows and hrows[0].statement_value and hrows[0].units else None
        nav_date, nav = latest if latest else (hrows[0].as_of if hrows else None, stmt_nav or 0.0)
        prev = navbook.previous(code)
        value = units * nav

        # Cost basis: FIFO lots when transactions reconcile with the statement balance,
        # otherwise the statement's own cost figure.
        if stxns and abs(book.units - units) <= max(0.01, 0.001 * units):
            invested = book.invested
        else:
            invested = sum(h.cost_value or 0 for h in hrows) or book.invested

        flows = external_cashflows(stxns)
        rate = xirr(flows + [(as_of, value)]) if flows else None
        first = min((t.txn_date for t in stxns), default=None)
        views.append(
            HoldingView(
                scheme=scheme, units=units, nav=nav, nav_date=nav_date, prev_nav=prev[1] if prev else None, value=value,
                invested=invested, folios=sorted({folio_no[h.folio_id] for h in hrows}), txns=stxns, lots=book, xirr=rate,
                first_invested=first, sip=detect_sip(stxns, as_of),
                unrealised=unrealised_split(book, scheme.category_key, nav, as_of, slab),
            )
        )
        for asset, share in get_category(scheme.category_key).mix.items():
            alloc[asset] += value * share
        if scheme.category_key in CASH_LIKE:
            cash_like += value
        if prev:
            day_change += units * (nav - prev[1])

    total = sum(v.value for v in views)
    for v in views:
        v.weight = v.value / total if total else 0.0
    views.sort(key=lambda v: -v.value)
    allocation = {k: alloc.get(k, 0.0) / total if total else 0.0 for k in ("equity", "debt", "gold")}
    nav_dates = [v.nav_date for v in views if v.nav_date]
    return PortfolioView(
        investor_id=investor.id,
        as_of=as_of,
        data_as_of=min(nav_dates) if nav_dates else None,
        holdings=views,
        value=total,
        invested=sum(v.invested for v in views),
        xirr=xirr(portfolio_flows + [(as_of, total)]) if portfolio_flows and total else None,
        day_change=day_change,
        allocation=allocation,
        cash_like_share=cash_like / total if total else 0.0,
        realised_fy=realised_fy,
        all_txns=list(txns),
        has_transactions=bool(txns),
    )
