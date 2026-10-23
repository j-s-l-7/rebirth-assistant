import hmac

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
    """Admin routes need ADMIN_TOKEN on the server and the same value in X-Admin-Token. With no token
    configured they stay closed: run syncs with the CLI instead."""
    token = get_settings().admin_token
    if not token:
        raise HTTPException(403, "Admin API disabled: set ADMIN_TOKEN on the server")
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), token.encode()):
        raise HTTPException(401, "Admin token required")
