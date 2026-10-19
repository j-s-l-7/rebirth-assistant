"""Admin routes: closed unless ADMIN_TOKEN is configured, then only with the matching header."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_admin_routes_are_closed_without_a_configured_token(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", None)
    assert client.get("/api/admin/jobs").status_code == 403
    assert client.post("/api/admin/metrics/recompute").status_code == 403


def test_admin_routes_need_the_matching_token(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "s3cret")
    assert client.get("/api/admin/jobs").status_code == 401
    assert client.get("/api/admin/jobs", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.get("/api/admin/jobs", headers={"X-Admin-Token": "s3cret"}).status_code == 200
