"""AMC monthly portfolio disclosures (stock-level holdings) for the fund lens's diversification view."""

import csv
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import SchemeHolding


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
