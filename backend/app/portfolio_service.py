"""Portfolio X-ray over the DB: resolve the investor's funds, value them, load each fund's latest disclosure and
hand the numbers to the pure maths in analytics/xray.py."""

from datetime import date

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics import xray
from app.analytics.fund_lens import regular_growth_sibling, resolve_funds
from app.analytics.performance import MODEL_VERSION, latest_metrics, regular_ter
from app.data.linking import norm_name
from app.models import Scheme, SchemeHolding
from app.reference.categories import CATEGORIES, get_category

CLEAR_MATCH = 85  # token_sort_ratio at or above which the top name match is taken without asking


# Neutral display labels: the ranking model's own tier names read as instructions in a fund list.
TIER_BAND = {"core": "top band", "satellite": "upper-middle band", "watch": "lower-middle band", "exit": "bottom band"}


def resolve_one(db: Session, fund: str) -> tuple[Scheme | None, dict | None]:
    """Fund name or AMFI code -> (scheme, None), or (None, error) with code not_found | ambiguous."""
    text = str(fund).strip()
    if text.isdigit() and (s := db.get(Scheme, int(text))):
        return s, None
    matches = resolve_funds(db, text)
    if not matches:
        return None, {"fund": text, "code": "not_found", "message": f"No mutual fund matches '{text}'."}
    bases = list(dict.fromkeys(norm_name(m["name"]) for m in matches))
    if len(bases) > 1 and fuzz.token_sort_ratio(norm_name(text), bases[0]) < CLEAR_MATCH:
        names = list(dict.fromkeys(m["name"] for m in matches))
        return None, {"fund": text, "code": "ambiguous", "message": f"'{text}' matches several funds; pick one.",
                      "candidates": names}
    return db.get(Scheme, matches[0]["amfi_code"]), None


def latest_holdings(db: Session, code: int) -> tuple[list[SchemeHolding], str | None]:
    """Rows of the latest disclosure stored under `code` (a Regular-Growth code) and its "YYYY-MM"."""
    month = db.scalar(select(func.max(SchemeHolding.disclosure_month)).where(SchemeHolding.amfi_code == code))
    if month is None:
        return [], None
    rows = db.scalars(select(SchemeHolding).where(SchemeHolding.amfi_code == code,
                                                  SchemeHolding.disclosure_month == month)).all()
    return list(rows), month.strftime("%Y-%m")


def load_portfolio(db: Session, items: list[dict]) -> dict:
    """items: [{fund, amount_inr?, units?}] -> fund name keyed holdings/values plus per-line detail and problems."""
    out = {"holdings": {}, "values": {}, "months": {}, "lines": [], "amcs": {}, "schemes": {},
           "errors": [], "missing_details": []}
    for item in items:
        s, err = resolve_one(db, item["fund"])
        if err:
            out["errors"].append(err)
            continue
        if item.get("amount_inr") is not None:
            value, method = float(item["amount_inr"]), "amount"
        elif item.get("units") is not None and s.latest_nav:
            value, method = float(item["units"]) * s.latest_nav, "units_x_nav"
        else:
            need = "latest NAV" if item.get("units") is not None else "amount_inr or units"
            out["missing_details"].append({"fund": s.name, "need": need})
            continue
        rows, month = latest_holdings(db, regular_growth_sibling(db, s).amfi_code)
        out["values"][s.name] = out["values"].get(s.name, 0.0) + value
        out["amcs"][s.name], out["schemes"][s.name] = s.amc_name, s
        if rows:
            out["holdings"][s.name], out["months"][s.name] = rows, month
        elif s.name not in {m["fund"] for m in out["missing_details"]}:
            out["missing_details"].append({"fund": s.name, "need": "portfolio disclosure not loaded"})
        out["lines"].append({"fund": s.name, "amfi_code": s.amfi_code, "plan": s.plan_type, "option": s.option_type,
                             "value_inr": value, "valuation_method": method, "disclosure_month": month})
    total = sum(out["values"].values())
    for line in out["lines"]:
        line["weight_pct"] = line["value_inr"] / total * 100 if total else 0.0
    return out


def _amc_exposure(p: dict) -> list[dict]:
    total = sum(p["values"].values())
    groups: dict[str, dict] = {}
    for fund, value in p["values"].items():
        g = groups.setdefault(p["amcs"][fund], {"name": p["amcs"][fund], "value_inr": 0.0, "funds": []})
        g["value_inr"] += value
        g["funds"].append(fund)
    for g in groups.values():
        g["pct"] = g["value_inr"] / total * 100 if total else 0.0
    return sorted(groups.values(), key=lambda g: g["value_inr"], reverse=True)


def xray_portfolio(db: Session, items: list[dict], objective: dict | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    p = load_portfolio(db, items)
    ex = xray.exposure(p["holdings"], p["values"])
    ex["amcs"] = _amc_exposure(p)
    ov = xray.pairwise_overlap(p["holdings"])
    cov = xray.coverage(p["values"], p["holdings"], p["months"], today)
    return {
        "as_of": today.isoformat(),
        "disclosure_months": p["months"],
        "holdings": p["lines"],
        "coverage": cov,
        "exposures": ex,
        "overlap": ov,
        "concentration": xray.concentration(ex, ov),
        "reference": None,  # no market reference portfolio is loaded yet
        "objective_checks": xray.objective_checks(ex, objective),
        "observations": xray.observations(ov, cov, p["months"]),
        "missing_details": p["missing_details"],
        "assumptions": [
            "Based on latest monthly disclosure per fund",
            "Other plans and options of a scheme share its Regular-Growth portfolio disclosure",
            "Values given as units use the latest NAV of that plan",
            "Exposure percentages are of total portfolio value, including funds without a disclosure",
        ],
        "errors": p["errors"],
    }


def simulate(db: Session, items: list[dict], changes: list[dict]) -> dict:
    p = load_portfolio(db, items)
    named = []
    for c in changes:
        c = dict(c)
        for key in ("from_fund", "to_fund"):
            s, err = resolve_one(db, c[key])
            if err:
                p["errors"].append(err)
            elif s:
                c[key] = s.name
                if key == "to_fund" and s.name not in p["holdings"]:
                    rows, _ = latest_holdings(db, regular_growth_sibling(db, s).amfi_code)
                    if rows:
                        p["holdings"][s.name] = rows
        named.append(c)
    out = xray.simulate_changes(p["holdings"], p["values"], named)
    return {**out, "missing_details": p["missing_details"], "errors": p["errors"],
            "assumptions": ["Based on latest monthly disclosure per fund"]}


def screen(db: Session, items: list[dict], category: str | None = None, amount_inr: float | None = None,
           limit: int = 10) -> dict:
    p = load_portfolio(db, items)
    if category:
        cat = next((c for c in CATEGORIES.values() if category.lower() in (c.key, c.label.lower())), None)
        if cat is None:
            raise ValueError(f"Unknown category '{category}'")
    elif p["values"]:
        cat = get_category(p["schemes"][max(p["values"], key=p["values"].get)].category_key)
    else:
        raise ValueError("No fund in the portfolio could be valued, so a category is needed")
    peers = db.scalars(select(Scheme).where(Scheme.category_key == cat.key, Scheme.in_universe.is_(True),
                                            Scheme.plan_type == "regular", Scheme.option_type == "growth")).all()
    held = {s.amfi_code for s in p["schemes"].values()}
    cands, months, by_name = {}, {}, {}
    for s in peers:
        if s.amfi_code in held:
            continue
        rows, month = latest_holdings(db, s.amfi_code)
        if rows:
            cands[s.name], months[s.name], by_name[s.name] = rows, month, s
    out = xray.low_overlap_screen(p["holdings"], p["values"], cands, amount_inr, limit)
    metrics = latest_metrics(db, {s.amfi_code for s in peers})
    scores = sorted((m["composite_score"] for m in metrics.values() if m.get("composite_score") is not None), reverse=True)
    ters = regular_ter(db, {by_name[c["name"]].amfi_code for c in out["candidates"]})
    for c in out["candidates"]:
        code = by_name[c["name"]].amfi_code
        m = metrics.get(code) or {}
        score = m.get("composite_score")
        c.update({"amfi_code": code, "expense_ratio_regular_pct": ters.get(code), "disclosure_month": months[c["name"]],
                  "rank": {"position": scores.index(score) + 1 if score is not None else None, "of": len(peers),
                           "tier": TIER_BAND.get(m.get("tier")), "rank_basis": f"{MODEL_VERSION} composite within {cat.label}"}})
    return {"category": cat.label, **out, "missing_details": p["missing_details"], "errors": p["errors"]}
