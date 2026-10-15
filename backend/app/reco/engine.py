"""Recommendation engine v0 (PRD §11): diagnosis rules -> suitability-gated action cards.

The engine decides; the copilot only explains. Every run is persisted with its inputs
and model version so it can be reproduced for AMC/AMFI due diligence (REC-7).
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.lots import redemption_estimate
from app.analytics.performance import latest_metrics, rank_category
from app.analytics.portfolio import CASH_LIKE, HoldingView
from app.analytics.sip import months_invested
from app.config import get_settings
from app.models import RecoItem, RecoRun, Scheme
from app.reference.categories import RISK_LABELS, get_category
from app.reference.risk import BUCKET_LABELS, TEMPLATES, suitability
from app.reference.tax import CESS, EQUITY_RULES, add_months
from app.services.analysis import Analysis

RECO_VERSION = "reco-v0.1"
CARD_TTL_DAYS = 7
DISCLOSURES = ["mf_market_risk", "commission_disclosure", "ai_generated"]
PRIORITY = {"risk_mismatch": 10, "laggard": 20, "drift": 30, "idle_cash": 35, "missing_asset": 40, "clutter": 50, "amc_concentration": 60, "sip_health": 25, "profile": 1}


def _scheme_ref(s: Scheme) -> dict:
    return {"amfi_code": s.amfi_code, "name": s.name, "category": get_category(s.category_key).label,
            "riskometer": RISK_LABELS.get(s.riskometer, s.riskometer), "plan": s.plan_type}


def _inr(x: float) -> float:
    return round(x / 100) * 100 if x >= 1000 else round(x, 2)


class Engine:
    def __init__(self, db: Session, a: Analysis):
        self.db, self.a = db, a
        self.settings = get_settings()
        self.risk = a.risk
        self.as_of = a.pv.as_of
        self.held = {h.code for h in a.pv.holdings}
        self.cards: list[dict] = []
        self._pick_cache: dict[str, Scheme | None] = {}
        self.ltcg_left = max(0.0, EQUITY_RULES[-1].ltcg_exemption - a.pv.realised_fy.get("equity_LTCG", 0.0))
        self.routed: dict[str, float] = defaultdict(float)  # money already sent to an asset class by earlier cards

    # ------------------------------------------------------------------ helpers
    def pick(self, category_key: str) -> Scheme | None:
        """Best suitable scheme in a category: tiered by the ranking model, >=3y record."""
        if category_key in self._pick_cache:
            return self._pick_cache[category_key]
        codes = set(self.db.scalars(select(Scheme.amfi_code).where(Scheme.category_key == category_key, Scheme.in_universe.is_(True))))
        metrics = latest_metrics(self.db, codes)
        if len(metrics) < max(3, len(codes) // 2):
            metrics = rank_category(self.db, self.a.nb, category_key, self.as_of)
        ranked = sorted(
            (c for c, m in metrics.items() if c in codes and m.get("tier") in ("core", "satellite") and m.get("composite_score") is not None),
            key=lambda c: (metrics[c]["tier"] != "core", -metrics[c]["composite_score"]),
        )
        held_first = [c for c in ranked if c in self.held] + [c for c in ranked if c not in self.held]
        choice = self.db.get(Scheme, held_first[0]) if held_first else None
        self._pick_cache[category_key] = choice
        return choice

    def suit(self, category_key: str) -> dict:
        return suitability(self.risk.bucket, category_key, self.risk.horizon_years)

    def tax_block(self, h: HoldingView, units: float) -> dict:
        slab = self.a.investor.tax_slab_pct or self.settings.default_tax_slab_pct
        est = redemption_estimate(h.lots, h.scheme.category_key, h.nav, self.as_of, slab, units)
        ltcg_tax, exemption_note = 0.0, None
        if est["ltcg_gain"] > 0:
            if get_category(h.scheme.category_key).tax_class == "equity":
                full = EQUITY_RULES[-1].ltcg_exemption
                if self.ltcg_left < full:
                    exemption_note = f"Assumes higher-priority suggestions use ₹{full - self.ltcg_left:,.0f} of this year's ₹{full:,.0f} LTCG exemption first."
                taxable = max(0.0, est["ltcg_gain"] - self.ltcg_left)
                self.ltcg_left = max(0.0, self.ltcg_left - est["ltcg_gain"])
                ltcg_tax = taxable * EQUITY_RULES[-1].ltcg_rate / 100 * (1 + CESS)
            else:
                ltcg_tax = est["ltcg_gain"] * 12.5 / 100 * (1 + CESS)
        st_lots = [l for l in est["lots"] if l["term"] != "LTCG" and l["gain"] > 0 and l["long_term_from"]]
        sequencing = None
        if st_lots or est["exit_load"] > 0 or est["units_locked"] > 0:
            parts = []
            if est["units_locked"] > 0:
                parts.append(f"{est['units_locked']:.2f} units are still in lock-in and can't be switched yet")
            if st_lots:
                when = max(l["long_term_from"] for l in st_lots)
                st_value = sum(l["value"] for l in st_lots)
                parts.append(f"₹{st_value:,.0f} of these units are short-term; waiting until {when} avoids short-term tax on them")
            if est["exit_load"] > 0:
                parts.append(f"an exit load of about ₹{est['exit_load']:,.0f} applies to units held under the exit-load period")
            sequencing = "Stop or redirect the SIP now; " + "; ".join(parts) + "."
        return {
            "units": est["units_sellable"],
            "value": est["value"],
            "est_gain_inr": est["gain"],
            "est_tax_inr": round(est["est_tax_excluding_ltcg"] + ltcg_tax, 2),
            "exit_load_inr": est["exit_load"],
            "stcg_gain_inr": est["stcg_gain"],
            "ltcg_gain_inr": est["ltcg_gain"],
            "sequencing": sequencing,
            "exemption_note": exemption_note,
            "label": "Estimate, not tax advice",
        }

    def card(self, finding: str, action: str, title: str, rationale: list[str], *, from_h: HoldingView | None = None,
             to: Scheme | None = None, amount: float | None = None, sip_amount: float | None = None, tax: dict | None = None,
             suit_cat: str | None = None) -> None:
        suit = None
        if suit_cat or to:
            cat_key = suit_cat or to.category_key
            s = self.suit(cat_key)
            suit = {"scheme_category": get_category(cat_key).label, "scheme_riskometer": RISK_LABELS.get(to.riskometer) if to else None,
                    "investor_bucket": BUCKET_LABELS[self.risk.bucket], "pass": s["pass"], "reason": s["reason"]}
            if not s["pass"]:
                return  # REC-6: never render a card that fails the gate
        self.cards.append({
            "finding": finding,
            "priority": PRIORITY.get(finding, 70),
            "action": action,
            "title": title,
            "from_scheme": _scheme_ref(from_h.scheme) if from_h else None,
            "to_scheme": _scheme_ref(to) if to else None,
            "amount_inr": _inr(amount) if amount else None,
            "sip_amount_inr": _inr(sip_amount) if sip_amount else None,
            "rationale": rationale,
            "suitability": suit,
            "tax_and_exit_load": tax,
            "data_as_of": (self.a.pv.data_as_of or self.as_of).isoformat(),
            "euin": self.settings.euin,
            "disclosures": DISCLOSURES,
        })

    def monthly_capacity(self) -> float:
        """Typical monthly investing, from SIPs or the last 12 months of purchases."""
        sip_total = sum(h.sip.amount for h in self.a.pv.holdings if h.sip and h.sip.status != "stopped")
        if sip_total:
            return sip_total
        start = add_months(self.as_of, -12)
        bought = sum(abs(t.amount or 0) for t in self.a.pv.all_txns if t.txn_type in ("PURCHASE", "PURCHASE_SIP") and t.txn_date > start)
        return bought / 12

    # ------------------------------------------------------------------ rules
    def rule_risk_mismatch(self) -> None:
        for h in self.a.pv.holdings:
            s = self.suit(h.scheme.category_key)
            if s["pass"]:
                continue
            why = [s["reason"] + ".", f"It is {h.weight * 100:.1f}% of your portfolio."]
            to = None
            # Coordinate with the mix: if equity is already over its band, send equity money
            # that doesn't fit to the most under-weight asset instead of another equity fund.
            under = self.underweight_assets()
            if h.category.mix.get("equity", 0) >= 0.65 and self.equity_over_band() and under:
                asset = under[0][0]
                to = self.asset_destination(asset)
                if to:
                    self.routed[asset] += h.value
                    why.append(f"Your equity is already above your band, so moving this money into {asset} also brings your mix back toward target.")
            if to is None:
                alt_cat = self.alternative_category(h.scheme.category_key)
                to = self.pick(alt_cat) if alt_cat else None
                if to:
                    why.append(f"{to.name} is a top-ranked {get_category(alt_cat).label} fund that fits your profile.")
            if h.sip and h.sip.status != "stopped" and to:
                self.card("risk_mismatch", "STOP_SIP", f"Redirect the ₹{h.sip.amount:,.0f}/month SIP in {h.scheme.name}",
                          [s["reason"] + ".", f"Send it to {to.name} instead, so new money goes where it fits."],
                          from_h=h, to=to, sip_amount=h.sip.amount)
            self.card("risk_mismatch", "SWITCH" if to else "REVIEW",
                      f"Move {h.scheme.name} to a fund that fits your profile" if to else f"Review {h.scheme.name}",
                      why, from_h=h, to=to, amount=h.value, tax=self.tax_block(h, h.units))

    def underweight_assets(self) -> list[tuple[str, float]]:
        """Debt/gold below target, largest rupee shortfall first (net of money already routed)."""
        t = TEMPLATES[self.risk.bucket]
        out = []
        for asset in ("debt", "gold"):
            gap = (t.target[asset] - self.a.pv.allocation.get(asset, 0.0) * 100) / 100 * self.a.pv.value - self.routed[asset]
            if gap > 0:
                out.append((asset, gap))
        return sorted(out, key=lambda x: -x[1])

    def equity_over_band(self) -> bool:
        return self.a.pv.allocation.get("equity", 0.0) * 100 > TEMPLATES[self.risk.bucket].bands["equity"][1]

    def asset_destination(self, asset: str) -> Scheme | None:
        t = TEMPLATES[self.risk.bucket]
        mix = {"debt": t.debt_mix, "gold": t.gold_mix, "equity": t.equity_mix}[asset]
        return self.pick(max(mix, key=mix.get))

    def alternative_category(self, key: str) -> str | None:
        ladder = {"small_cap": "mid_cap", "mid_cap": "large_mid_cap", "sectoral": "flexi_cap", "thematic": "flexi_cap",
                  "large_mid_cap": "flexi_cap", "multi_cap": "flexi_cap", "focused": "flexi_cap", "value": "flexi_cap",
                  "contra": "flexi_cap", "aggressive_hybrid": "dynamic_asset_allocation", "credit_risk": "corporate_bond",
                  "flexi_cap": "large_cap", "intl_fof": "flexi_cap", "dynamic_asset_allocation": "conservative_hybrid"}
        cur = key
        for _ in range(5):
            cur = ladder.get(cur)
            if cur is None:
                return None
            if self.suit(cur)["pass"]:
                return cur
        return None

    def rule_laggards(self) -> None:
        for h in self.a.pv.holdings:
            m = h.extras.get("metrics") or {}
            vb = h.extras.get("vs_benchmark")
            weak_tier = m.get("tier") == "exit"
            weak_alpha = vb is not None and vb["years"] >= 3 and vb["alpha_pp"] <= -3
            if not (weak_tier or weak_alpha) or not self.suit(h.scheme.category_key)["pass"]:
                continue
            to = self.pick(h.scheme.category_key)
            if to is None or to.amfi_code == h.code:
                continue
            why = []
            if weak_tier:
                why.append(f"Bottom-tier in its category on consistency, risk-adjusted return and downside protection (rank model {m.get('composite_score')}/100).")
            if weak_alpha:
                why.append(f"On your own investments it trailed {vb['benchmark']} by {abs(vb['alpha_pp']):.1f} pts a year over {vb['years']:.1f} years.")
            if h.sip and h.sip.status != "stopped":
                self.card("laggard", "STOP_SIP", f"Redirect the SIP in {h.scheme.name}", why + [f"Send the ₹{h.sip.amount:,.0f}/month to {to.name} instead."],
                          from_h=h, to=to, sip_amount=h.sip.amount)
            self.card("laggard", "SWITCH", f"Switch out of {h.scheme.name}", why, from_h=h, to=to, amount=h.value, tax=self.tax_block(h, h.units))

    def rule_clutter(self) -> None:
        by_cat: dict[str, list[HoldingView]] = defaultdict(list)
        for h in self.a.pv.holdings:
            if get_category(h.scheme.category_key).peer_group:
                by_cat[h.scheme.category_key].append(h)
        for key, hs in by_cat.items():
            if len(hs) <= 2:
                continue
            ordered = sorted(hs, key=lambda h: -((h.extras.get("metrics") or {}).get("composite_score") or 0))
            keep = ordered[0]
            for h in ordered[2:]:
                self.card("clutter", "SWITCH", f"Consolidate {h.scheme.name} into {keep.scheme.name}",
                          [f"You hold {len(hs)} {get_category(key).label} funds; beyond two they mostly own the same stocks.",
                           f"{keep.scheme.name} ranks highest of the ones you hold."],
                          from_h=h, to=keep.scheme, amount=h.value, tax=self.tax_block(h, h.units))

    def rule_balance(self) -> None:
        """Rebalance with new money first (no tax, no exit load): one card per destination."""
        t = TEMPLATES[self.risk.bucket]
        alloc = self.a.pv.allocation
        capacity = self.monthly_capacity()
        eq = alloc.get("equity", 0.0) * 100
        eq_lo, eq_hi = t.bands["equity"]
        over = eq > eq_hi
        gaps = [(a, g) for a, g in self.underweight_assets() if over or alloc.get(a, 0.0) * 100 < t.bands[a][0]]
        total_gap = sum(g for _, g in gaps)
        budget = capacity * 0.5 if capacity else None  # keep at least half of today's SIPs in equity
        for asset, gap in gaps:
            to = self.asset_destination(asset)
            if to is None:
                continue
            lo, hi = t.bands[asset]
            actual = alloc.get(asset, 0.0) * 100
            sip = budget * gap / total_gap if budget else gap / 12
            months = max(1, round(gap / sip)) if sip else None
            why = [f"{asset.title()} is {actual:.0f}% of your portfolio against a {lo:.0f}-{hi:.0f}% band for a {BUCKET_LABELS[self.risk.bucket]} profile."]
            if over:
                why.append(f"Equity is {eq:.0f}%, above your {eq_lo:.0f}-{eq_hi:.0f}% band. Steering new money is the tax-free way to rebalance.")
            why.append(f"About ₹{_inr(gap):,.0f} more in {asset} reaches the {t.target[asset]:.0f}% target" + (f"; at this pace, in roughly {months} months." if months else "."))
            self.card("missing_asset" if actual < 1 else "drift", "REDIRECT_SIP" if over and capacity else "START_SIP",
                      f"Point ₹{_inr(sip):,.0f}/month toward {asset}", why, to=to, sip_amount=sip)
        if eq < eq_lo:
            to = self.asset_destination("equity")
            if to:
                gap = (t.target["equity"] - eq) / 100 * self.a.pv.value
                self.card("drift", "START_SIP", "Add equity for long-term growth",
                          [f"Equity is {eq:.0f}% against a {eq_lo:.0f}-{eq_hi:.0f}% band for your profile.",
                           f"About ₹{_inr(gap):,.0f} more reaches the {t.target['equity']:.0f}% target; a monthly SIP gets there without timing the market."],
                          to=to, sip_amount=gap / 12)

    def rule_idle_cash(self) -> None:
        pv = self.a.pv
        if pv.cash_like_share <= 0.20:
            return
        idle = [h for h in pv.holdings if h.scheme.category_key in CASH_LIKE and h.first_invested and (self.as_of - h.first_invested).days > 90]
        if not idle:
            return
        h = max(idle, key=lambda x: x.value)
        t = TEMPLATES[self.risk.bucket]
        to = self.pick(max(t.equity_mix, key=t.equity_mix.get))
        if to is None:
            return
        excess = (pv.cash_like_share - 0.10) * pv.value
        months = 6 if self.risk.bucket in ("conservative", "moderately_conservative") else 9
        self.card("idle_cash", "STP", f"Put idle money in {h.scheme.name} to work gradually",
                  [f"{pv.cash_like_share * 100:.0f}% of your portfolio has been in cash-like funds for over 90 days.",
                   f"A systematic transfer of about ₹{_inr(excess / months):,.0f}/month over {months} months moves it into your target mix without timing the market."],
                  from_h=h, to=to, amount=excess, sip_amount=excess / months)

    def rule_amc_concentration(self) -> None:
        eq = [h for h in self.a.pv.holdings if h.category.mix.get("equity", 0) >= 0.65]
        total = sum(h.value for h in eq)
        by_amc = defaultdict(float)
        for h in eq:
            by_amc[h.scheme.amc_name] += h.value
        if len(eq) < 3 or not total:
            return
        amc, v = max(by_amc.items(), key=lambda kv: kv[1])
        if v / total > 0.40:
            self.card("amc_concentration", "REVIEW", f"Spread new equity SIPs beyond {amc}",
                      [f"{v / total * 100:.0f}% of your equity money is with one fund house.",
                       "Spreading across AMCs limits the impact of any one house's process or people changes."])

    def rule_sip_health(self) -> None:
        pv = self.a.pv
        lapsing = [h for h in pv.holdings if h.sip and h.sip.status == "missed_latest"]
        for h in lapsing:
            self.card("sip_health", "REVIEW", f"Your SIP in {h.scheme.name} missed its last instalment",
                      [f"The last instalment of ₹{h.sip.amount:,.0f} was on {h.sip.last_date.isoformat()}.", "Check the bank mandate or balance so the SIP doesn't lapse."], from_h=h)
        if pv.has_transactions and not any(h.sip and h.sip.status != "stopped" for h in pv.holdings) and months_invested(pv.all_txns, self.as_of) < 6:
            t = TEMPLATES[self.risk.bucket]
            to = self.pick(max(t.equity_mix, key=t.equity_mix.get))
            if to:
                self.card("sip_health", "START_SIP", "Restart a monthly SIP",
                          ["You have no running SIP and invested in fewer than 6 of the last 12 months.", "A fixed monthly SIP keeps you investing through market dips."], to=to)

    def run(self) -> list[dict]:
        if self.risk is None:
            self.cards.append({"finding": "profile", "priority": 1, "action": "COMPLETE_RISK_PROFILE", "title": "Complete your risk profile",
                               "rationale": ["Fund suggestions need your risk profile first; it takes about 2 minutes."],
                               "from_scheme": None, "to_scheme": None, "amount_inr": None, "sip_amount_inr": None, "suitability": None,
                               "tax_and_exit_load": None, "data_as_of": self.as_of.isoformat(), "euin": self.settings.euin, "disclosures": ["ai_generated"]})
            return self.cards
        for rule in (self.rule_risk_mismatch, self.rule_laggards, self.rule_clutter, self.rule_balance, self.rule_idle_cash,
                     self.rule_amc_concentration, self.rule_sip_health):
            rule()
        # One card per (action, from, to): rules can overlap (e.g. a misfit that is also a laggard).
        seen, unique = set(), []
        for c in sorted(self.cards, key=lambda c: c["priority"]):
            key = (c["action"], (c["from_scheme"] or {}).get("amfi_code"), (c["to_scheme"] or {}).get("amfi_code"))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        self.cards = unique
        return self.cards


def run_recommendations(db: Session, a: Analysis, trigger: str = "user") -> RecoRun:
    cards = Engine(db, a).run()
    expires = datetime.combine(date.today() + timedelta(days=CARD_TTL_DAYS), time(23, 59, 59), tzinfo=timezone.utc)
    run = RecoRun(
        investor_id=a.investor.id,
        risk_profile_id=a.risk.id if a.risk else None,
        trigger=trigger,
        model_version=RECO_VERSION,
        inputs={
            "as_of": a.as_of.isoformat(),
            "risk_bucket": a.risk.bucket if a.risk else None,
            "horizon_years": a.risk.horizon_years if a.risk else None,
            "allocation": a.pv.overview()["allocation_pct"],
            "holdings": [{"amfi_code": h.code, "value": round(h.value, 2), "tier": (h.extras.get("metrics") or {}).get("tier")} for h in a.pv.holdings],
            "score_version": a.score["version"],
        },
        summary={"cards": len(cards), "score": a.score["score"]},
    )
    db.add(run)
    db.flush()
    for c in cards:
        c["run_id"] = run.id
        c["model_version"] = RECO_VERSION
        c["risk_bucket"] = a.risk.bucket if a.risk else None
        c["expires_at"] = expires.isoformat()
        item = RecoItem(run_id=run.id, action=c["action"], amfi_code=(c["to_scheme"] or {}).get("amfi_code"),
                        from_amfi_code=(c["from_scheme"] or {}).get("amfi_code"), amount=c.get("amount_inr"), priority=c["priority"],
                        suitability_pass=(c["suitability"] or {}).get("pass", True), card=c, expires_at=expires)
        db.add(item)
        db.flush()
        c["id"] = item.id
        item.card = dict(c)
    db.commit()
    return run
