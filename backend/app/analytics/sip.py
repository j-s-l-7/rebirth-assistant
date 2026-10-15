"""SIP detection from transaction history (CAS has no explicit mandate data)."""

from dataclasses import dataclass
from datetime import date, timedelta

from app.reference.tax import add_months

PURCHASE_TYPES = {"PURCHASE", "PURCHASE_SIP"}


@dataclass
class SipInfo:
    amount: float
    day_of_month: int
    first_date: date
    last_date: date
    instalments_12m: int
    missed_12m: int
    status: str  # active | missed_latest | stopped
    next_due: date | None

    def as_dict(self) -> dict:
        return {
            "amount": round(self.amount, 2),
            "day_of_month": self.day_of_month,
            "first_date": self.first_date.isoformat(),
            "last_date": self.last_date.isoformat(),
            "instalments_last_12m": self.instalments_12m,
            "missed_last_12m": self.missed_12m,
            "status": self.status,
            "next_due": self.next_due.isoformat() if self.next_due else None,
        }


def _month_key(d: date) -> tuple[int, int]:
    return d.year, d.month


def detect_sip(txns: list, as_of: date) -> SipInfo | None:
    purchases = sorted((t for t in txns if t.txn_type in PURCHASE_TYPES and (t.units or 0) > 0 and t.amount), key=lambda t: t.txn_date)
    labelled = [t for t in purchases if t.txn_type == "PURCHASE_SIP"]
    stream = labelled
    if not stream:
        # Unlabelled statements: >=4 purchases in distinct months within the last 6 months.
        recent = [t for t in purchases if t.txn_date >= as_of - timedelta(days=190)]
        if len({_month_key(t.txn_date) for t in recent}) >= 4:
            stream = recent
    if not stream:
        return None

    last = stream[-1]
    # The 12 most recent calendar months in which an instalment was due. The current month
    # counts only once its usual date (+7 days' grace) has passed.
    due_this_month = as_of.day >= min(28, last.txn_date.day) + 7
    newest = date(as_of.year, as_of.month, 1) if due_this_month else add_months(date(as_of.year, as_of.month, 1), -1)
    first_month = date(stream[0].txn_date.year, stream[0].txn_date.month, 1)
    expected = [_month_key(m) for m in (add_months(newest, -i) for i in range(12)) if m >= first_month]
    months_with = {_month_key(t.txn_date) for t in stream} & set(expected)
    missed = sum(1 for m in expected if m not in months_with)

    gap = (as_of - last.txn_date).days
    status = "active" if gap <= 40 else "missed_latest" if gap <= 75 else "stopped"
    next_due = add_months(last.txn_date, 1) if status != "stopped" else None
    return SipInfo(
        amount=float(abs(last.amount)),
        day_of_month=last.txn_date.day,
        first_date=stream[0].txn_date,
        last_date=last.txn_date,
        instalments_12m=len(months_with),
        missed_12m=missed,
        status=status,
        next_due=next_due,
    )


def months_invested(all_txns: list, as_of: date, months: int = 12) -> int:
    """Distinct calendar months with a purchase, over the `months` calendar months ending with as_of's."""
    start = add_months(date(as_of.year, as_of.month, 1), -(months - 1))
    return len({_month_key(t.txn_date) for t in all_txns if t.txn_type in PURCHASE_TYPES and (t.units or 0) > 0 and start <= t.txn_date <= as_of})
