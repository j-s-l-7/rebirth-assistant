"""Portfolio X-ray: look-through exposure, overlap, what-if moves and a one-criterion overlap screen."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import portfolio_service as svc
from app.analytics.fund_lens import resolve_funds
from app.db import get_db

router = APIRouter(tags=["portfolio"])


class XrayHolding(BaseModel):
    fund: str = Field(min_length=1, max_length=120, description="Fund name or AMFI scheme code")
    amount_inr: float | None = Field(default=None, ge=0)
    units: float | None = Field(default=None, ge=0)


class Objective(BaseModel):
    max_security_pct: float | None = Field(default=None, gt=0, le=100)
    max_sector_pct: float | None = Field(default=None, gt=0, le=100)
    max_industry_pct: float | None = Field(default=None, gt=0, le=100)
    max_amc_pct: float | None = Field(default=None, gt=0, le=100)
    avoid: list[str] = []


class XrayInput(BaseModel):
    holdings: list[XrayHolding] = Field(min_length=1, max_length=25)
    objective: Objective | None = None


class Change(BaseModel):
    from_fund: str = Field(min_length=1, max_length=120)
    to_fund: str = Field(min_length=1, max_length=120)
    amount_inr: float


class SimulateInput(BaseModel):
    holdings: list[XrayHolding] = Field(min_length=1, max_length=25)
    changes: list[Change] = Field(min_length=1, max_length=10)
    objective: Objective | None = None


class ScreenInput(BaseModel):
    holdings: list[XrayHolding] = Field(min_length=1, max_length=25)
    category: str | None = None
    amount_inr: float | None = Field(default=None, gt=0)
    limit: int = Field(default=10, ge=1, le=25)


def _items(holdings: list[XrayHolding]) -> list[dict]:
    return [h.model_dump() for h in holdings]


@router.post("/portfolio/xray")
def xray_route(body: XrayInput, db: Session = Depends(get_db)):
    return svc.xray_portfolio(db, _items(body.holdings), body.objective.model_dump() if body.objective else None)


@router.post("/portfolio/simulate")
def simulate_route(body: SimulateInput, db: Session = Depends(get_db)):
    return svc.simulate(db, _items(body.holdings), [c.model_dump() for c in body.changes])


@router.post("/portfolio/screen")
def screen_route(body: ScreenInput, db: Session = Depends(get_db)):
    try:
        return svc.screen(db, _items(body.holdings), body.category, body.amount_inr, body.limit)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/funds/search")
def search_funds(q: str = Query(min_length=2, max_length=120), db: Session = Depends(get_db)):
    matches = resolve_funds(db, q, limit=100)
    return [{"name": m["name"], "amfi_code": m["amfi_code"], "category": m["category"]} for m in matches
            if m["in_peer_set"] and m["plan"] == "regular" and m["option"] == "growth"][:10]
