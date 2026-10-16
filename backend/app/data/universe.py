"""Universe refinements that need feeds beyond the AMFI scheme master (PRD §11.1).

`sync_scheme_master` rebuilds `Scheme.in_universe` from NAVAll every day; this step then
applies what only the fund-performance feed knows:
  - schemes with a published AUM below MIN_AUM_CR leave the universe (unknown AUM: kept);
  - the official riskometer replaces the category default (riskometer_source = "amfi").
"""

from collections import defaultdict

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Scheme, SchemeLink, SchemePerformance
from app.reference.categories import RISK_LEVELS

MIN_AUM_CR = 500.0


def refine_universe(db: Session) -> dict:
    latest = db.scalar(select(func.max(SchemePerformance.as_of)))
    if latest is None:
        return {"applied": False, "reason": "no performance feed loaded"}
    rows = db.execute(
        select(SchemePerformance.external_key, SchemePerformance.aum_cr, SchemePerformance.riskometer).where(SchemePerformance.as_of == latest)
    ).all()
    aum: dict[str, float] = {}
    risk: dict[str, str] = {}
    for key, cr, level in rows:
        if cr is not None:
            aum[key] = max(aum.get(key, 0.0), float(cr))
        if level in RISK_LEVELS:
            risk[key] = level

    codes_by_key: dict[str, set[int]] = defaultdict(set)
    for code, key in db.execute(select(SchemeLink.amfi_code, SchemeLink.external_key).where(SchemeLink.source == "performance")):
        codes_by_key[key].add(code)

    small = {c for key, cr in aum.items() if cr < MIN_AUM_CR for c in codes_by_key.get(key, ())}
    if small:
        db.execute(update(Scheme).where(Scheme.amfi_code.in_(small), Scheme.in_universe.is_(True)).values(in_universe=False))
    updated = 0
    for key, level in risk.items():
        codes = codes_by_key.get(key)
        if codes:
            updated += db.execute(update(Scheme).where(Scheme.amfi_code.in_(codes)).values(riskometer=level, riskometer_source="amfi")).rowcount
    db.commit()
    return {"applied": True, "as_of": latest.isoformat(), "below_min_aum_removed": len(small), "riskometers_updated": updated}
