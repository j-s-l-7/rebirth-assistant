"""Chat owners. Until sign-in ships, the chat page creates a guest investor per browser."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_investor
from app.db import get_db
from app.models import Investor

router = APIRouter(tags=["investors"])


class InvestorIn(BaseModel):
    display_name: str = Field(default="Guest", max_length=120)


def investor_out(inv: Investor) -> dict:
    return {"id": inv.id, "display_name": inv.display_name, "created_at": inv.created_at.isoformat()}


@router.post("/investors")
def create_investor(body: InvestorIn, db: Session = Depends(get_db)):
    inv = Investor(display_name=body.display_name)
    db.add(inv)
    db.commit()
    return investor_out(inv)


@router.get("/investors/{investor_id}")
def get_investor_route(inv: Investor = Depends(get_investor)):
    return investor_out(inv)
