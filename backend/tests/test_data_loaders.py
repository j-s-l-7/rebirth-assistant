"""Loader behaviour that needs no network: holdings CSV idempotency, NAV-history gap checks."""

from datetime import date

from sqlalchemy import delete, select

from app.data import amfi
from app.data.bulk import copy_nav_points
from app.data.disclosures import load_disclosure_csv
from app.models import NavPoint, SchemeHolding


def test_loading_a_disclosure_file_twice_keeps_one_copy(db, market, tmp_path):
    code = market["flexi_cap"][0]
    path = tmp_path / "holdings.csv"
    path.write_text("amfi_code,disclosure_month,security_isin,security_name,sector,weight_pct\n"
                    f"{code},2026-09-30,INE002A01018,Reliance Industries,Energy,8.5\n"
                    f"{code},2026-09-30,INE040A01034,HDFC Bank,Financials,7.25\n")
    try:
        assert load_disclosure_csv(db, path) == 2
        assert load_disclosure_csv(db, path) == 2
        assert sorted(db.scalars(select(SchemeHolding.weight_pct).where(SchemeHolding.amfi_code == code))) == [7.25, 8.5]
    finally:
        db.execute(delete(SchemeHolding).where(SchemeHolding.amfi_code == code))
        db.commit()


def test_history_gaps_flags_a_month_that_lost_its_navs(db, market):
    march = (date(2024, 3, 1), date(2024, 3, 31))
    saved = db.execute(select(NavPoint.amfi_code, NavPoint.nav_date, NavPoint.nav).where(NavPoint.nav_date.between(*march))).all()
    assert saved
    db.execute(delete(NavPoint).where(NavPoint.nav_date.between(*march)))
    db.commit()
    try:
        gaps = amfi.history_gaps(db, years=10)
        assert march in gaps and (date(2024, 4, 1), date(2024, 4, 30)) not in gaps
    finally:
        copy_nav_points(db, [tuple(r) for r in saved])
        db.commit()
    assert march not in amfi.history_gaps(db, years=10)


def test_ensure_history_backfills_only_thin_schemes(db, market, monkeypatch):
    calls = []
    monkeypatch.setattr(amfi, "backfill_scheme_mfapi", lambda db, code: calls.append(code) or 0)
    thin = 123456789  # nothing stored
    assert amfi.ensure_history(db, [market["large_cap"][0], thin, thin], min_points=30) == [thin]
    assert calls == [thin]
