"""AMC monthly portfolio disclosures (stock-level holdings) for the fund lens's diversification view."""

import csv
from datetime import date
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import SchemeHolding


def load_disclosure_csv(db: Session, path: Path) -> int:
    """CSV columns: amfi_code, disclosure_month (YYYY-MM-DD), security_isin, security_name, sector, weight_pct.

    A file holds complete portfolios: each (amfi_code, disclosure_month) in it replaces what is stored for that
    pair, so loading the same file twice keeps one copy."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [SchemeHolding(amfi_code=int(r["amfi_code"]), disclosure_month=date.fromisoformat(r["disclosure_month"]),
                              security_isin=r.get("security_isin") or None, security_name=r["security_name"],
                              sector=r.get("sector") or None, weight_pct=float(r["weight_pct"])) for r in csv.DictReader(fh)]
    for code, month in {(h.amfi_code, h.disclosure_month) for h in rows}:
        db.execute(delete(SchemeHolding).where(SchemeHolding.amfi_code == code, SchemeHolding.disclosure_month == month))
    db.add_all(rows)
    db.commit()
    return len(rows)
