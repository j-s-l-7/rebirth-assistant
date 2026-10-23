import importlib
import zipfile
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import data_explorer
from app.data import source_files
from app.data.source_files import register_files
from app.db import Base, get_db
from app.models import Scheme, SchemeHolding, SourceFile


def _scheme(code, name, amc, nav=None):
    return Scheme(amfi_code=code, name=name, amc_name=amc, raw_category="x", category_key="large_cap", plan_type="regular",
                  option_type="growth", riskometer="high", in_universe=True, latest_nav=nav)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(source_files, "SOURCE_DIR", tmp_path / "store")
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    db = Session()
    db.add_all([_scheme(1, "A Fund", "Alpha MF", 10.0), _scheme(2, "B Fund", "Alpha MF"), _scheme(3, "C Fund", "HDFC Mutual Fund")])
    db.add(SchemeHolding(amfi_code=1, disclosure_month=date(2026, 9, 30), security_name="Foo", weight_pct=5.0))
    db.commit()
    wb = Workbook()
    wb.active.title = "S1"
    wb.active.append(["a", "b"])
    wb.active.append([1, None])
    xlsx = tmp_path / "p.xlsx"
    wb.save(xlsx)
    z = tmp_path / "all.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(xlsx, "inner.xlsx")
    app = FastAPI()
    app.include_router(data_explorer.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db, xlsx, z
    db.close()


def test_coverage_and_missing(env):
    c, db, xlsx, _ = env
    register_files(db, [str(xlsx)], "Alpha MF", "2026-09", "https://amc.example/p", "amc")
    cov = c.get("/api/data/coverage").json()
    d = {x["key"]: x for x in cov["datasets"]}
    assert cov["universe"] == 3 and d["nav"]["have"] == 1 and d["holdings"]["have"] == 1 and d["source_file"]["have"] == 1
    assert d["ter"]["have"] == 0 and d["nav"]["pct"] == 33.3
    assert cov["headline_pct"] == round(sum(x["pct"] for x in cov["datasets"]) / 7, 1)
    assert cov["reference_documents"] == {"loaded": 0, "note": "not per fund"}
    m = c.get("/api/data/coverage/missing", params={"dataset": "holdings"}).json()
    assert m["total_missing"] == 2 and {f["amfi_code"] for f in m["funds"]} == {2, 3}
    reasons = {a["amc"]: a["reason"] for a in m["by_amc"]}
    assert "BLOCKED" in reasons["HDFC Mutual Fund"] and reasons["Alpha MF"] == "not loaded yet"


def test_fund_datasets_and_404(env):
    c, *_ = env
    ds = {x["key"]: x for x in c.get("/api/data/funds/1/datasets").json()}
    assert ds["holdings"]["available"] and ds["nav"]["available"] and not ds["ter"]["available"]
    assert c.get("/api/data/funds/1/datasets/holdings").json()["rows"][0][0] == "Foo"
    r = c.get("/api/data/funds/2/datasets/holdings")
    assert r.status_code == 404 and r.json() == {"detail": "not available"}


def test_files_preview_download_catalog(env):
    c, db, xlsx, z = env
    register_files(db, [str(xlsx), str(z)], "Alpha MF", "2026-09", "https://amc.example/p", "amc")
    assert register_files(db, [str(xlsx)], "Alpha MF", "2026-09", "u", "amc")["skipped_duplicates"] == 1
    files = {f["filename"]: f for f in c.get("/api/data/funds/1/files").json()}
    x, zz = files["p.xlsx"], files["all.zip"]
    pv = c.get(f"/api/data/files/{x['id']}/preview").json()
    assert pv["sheets"] == ["S1"] and pv["rows"] == [["a", "b"], ["1"]]
    assert c.get(f"/api/data/files/{zz['id']}/preview").json()["members"][0]["name"] == "inner.xlsx"
    assert c.get(f"/api/data/files/{zz['id']}/preview", params={"member": "inner.xlsx"}).json()["rows"][0] == ["a", "b"]
    assert c.get(f"/api/data/files/{x['id']}/download").content == xlsx.read_bytes()
    assert c.get("/api/data/files/..%2f..%2fetc%2fpasswd/download").status_code == 404
    row = db.get(SourceFile, x["id"])
    row.stored_path = "../../outside"
    db.commit()
    assert c.get(f"/api/data/files/{x['id']}/download").status_code == 404
    cat = c.get("/api/data/catalog", params={"dataset": "holdings"}).json()
    assert cat["total"] == 3 and [i["available"] for i in cat["items"]] == [True, False, False]
    assert cat["items"][0]["location"] == "Local DB: scheme_portfolio (Sep 2026)" and cat["items"][0]["details"]["rows"] == 1
    assert cat["items"][2]["location"] == "Not in the library"
    assert c.get("/api/data/catalog", params={"kind": "file"}).json()["total"] == 2


def test_router_absent_when_flag_false(monkeypatch):
    from app import config

    def paths():
        config.get_settings.cache_clear()
        import app.main
        return set(importlib.reload(app.main).app.openapi()["paths"])

    monkeypatch.setenv("DATA_EXPLORER_ENABLED", "false")
    assert "/api/data/coverage" not in paths()
    monkeypatch.setenv("DATA_EXPLORER_ENABLED", "true")
    assert "/api/data/coverage" in paths()
    monkeypatch.undo()
    config.get_settings.cache_clear()
