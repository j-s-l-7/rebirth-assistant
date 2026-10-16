"""Integration of the Phase-1 feeds with ranking and the universe (models only, offline)."""

from datetime import date

from app.analytics import performance
from app.analytics.navbook import NavBook
from app.data.universe import MIN_AUM_CR, refine_universe
from app.models import Scheme, SchemeLink, SchemePerformance, SchemeTer

AS_OF = date(2026, 10, 14)


def test_expense_ratio_is_a_ranking_factor(db, market):
    peers = market["large_cap"]
    for i, code in enumerate(peers):
        key = f"NSDL/LC/{i}"
        db.merge(SchemeLink(amfi_code=code, source="ter", external_key=key, method="exact", score=100.0))
        db.add(SchemeTer(nsdl_code=key, scheme_name=f"Large {i}", effective_from=date(2026, 9, 1), r_ter=0.8 + 0.3 * i, d_ter=0.3))
    db.commit()

    ranked = performance.rank_category(db, NavBook(db), "large_cap", AS_OF)
    cheap, dear = ranked[peers[0]], ranked[peers[-1]]
    assert cheap["ter_regular"] == 0.8 and "cost_ter" not in cheap["factors_unavailable"]
    assert cheap["factor_percentiles"]["cost"] > dear["factor_percentiles"]["cost"]

    stats = performance.latest_category_stats(db, "large_cap")
    assert stats["peer_factors"]["ter_regular"]  # cached so out-of-universe funds get a cost percentile too


def test_refine_universe_applies_min_aum_and_official_riskometer(db, market):
    small, big = market["mid_cap"][0], market["mid_cap"][1]
    for code, aum, risk in ((small, MIN_AUM_CR / 2, "very_high"), (big, MIN_AUM_CR * 4, "high")):
        key = f"perf-{code}"
        db.merge(SchemeLink(amfi_code=code, source="performance", external_key=key, method="exact", score=100.0))
        db.add(SchemePerformance(external_key=key, plan="regular", amfi_code=code, scheme_name=key, as_of=AS_OF, aum_cr=aum, riskometer=risk, raw={}))
    db.commit()

    out = refine_universe(db)
    assert out["applied"] and out["below_min_aum_removed"] == 1
    db.expire_all()
    assert db.get(Scheme, small).in_universe is False
    assert db.get(Scheme, big).in_universe is True
    assert (db.get(Scheme, big).riskometer, db.get(Scheme, big).riskometer_source) == ("high", "amfi")
