"""End-to-end over the synthetic market: CAS import -> analysis -> score -> engine -> insights."""

from datetime import date, timedelta

import pytest

from app.ingest.cas import ImportRejected, import_cas_data
from app.insights.nudges import nudges
from app.insights.tax import tax_summary
from app.models import Investor, RiskProfile
from app.reco.engine import run_recommendations
from app.reference.risk import QUESTIONNAIRE_VERSION
from app.security import encrypt_pan, hash_pan, mask_pan
from app.services.analysis import analyse
from tests.synthetic import cas_dict


def _investor(db, pan="ABCDE1234F", bucket="moderate"):
    inv = Investor(display_name="Test", pan_enc=encrypt_pan(pan), pan_hash=hash_pan(pan), pan_masked=mask_pan(pan))
    db.add(inv)
    db.commit()
    if bucket:
        db.add(RiskProfile(investor_id=inv.id, questionnaire_version=QUESTIONNAIRE_VERSION, answers={}, score=55, bucket=bucket,
                           horizon_years=7, valid_till=date.today() + timedelta(days=365)))
        db.commit()
    return inv


def test_pan_mismatch_blocks_import(db, market):
    inv = _investor(db, pan="ABCDE1234F")
    with pytest.raises(ImportRejected):
        import_cas_data(db, inv, cas_dict(market, pan="ZZZZZ9999Z"))
    db.rollback()
    assert not inv.folios


def test_full_analysis_and_recommendations(db, market):
    inv = _investor(db, pan="PQRST6789K")
    job = import_cas_data(db, inv, cas_dict(market, pan="PQRST6789K"))
    assert job.status == "done" and job.summary["schemes"] == 7 and job.exceptions == []

    a = analyse(db, inv)
    ov = a.pv.overview()
    assert len(a.pv.holdings) == 7
    assert ov["current_value"] > ov["invested"] > 0
    assert ov["xirr_pct"] is not None
    assert abs(sum(ov["allocation_pct"].values()) - 100) < 0.5
    assert ov["cash_like_pct"] > 0

    score = a.score
    assert [d["key"] for d in score["dimensions"]] == ["discipline", "balance", "risk_fit", "diversification", "performance", "efficiency"]
    assert 0 <= score["score"] <= 100
    risk_fit = next(d for d in score["dimensions"] if d["key"] == "risk_fit")
    assert risk_fit["score"] < 100  # small cap doesn't fit a moderate profile
    assert any("Small Cap" in m["reason"] for m in risk_fit["inputs"]["misfits"])

    run = run_recommendations(db, a)
    cards = [i.card for i in run.items]
    assert cards, "engine should find something in this portfolio"
    actions = {(c["finding"], c["action"]) for c in cards}
    assert ("risk_mismatch", "SWITCH") in actions
    assert any(c["finding"] in ("missing_asset", "drift") for c in cards)  # no gold in the book
    for c in cards:
        if c["to_scheme"]:
            assert c["suitability"]["pass"] is True  # REC-6: no card fails the gate
        assert c["euin"] and c["expires_at"] and "mf_market_risk" in c["disclosures"]

    t = tax_summary(a)
    assert t["equity_ltcg_exemption"]["limit"] == 125000
    assert set(t["unrealised_if_sold_today"]) == {"short_term_gain", "long_term_gain", "slab_taxed_gain"}
    assert isinstance(nudges(a), list)


def test_no_recommendations_without_risk_profile(db, market):
    inv = _investor(db, pan="LMNOP4321Q", bucket=None)
    import_cas_data(db, inv, cas_dict(market, pan="LMNOP4321Q"))
    run = run_recommendations(db, analyse(db, inv))
    assert [i.card["action"] for i in run.items] == ["COMPLETE_RISK_PROFILE"]
