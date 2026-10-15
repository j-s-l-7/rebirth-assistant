"""CAS import (PRD §8.6 IMP-1..4): parse a CAMS/KFintech CAS PDF and load it.

The PDF is parsed in memory and never written to disk; the password is never stored.
Validation runs before anything is written: if the statement's PAN doesn't match the
investor's verified PAN, the import is rejected with nothing persisted.
"""

import io
import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Folio, Holding, ImportJob, Investor, Scheme, Txn
from app.security import decrypt_pan

log = logging.getLogger(__name__)


class ImportRejected(Exception):
    """Validation failure with an investor-facing message; nothing was written."""


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _date(v) -> date | None:
    if isinstance(v, date):
        return v
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(v), fmt).date()
        except ValueError:
            continue
    return None


def parse_cas_pdf(pdf_bytes: bytes, password: str) -> dict:
    import casparser
    from casparser.exceptions import CASParseError, IncorrectPasswordError

    try:
        return casparser.read_cas_pdf(io.BytesIO(pdf_bytes), password, output="dict")
    except IncorrectPasswordError as exc:
        raise ImportRejected("That password didn't open the statement. CAS passwords are the one you set when requesting it.") from exc
    except CASParseError as exc:
        raise ImportRejected(
            "We couldn't read this file as a CAMS/KFintech CAS. Upload the original PDF from your email "
            "(re-saved or 'printed to PDF' copies can't be parsed)."
        ) from exc


def _resolve_scheme(db: Session, amfi: str | None, isin: str | None) -> Scheme | None:
    if amfi and str(amfi).isdigit():
        found = db.get(Scheme, int(amfi))
        if found:
            return found
    if isin:
        return db.scalar(select(Scheme).where((Scheme.isin_growth == isin) | (Scheme.isin_reinvest == isin)))
    return None


def import_cas_data(db: Session, investor: Investor, cas: dict, source: str = "cas_pdf") -> ImportJob:
    """Validate and persist parsed CAS data. Returns the ImportJob (status done)."""
    folios = cas.get("folios") or []
    if not folios:
        raise ImportRejected("This statement has no mutual fund folios in it.")

    # IMP-2: the statement must belong to this investor.
    if investor.pan_enc:
        verified = decrypt_pan(investor.pan_enc)
        cas_pans = {(f.get("PAN") or "").strip().upper() for f in folios} - {""}
        if cas_pans and cas_pans != {verified}:
            raise ImportRejected("The PAN on this statement doesn't match your verified PAN, so we didn't import it.")

    period = cas.get("statement_period") or {}
    job = ImportJob(
        investor_id=investor.id,
        source=source,
        status="parsing",
        parser_version=f"casparser-{_casparser_version()}",
        statement_from=_date(period.get("from") or period.get("from_")),
        statement_to=_date(period.get("to")),
    )
    db.add(job)
    db.flush()

    exceptions: list[dict] = []
    n_schemes = n_txns = 0
    total_value = 0.0
    for f in folios:
        folio_no = str(f.get("folio") or "").strip()
        amc = (f.get("amc") or "").strip()
        folio = db.scalar(select(Folio).where(Folio.investor_id == investor.id, Folio.amc_name == amc, Folio.folio_no == folio_no))
        if folio is None:
            folio = Folio(investor_id=investor.id, amc_name=amc, folio_no=folio_no, source=source)
            db.add(folio)
            db.flush()

        for s in f.get("schemes") or []:
            scheme = _resolve_scheme(db, s.get("amfi"), s.get("isin"))
            if scheme is None:
                # IMP-3: unmapped lines go to the exceptions queue.
                exceptions.append({"type": "unmapped_scheme", "folio": folio_no, "scheme": s.get("scheme"), "isin": s.get("isin")})
                continue
            val = s.get("valuation") or {}
            units = _num(s.get("close")) or 0.0
            value = _num(val.get("value")) or 0.0
            total_value += value
            close_calc = _num(s.get("close_calculated"))
            if close_calc is not None and abs(close_calc - units) > 0.001:
                exceptions.append(
                    {"type": "units_mismatch", "folio": folio_no, "scheme": scheme.name, "statement_units": units, "transaction_units": close_calc,
                     "hint": "Transactions in this CAS don't add up to the closing balance; request a CAS from inception for accurate returns and tax."}
                )

            holding = db.scalar(select(Holding).where(Holding.folio_id == folio.id, Holding.amfi_code == scheme.amfi_code))
            if holding is None:
                holding = Holding(folio_id=folio.id, amfi_code=scheme.amfi_code, units=units, as_of=_date(val.get("date")) or date.today())
                db.add(holding)
            holding.units = units
            holding.cost_value = _num(val.get("cost"))
            holding.statement_value = value
            holding.as_of = _date(val.get("date")) or holding.as_of
            n_schemes += 1

            existing = {
                (t.txn_date, t.txn_type, round(t.units or 0, 3), round(t.amount or 0, 2))
                for t in db.scalars(select(Txn).where(Txn.folio_id == folio.id, Txn.amfi_code == scheme.amfi_code))
            }
            for t in s.get("transactions") or []:
                t_date = _date(t.get("date"))
                if t_date is None:
                    continue
                t_type = str(t.get("type") or "UNKNOWN").split(".")[-1]
                amount, t_units = _num(t.get("amount")), _num(t.get("units"))
                key = (t_date, t_type, round(t_units or 0, 3), round(amount or 0, 2))
                if key in existing:
                    continue
                existing.add(key)
                db.add(
                    Txn(
                        folio_id=folio.id,
                        amfi_code=scheme.amfi_code,
                        import_job_id=job.id,
                        txn_type=t_type,
                        txn_date=t_date,
                        amount=amount,
                        units=t_units,
                        nav=_num(t.get("nav")),
                        balance=_num(t.get("balance")),
                        description=(t.get("description") or "")[:255],
                    )
                )
                n_txns += 1

    cas_type = str(cas.get("cas_type") or "").split(".")[-1]
    if cas_type == "SUMMARY":
        exceptions.append({"type": "summary_cas", "hint": "A summary CAS has no transactions, so XIRR and tax views are limited. Request a *detailed* CAS."})

    job.status = "done"
    job.summary = {
        "folios": len(folios),
        "schemes": n_schemes,
        "transactions_added": n_txns,
        "statement_value": round(total_value, 2),
        "cas_type": cas_type or None,
        "file_type": str(cas.get("file_type") or "").split(".")[-1] or None,
    }
    job.exceptions = exceptions
    db.commit()
    return job


def _casparser_version() -> str:
    try:
        import casparser

        return casparser.__version__
    except Exception:  # pragma: no cover - version is informational only
        return "unknown"
