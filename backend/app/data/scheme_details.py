"""AMFI scheme-details master (DownloadSchemeData_Po.aspx?mf=0): every scheme AMFI knows, 16k+ rows.

A single CSV (CRLF, ~4 MB, UTF-8) with 10 columns:
    AMC, Code, Scheme Name, Scheme Type, Scheme Category, Scheme NAV Name,
    Scheme Minimum Amount, Launch Date, " Closure Date", "ISIN Div Payout/ ISIN GrowthISIN Div Reinvestment"
It is a superset of NAVAll: it also lists matured / merged / closed-ended codes.

Quirks (all handled below; the untouched source row is kept in `SchemeDetail.raw`):
- " Closure Date" has a leading space in the header, so columns are looked up by stripped, lower-cased name.
- The last header merges two ISIN columns and the cell holds up to two ISINs glued together
  (`INF209K01157INF209K01CE5`), sometimes with junk: a trailing "-", "NA", "Redeemed", an exchange
  ticker, or space-separated pairs. ISINs are therefore extracted with a regex, never sliced.
- "Scheme Minimum Amount" is free text, truncated at 50 characters by the source ("Rs. 5000 and in
  multiples of Re. 1 thereafter", "REFER SID", "Retail Rs.5000 Institutional Rs.1 Crore"). `min_amount` is
  the first rupee amount in it; the full text stays in `raw`.
- "Closure Date" is the NFO closing date (days to weeks after launch), not a maturity date; blank for 76%.
- 01-Jan-1900 is a placeholder for "unknown" (20 legacy rows) and is stored as NULL.
- A name containing literal double quotes appears unquoted (`... YEARLY "A"-Dividend Plan`); csv copes.
"""

import csv
import io
import logging
import re
import time
from datetime import date, datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.amfi import _client, _parse_date
from app.data.bulk import upsert
from app.models import Scheme, SchemeDetail

log = logging.getLogger(__name__)

SCHEME_DETAILS_URL = "https://portal.amfiindia.com/DownloadSchemeData_Po.aspx?mf=0"

_ISIN = re.compile(r"INF[A-Z0-9]{9}")
# A number not glued to preceding letters ("NF1- Rs. 5000" skips the 1) unless they are "Rs" ("Rs5000");
# "5 000" / "1 00000" are thousands typed with a space; "1)Growth-..." is a list marker, not an amount.
_AMOUNT = re.compile(
    r"(?:(?<![A-Za-z0-9,])|(?<=[Rr][Ss]))(\d{1,2}\s\d{3,5}\b|\d[\d,]*(?:\.\d+)?)(?![\d,.]*\))(?:\s*(lakhs?|lacs?|crores?|crs?)\b)?",
    re.I,
)
_MULTIPLIER = {"l": 1e5, "c": 1e7}  # lakh/lac -> 1e5, crore/cr -> 1e7 (keyed by first letter)
_UNKNOWN_DATE = date(1900, 1, 1)

# Header (stripped, lower-cased) -> field. The merged ISIN header is matched by prefix.
_COLUMNS = {
    "amc": "amc",
    "code": "code",
    "scheme name": "base_name",
    "scheme type": "scheme_type",
    "scheme category": "category",
    "scheme nav name": "nav_name",
    "scheme minimum amount": "min_amount",
    "launch date": "launch",
    "closure date": "closure",
}


def split_isins(cell: str) -> list[str]:
    """All ISINs in a cell, in source order (payout/growth first, reinvestment second)."""
    return _ISIN.findall(cell or "")


def parse_min_amount(text: str) -> float | None:
    """First rupee amount in AMFI's free-text minimum investment; None when there is none."""
    s = (text or "").strip()
    if re.fullmatch(r"[\d,]+(?:\.\d+)?", s):
        v = float(s.replace(",", ""))
        return v if v > 0 else None
    for m in _AMOUNT.finditer(s):
        if re.match(r"\s*units?\b", s[m.end():], re.I):  # "round lot of 1 unit" is not rupees
            continue
        v = float(re.sub(r"[\s,]", "", m.group(1))) * _MULTIPLIER.get((m.group(2) or " ")[0].lower(), 1)
        return v if v > 0 else None
    return None


def _date(s: str) -> date | None:
    d = _parse_date(s or "")
    return None if d == _UNKNOWN_DATE else d


def _column_map(header: list[str]) -> dict[str, int]:
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        key = h.strip().lower()
        if key in _COLUMNS:
            idx[_COLUMNS[key]] = i
        elif key.startswith("isin"):
            idx["isin"] = i
    missing = {"code", "base_name", "nav_name"} - idx.keys()
    if missing:
        raise ValueError(f"Unrecognised scheme-details header, missing {sorted(missing)}: {header!r}")
    return idx


def parse_scheme_details(text: str) -> list[dict]:
    """CSV text -> one dict per scheme row, keyed like `SchemeDetail` (minus updated_at).

    Rows without a numeric code or with the wrong number of cells are skipped (logged), not guessed at.
    """
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = next(reader, None)
    if header is None:
        return []
    idx = _column_map(header)

    def cell(row: list[str], key: str) -> str:
        i = idx.get(key)
        return row[i].strip() if i is not None else ""

    out = []
    for n, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(header) or not cell(row, "code").isdigit():
            log.warning("scheme details line %d skipped: %r", n, row[:3])
            continue
        out.append(
            {
                "amfi_code": int(cell(row, "code")),
                "amc_full_name": cell(row, "amc")[:160] or None,
                "scheme_base_name": cell(row, "base_name")[:255] or None,
                "scheme_type": cell(row, "scheme_type")[:40] or None,
                "scheme_category": cell(row, "category")[:160] or None,
                "nav_name": cell(row, "nav_name")[:255] or None,
                "min_amount": parse_min_amount(cell(row, "min_amount")),
                "launch_date": _date(cell(row, "launch")),
                "closure_date": _date(cell(row, "closure")),
                "isins": split_isins(row[idx["isin"]]) if "isin" in idx else [],
                "raw": dict(zip(header, row)),
            }
        )
    return out


def fetch_scheme_details(url: str = SCHEME_DETAILS_URL, attempts: int = 4) -> str:
    for attempt in range(attempts):
        try:
            with _client() as client:
                resp = client.get(url)
                resp.raise_for_status()
            try:
                return resp.content.decode("utf-8-sig")
            except UnicodeDecodeError:
                return resp.content.decode("cp1252")
        except httpx.HTTPError as exc:
            log.warning("AMFI scheme details attempt %d failed: %s", attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("AMFI scheme details download failed")


def sync_scheme_details(db: Session) -> dict:
    """Download the master and upsert every row into `scheme_detail` (idempotent)."""
    rows = parse_scheme_details(fetch_scheme_details())
    if not rows:
        raise RuntimeError("AMFI scheme details came back empty; refusing to overwrite")
    by_code = {r["amfi_code"]: r for r in rows}  # a repeated code keeps its last row
    now = datetime.now(timezone.utc)
    batch = [{**r, "updated_at": now} for r in by_code.values()]
    existing = set(db.scalars(select(SchemeDetail.amfi_code)))
    in_master = set(db.scalars(select(Scheme.amfi_code)))
    update_cols = [c for c in batch[0] if c != "amfi_code"]
    upsert(db, SchemeDetail, batch, ["amfi_code"], update_cols)
    db.commit()
    return {
        "rows": len(rows),
        "duplicate_codes": len(rows) - len(by_code),
        "new": len(by_code.keys() - existing),
        "updated": len(by_code.keys() & existing),
        "in_scheme_master": len(by_code.keys() & in_master),
        "details_only": len(by_code.keys() - in_master),
        "with_isin": sum(1 for r in by_code.values() if r["isins"]),
        "with_min_amount": sum(1 for r in by_code.values() if r["min_amount"] is not None),
        "with_launch_date": sum(1 for r in by_code.values() if r["launch_date"]),
    }
