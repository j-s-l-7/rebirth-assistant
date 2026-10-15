"""Demo investor built from real AMFI NAVs: steady SIPs with a couple of misses, a 2021
sector bet partly redeemed, small-cap exposure above a moderately-aggressive profile,
idle liquid cash and no gold - so every score dimension and engine rule has something to find.
"""

import zlib
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.analytics.lots import build_lots
from app.analytics.navbook import NavBook
from app.data.amfi import ensure_history
from app.models import Folio, Goal, Holding, ImportJob, Investor, RiskProfile, Scheme, Txn
from app.reference.risk import QUESTIONNAIRE_VERSION, score_answers
from app.reference.tax import add_months

# Regular plan, Growth option schemes: (amfi_code, monthly amount, first instalment, skipped months)
SIPS = [
    (122640, 10_000, date(2021, 1, 5), {(2024, 5)}),  # Parag Parikh Flexi Cap
    (101762, 5_000, date(2022, 6, 10), set()),  # HDFC Flexi Cap
    (108466, 5_000, date(2023, 4, 12), set()),  # ICICI Prudential Large Cap
    (105758, 5_000, date(2022, 1, 7), {(2024, 5), (2024, 6)}),  # HDFC Mid Cap
    (113177, 3_000, date(2023, 1, 20), set()),  # Nippon India Small Cap
]
LUMPSUMS = [
    (108466, date(2021, 3, 15), 200_000),
    (113177, date(2021, 10, 18), 150_000),
    (135784, date(2022, 1, 25), 50_000),  # Mirae Asset ELSS Tax Saver
    (135784, date(2023, 2, 10), 50_000),
    (135784, date(2024, 1, 22), 50_000),
    (135784, date(2025, 2, 14), 50_000),
    (100363, date(2021, 12, 13), 100_000),  # ICICI Prudential Technology
    (100868, date(2025, 2, 3), 300_000),  # HDFC Liquid
]
REDEMPTIONS = [(100363, date(2025, 8, 18), 30_000)]
DEMO_CODES = sorted({c for c, *_ in SIPS} | {c for c, *_ in LUMPSUMS})
DEMO_ANSWERS = {"age": "30_40", "horizon": "5_10", "income": "very", "emergency": "3_6", "dependents": "1_2",
                "drawdown": "hold", "goal": "growth", "experience": "gt5"}


class DemoDataMissing(RuntimeError):
    pass


def create_demo_investor(db: Session, display_name: str = "Demo Investor", fetch_missing: bool = True) -> Investor:
    missing = [c for c in DEMO_CODES if db.get(Scheme, c) is None]
    if missing:
        raise DemoDataMissing(f"Scheme master not loaded (missing {missing}). Run: python -m app.cli sync-schemes")
    if fetch_missing:
        ensure_history(db, DEMO_CODES, min_points=500)
    nb = NavBook(db)
    nb.preload(set(DEMO_CODES))
    latest = [nb.latest(c) for c in DEMO_CODES]
    if any(x is None for x in latest):
        raise DemoDataMissing("NAV history unavailable for demo schemes")
    as_of = min(d for d, _ in latest)

    inv = Investor(display_name=display_name, is_demo=True, tax_slab_pct=30.0)
    db.add(inv)
    db.flush()
    job = ImportJob(investor_id=inv.id, source="demo", status="done", parser_version="demo-v1")
    db.add(job)
    db.flush()

    folios: dict[str, Folio] = {}
    txns: dict[int, list[Txn]] = {}

    def folio_for(code: int) -> Folio:
        amc = db.get(Scheme, code).amc_name
        if amc not in folios:
            f = Folio(investor_id=inv.id, amc_name=amc, folio_no=f"DEMO{len(folios) + 1:03d}/{zlib.crc32(amc.encode()) % 90 + 10}", source="demo")
            db.add(f)
            db.flush()
            folios[amc] = f
        return folios[amc]

    def buy(code: int, d: date, amount: float, kind: str) -> None:
        hit = nb.nav_on_or_after(code, d)
        if hit is None or hit[0] > as_of:
            return
        nav_d, nav = hit
        t = Txn(folio_id=folio_for(code).id, amfi_code=code, import_job_id=job.id, txn_type=kind, txn_date=nav_d,
                amount=amount, units=round(amount / nav, 3), nav=nav, description=f"{'SIP ' if kind == 'PURCHASE_SIP' else ''}Purchase")
        txns.setdefault(code, []).append(t)

    for code, amount, start, skips in SIPS:
        d = start
        while d <= as_of:
            if (d.year, d.month) not in skips:
                buy(code, d, amount, "PURCHASE_SIP")
            d = add_months(d, 1)
    for code, d, amount in LUMPSUMS:
        buy(code, d, amount, "PURCHASE")
    for code, d, amount in REDEMPTIONS:
        hit = nb.nav_on_or_after(code, d)
        if hit:
            nav_d, nav = hit
            txns.setdefault(code, []).append(Txn(folio_id=folio_for(code).id, amfi_code=code, import_job_id=job.id, txn_type="REDEMPTION",
                                                 txn_date=nav_d, amount=-amount, units=-round(amount / nav, 3), nav=nav, description="Redemption"))

    for code, ts in txns.items():
        ts.sort(key=lambda t: t.txn_date)
        bal = 0.0
        for t in ts:
            bal += t.units
            t.balance = round(bal, 3)
            db.add(t)
        scheme = db.get(Scheme, code)
        book = build_lots(ts, scheme.category_key)
        nav = nb.latest(code)[1]
        db.add(Holding(folio_id=folio_for(code).id, amfi_code=code, units=round(bal, 3), cost_value=round(book.invested, 2),
                       statement_value=round(bal * nav, 2), as_of=as_of))

    result = score_answers(DEMO_ANSWERS)
    db.add(RiskProfile(investor_id=inv.id, questionnaire_version=QUESTIONNAIRE_VERSION, answers=DEMO_ANSWERS, score=result.score,
                       bucket=result.bucket, horizon_years=result.horizon_years, valid_till=date.today() + timedelta(days=365)))
    inv.risk_bucket = result.bucket
    db.add(Goal(investor_id=inv.id, name="Home down payment", target_amount=2_500_000, target_date=date(as_of.year + 5, 3, 31)))
    job.summary = {"folios": len(folios), "schemes": len(txns), "transactions_added": sum(len(v) for v in txns.values()), "demo": True}
    job.exceptions = []
    db.commit()
    return inv
