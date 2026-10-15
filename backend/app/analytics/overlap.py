"""Stock-level overlap between equity schemes (PRD PF-4).

Uses AMC monthly portfolio disclosures when loaded (SchemeHolding rows); otherwise
reports same-category duplicates only, labelled as an estimate - we never invent an
overlap percentage without disclosure data.
"""

import csv
from collections import defaultdict
from datetime import date
from itertools import combinations
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.portfolio import PortfolioView
from app.models import SchemeHolding


def _latest_weights(db: Session, code: int) -> dict[str, tuple[str, str | None, float]]:
    month = db.scalar(select(func.max(SchemeHolding.disclosure_month)).where(SchemeHolding.amfi_code == code))
    if month is None:
        return {}
    rows = db.scalars(select(SchemeHolding).where(SchemeHolding.amfi_code == code, SchemeHolding.disclosure_month == month)).all()
    return {(r.security_isin or r.security_name.lower()): (r.security_name, r.sector, r.weight_pct) for r in rows}


def overlap_report(db: Session, pv: PortfolioView) -> dict:
    equity = [h for h in pv.holdings if h.category.mix.get("equity", 0) >= 0.65]
    codes = [h.code for h in equity]
    has_data = bool(codes) and db.scalar(select(SchemeHolding.amfi_code).where(SchemeHolding.amfi_code.in_(codes)).limit(1)) is not None
    weights = {h.code: _latest_weights(db, h.code) if has_data else {} for h in equity}
    pairs, sectors = [], defaultdict(float)
    for a, b in combinations(equity, 2):
        wa, wb = weights[a.code], weights[b.code]
        if wa and wb:
            common = set(wa) & set(wb)
            pct = sum(min(wa[k][2], wb[k][2]) for k in common)
            top = sorted(common, key=lambda k: -min(wa[k][2], wb[k][2]))[:5]
            pairs.append({"a": a.scheme.name, "b": b.scheme.name, "a_code": a.code, "b_code": b.code, "overlap_pct": round(pct, 1),
                          "basis": "portfolio_disclosure", "top_common": [wa[k][0] for k in top]})
        elif a.scheme.category_key == b.scheme.category_key:
            pairs.append({"a": a.scheme.name, "b": b.scheme.name, "a_code": a.code, "b_code": b.code, "overlap_pct": None,
                          "basis": "same_category_estimate", "top_common": []})
    covered = [h for h in equity if weights[h.code]]
    eq_value = sum(h.value for h in covered)
    for h in covered:
        for _, sector, w in weights[h.code].values():
            sectors[sector or "Unclassified"] += h.value * w / 100
    sector_pct = {k: round(v / eq_value * 100, 1) for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])} if eq_value else {}
    return {
        "pairs": sorted(pairs, key=lambda p: -(p["overlap_pct"] or 0)),
        "disclosure_coverage_pct": round(sum(h.value for h in covered) / sum(h.value for h in equity) * 100, 1) if equity else 0.0,
        "sector_exposure_pct": sector_pct,
    }


def load_disclosure_csv(db: Session, path: Path) -> int:
    """CSV columns: amfi_code, disclosure_month (YYYY-MM-DD), security_isin, security_name, sector, weight_pct."""
    n = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            db.add(SchemeHolding(amfi_code=int(row["amfi_code"]), disclosure_month=date.fromisoformat(row["disclosure_month"]),
                                 security_isin=row.get("security_isin") or None, security_name=row["security_name"],
                                 sector=row.get("sector") or None, weight_pct=float(row["weight_pct"])))
            n += 1
    db.commit()
    return n
