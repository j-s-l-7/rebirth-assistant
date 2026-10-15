from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Investor


def get_investor(investor_id: str, db: Session = Depends(get_db)) -> Investor:
    inv = db.get(Investor, investor_id)
    if inv is None:
        raise HTTPException(404, "Investor not found")
    return inv


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    token = get_settings().admin_token
    if token and x_admin_token != token:
        raise HTTPException(401, "Admin token required")
