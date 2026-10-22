from datetime import date

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.data.push_holdings import push_holdings
from app.models import Base, Scheme, SchemeHolding


def _engine(codes):
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    with Session(e) as s, s.begin():
        for c in codes:
            s.add(Scheme(amfi_code=c, name=f"SBI Fund {c}", amc_name="SBI Mutual Fund", raw_category="x", category_key="large_cap",
                         plan_type="regular", option_type="growth", riskometer="very_high"))
    return e


def _holding(code, month=date(2026, 8, 31)):
    return SchemeHolding(amfi_code=code, disclosure_month=month, security_isin="INE002A01018", security_name="Reliance",
                         weight_pct=5.0, source_url="/Users/someone/file.xlsx")


def test_push_dry_run_apply_and_idempotent():
    src_engine, dst = _engine([1, 2]), _engine([1])
    with Session(src_engine) as src, src.begin():
        src.add_all([_holding(1), _holding(2)])
    with Session(src_engine) as src:
        dry = push_holdings(src, dst)
        assert dry["rows"] == 1 and dry["rows_for_funds_not_in_target"] == 1 and dry["applied"] is False
        with Session(dst) as d:
            assert d.scalar(select(func.count()).select_from(SchemeHolding)) == 0
        for _ in range(2):  # a re-run replaces, never duplicates
            assert push_holdings(src, dst, apply=True)["applied"] is True
    with Session(dst) as d:
        rows = d.scalars(select(SchemeHolding)).all()
        assert len(rows) == 1 and rows[0].amfi_code == 1
        assert rows[0].source_url is not None and "/Users/" not in rows[0].source_url
