from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.navbook import NavBook
from app.analytics.performance import benchmark_code, benchmark_label, latest_metrics, rank_category, scheme_metrics
from app.api.deps import get_investor
from app.config import get_settings
from app.copilot.compliance import DISCLOSURE_TEXT
from app.db import get_db
from app.insights.nudges import nudges
from app.insights.tax import tax_summary
from app.models import Investor, RecoRun, Scheme
from app.reco.engine import run_recommendations
from app.reference.categories import RISK_LABELS, get_category
from app.services.analysis import analyse

router = APIRouter(tags=["portfolio"])


@router.get("/investors/{investor_id}/portfolio")
def portfolio(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    a = analyse(db, inv)
    return {"overview": a.pv.overview(), "holdings": [h.summary() for h in a.pv.holdings],
            "risk_bucket": a.risk.bucket if a.risk else None}


@router.get("/investors/{investor_id}/dashboard")
def dashboard(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    """Everything the home screen needs in one round trip."""
    a = analyse(db, inv)
    return {
        "overview": a.pv.overview(),
        "holdings": [h.summary() for h in a.pv.holdings],
        "score": a.score if a.pv.holdings else None,
        "alerts": nudges(a)[:6],
        "risk_bucket": a.risk.bucket if a.risk else None,
    }


@router.get("/investors/{investor_id}/score")
def score(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    return analyse(db, inv).score


@router.get("/investors/{investor_id}/tax")
def tax(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    return tax_summary(analyse(db, inv))


@router.get("/investors/{investor_id}/alerts")
def alerts(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    return nudges(analyse(db, inv))


@router.get("/investors/{investor_id}/overlap")
def overlap(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    return analyse(db, inv).overlap


@router.post("/investors/{investor_id}/recommendations/run")
def run_reco(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    run = run_recommendations(db, analyse(db, inv), trigger="user")
    return {"run_id": run.id, "model_version": run.model_version, "created_at": run.created_at.isoformat(),
            "cards": [i.card for i in sorted(run.items, key=lambda i: i.priority)]}


@router.get("/investors/{investor_id}/recommendations/latest")
def latest_reco(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    run = db.scalar(select(RecoRun).where(RecoRun.investor_id == inv.id).order_by(RecoRun.created_at.desc()).limit(1))
    if run is None:
        return None
    return {"run_id": run.id, "model_version": run.model_version, "created_at": run.created_at.isoformat(),
            "cards": [i.card for i in sorted(run.items, key=lambda i: i.priority)]}


@router.get("/schemes/search")
def search_schemes(q: str, regular_growth_only: bool = True, db: Session = Depends(get_db)):
    terms = [t for t in q.split() if len(t) > 1][:6]
    if not terms:
        return []
    stmt = select(Scheme).where(*[Scheme.name.ilike(f"%{t}%") for t in terms])
    if regular_growth_only:
        stmt = stmt.where(Scheme.plan_type == "regular", Scheme.option_type == "growth")
    return [{"amfi_code": s.amfi_code, "name": s.name, "amc": s.amc_name, "category": get_category(s.category_key).label,
             "plan": s.plan_type, "option": s.option_type, "nav": s.latest_nav, "nav_date": s.latest_nav_date.isoformat() if s.latest_nav_date else None}
            for s in db.scalars(stmt.limit(20))]


@router.get("/schemes/{amfi_code}")
def scheme_detail(amfi_code: int, db: Session = Depends(get_db)):
    s = db.get(Scheme, amfi_code)
    if s is None:
        raise HTTPException(404, "Scheme not found")
    nb = NavBook(db)
    latest = nb.latest(amfi_code)
    as_of = latest[0] if latest else None
    cat = get_category(s.category_key)
    m = latest_metrics(db, {amfi_code}).get(amfi_code)
    if m is None and as_of:
        m = rank_category(db, nb, s.category_key, as_of, extra_codes={amfi_code}).get(amfi_code) if cat.peer_group else scheme_metrics(nb, amfi_code, as_of, benchmark_code(s.category_key))[0]
    return {"amfi_code": s.amfi_code, "name": s.name, "amc": s.amc_name, "category": cat.label, "plan": s.plan_type, "option": s.option_type,
            "riskometer": RISK_LABELS.get(s.riskometer), "riskometer_basis": s.riskometer_source, "nav": latest[1] if latest else None,
            "nav_date": as_of.isoformat() if as_of else None, "benchmark_proxy": benchmark_label(s.category_key), "metrics": m,
            "nav_series": [{"date": d.isoformat(), "nav": n} for d, n in nb.month_end_series(amfi_code)][-72:],
            "disclosure": DISCLOSURE_TEXT["mf_market_risk"]}


@router.get("/meta/compliance")
def compliance_meta():
    s = get_settings()
    return {"distributor_name": s.distributor_name, "arn": s.arn, "tagline": "AMFI-registered Mutual Fund Distributor",
            "disclosures": DISCLOSURE_TEXT}
