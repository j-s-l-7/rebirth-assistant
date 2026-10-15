from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_investor
from app.db import get_db
from app.ingest.cas import ImportRejected, import_cas_data, parse_cas_pdf
from app.ingest.demo import DemoDataMissing, create_demo_investor
from app.insights.goals import goal_plan
from app.models import Consent, Goal, ImportJob, Investor, RiskProfile
from app.reference.risk import BUCKET_LABELS, QUESTIONNAIRE_VERSION, QUESTIONS, TEMPLATES, score_answers
from app.security import encrypt_pan, hash_pan, mask_pan, normalise_pan
from app.services.analysis import latest_risk_profile

router = APIRouter(tags=["investors"])
NOTICE_VERSION = "privacy-v0.1"
MAX_CAS_BYTES = 10 * 1024 * 1024


class InvestorIn(BaseModel):
    display_name: str = Field(default="Investor", max_length=120)
    pan: str | None = None
    tax_slab_pct: float | None = Field(default=None, ge=0, le=42.744)


def investor_out(inv: Investor, db: Session) -> dict:
    risk = latest_risk_profile(db, inv.id)
    return {
        "id": inv.id,
        "display_name": inv.display_name,
        "pan_masked": inv.pan_masked,
        "is_demo": inv.is_demo,
        "tax_slab_pct": inv.tax_slab_pct,
        "risk_profile": {"bucket": risk.bucket, "label": BUCKET_LABELS[risk.bucket], "horizon_years": risk.horizon_years,
                         "valid_till": risk.valid_till.isoformat()} if risk else None,
        "created_at": inv.created_at.isoformat(),
    }


@router.post("/investors")
def create_investor(body: InvestorIn, db: Session = Depends(get_db)):
    inv = Investor(display_name=body.display_name, tax_slab_pct=body.tax_slab_pct)
    if body.pan:
        try:
            pan = normalise_pan(body.pan)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if db.scalar(select(Investor).where(Investor.pan_hash == hash_pan(pan))):
            raise HTTPException(409, "An investor with this PAN already exists")
        inv.pan_enc, inv.pan_hash, inv.pan_masked = encrypt_pan(pan), hash_pan(pan), mask_pan(pan)
    db.add(inv)
    db.commit()
    return investor_out(inv, db)


@router.get("/investors")
def list_investors(db: Session = Depends(get_db)):
    """Dev convenience until OTP auth lands (phase 2)."""
    return [investor_out(i, db) for i in db.scalars(select(Investor).order_by(Investor.created_at.desc()).limit(50))]


@router.get("/investors/{investor_id}")
def get_investor_route(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    return investor_out(inv, db)


@router.post("/investors/demo")
def create_demo(db: Session = Depends(get_db)):
    try:
        inv = create_demo_investor(db)
    except DemoDataMissing as exc:
        raise HTTPException(503, str(exc)) from exc
    for purpose in ("import_analysis", "ai_processing"):
        db.add(Consent(investor_id=inv.id, purpose=purpose, notice_version=NOTICE_VERSION))
    db.commit()
    return investor_out(inv, db)


class ConsentIn(BaseModel):
    purposes: list[str]


@router.post("/investors/{investor_id}/consents")
def grant_consents(body: ConsentIn, inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    allowed = {"import_analysis", "ai_processing", "marketing"}
    bad = set(body.purposes) - allowed
    if bad:
        raise HTTPException(422, f"Unknown purposes: {sorted(bad)}")
    for p in body.purposes:
        db.add(Consent(investor_id=inv.id, purpose=p, notice_version=NOTICE_VERSION))
    db.commit()
    return {"granted": body.purposes, "notice_version": NOTICE_VERSION}


@router.get("/risk/questionnaire")
def questionnaire():
    return {"version": QUESTIONNAIRE_VERSION,
            "questions": [{"id": q["id"], "text": q["text"], "options": [{"id": o[0], "label": o[1]} for o in q["options"]]} for q in QUESTIONS]}


class RiskIn(BaseModel):
    answers: dict[str, str]


@router.post("/investors/{investor_id}/risk-profile")
def submit_risk_profile(body: RiskIn, inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    try:
        r = score_answers(body.answers)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    rp = RiskProfile(investor_id=inv.id, questionnaire_version=QUESTIONNAIRE_VERSION, answers=body.answers, score=r.score,
                     bucket=r.bucket, horizon_years=r.horizon_years, valid_till=date.today() + timedelta(days=365))
    db.add(rp)
    inv.risk_bucket = r.bucket
    db.commit()
    t = TEMPLATES[r.bucket]
    return {"bucket": r.bucket, "label": BUCKET_LABELS[r.bucket], "score": r.score, "capacity_score": r.capacity_score,
            "willingness_score": r.willingness_score, "horizon_years": r.horizon_years, "reasons": r.reasons,
            "target_allocation_pct": t.target, "bands_pct": {k: list(v) for k, v in t.bands.items()}}


@router.post("/investors/{investor_id}/imports")
async def upload_cas(file: UploadFile = File(...), password: str = Form(...), inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    data = await file.read(MAX_CAS_BYTES + 1)
    if len(data) > MAX_CAS_BYTES:
        raise HTTPException(413, "CAS files are limited to 10 MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(422, "Please upload the CAS PDF you received by email")
    try:
        parsed = parse_cas_pdf(data, password)
        job = import_cas_data(db, inv, parsed)
    except ImportRejected as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    finally:
        del data  # IMP-4: the PDF and password are never persisted
    return {"import_id": job.id, "status": job.status, "summary": job.summary, "exceptions": job.exceptions}


@router.get("/investors/{investor_id}/imports")
def list_imports(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    jobs = db.scalars(select(ImportJob).where(ImportJob.investor_id == inv.id).order_by(ImportJob.created_at.desc())).all()
    return [{"id": j.id, "source": j.source, "status": j.status, "summary": j.summary, "exceptions": j.exceptions,
             "created_at": j.created_at.isoformat()} for j in jobs]


class GoalIn(BaseModel):
    name: str = Field(max_length=80)
    target_amount: float = Field(gt=0)
    target_date: date
    current_allocated: float = Field(default=0, ge=0)


@router.get("/investors/{investor_id}/goals")
def list_goals(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    risk = latest_risk_profile(db, inv.id)
    out = []
    for g in db.scalars(select(Goal).where(Goal.investor_id == inv.id).order_by(Goal.target_date)):
        plan = goal_plan(g.target_amount, g.target_date, risk.bucket if risk else "moderate", g.current_allocated, inflation=0.06)
        out.append({"id": g.id, "name": g.name, "target_amount": g.target_amount, "target_date": g.target_date.isoformat(),
                    "current_allocated": g.current_allocated, "plan": plan})
    return out


@router.post("/investors/{investor_id}/goals")
def create_goal(body: GoalIn, inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    g = Goal(investor_id=inv.id, **body.model_dump())
    db.add(g)
    db.commit()
    return {"id": g.id}
