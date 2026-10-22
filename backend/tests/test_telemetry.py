"""Request IDs, structured JSON logs and counters."""

import json
import logging

from fastapi.testclient import TestClient

from app import telemetry
from app.main import app


def test_every_response_gets_a_request_id(caplog):
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="ra"):
        r = client.get("/api/investors/missing-id")
    rid = r.headers["X-Request-ID"]
    assert r.status_code == 404 and len(rid) == 32
    line = next(rec for rec in caplog.records if rec.getMessage() == "http.request")
    assert line.fields["status"] == 404 and line.fields["path"] == "/api/investors/missing-id"


def test_a_well_formed_incoming_request_id_is_kept_and_a_bad_one_replaced():
    client = TestClient(app)
    assert client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"}).headers["X-Request-ID"] == "trace-abc-123"
    bad = client.get("/api/health", headers={"X-Request-ID": "x\" injected"}).headers["X-Request-ID"]
    assert bad != "x\" injected" and len(bad) == 32


def test_json_formatter_carries_request_id_and_fields_without_overriding_core_keys():
    token = telemetry.request_id_var.set("req-12345678")
    try:
        record = logging.LogRecord("ra", logging.INFO, __file__, 1, "chat.turn", None, None)
        record.fields = {"session_id": "s1", "tokens": 42, "level": "spoofed"}
        out = json.loads(telemetry.JsonFormatter().format(record))
    finally:
        telemetry.request_id_var.reset(token)
    assert out["event"] == "chat.turn" and out["request_id"] == "req-12345678"
    assert out["session_id"] == "s1" and out["tokens"] == 42 and out["level"] == "INFO"


def test_log_event_redacts_free_text_fields(caplog):
    with caplog.at_level(logging.INFO, logger="ra"):
        telemetry.log_event("chat.turn", session_id="s1", text="my PAN is ABCDE1234F", email="a@b.com")
    rec = next(r for r in caplog.records if r.getMessage() == "chat.turn")
    assert rec.fields == {"session_id": "s1", "text": "[redacted]", "email": "[redacted]"}


def test_counters():
    telemetry.reset_counters()
    telemetry.count("chat.turns")
    telemetry.count("chat.turns")
    telemetry.count("chat.cost_usd", 0.25)
    assert telemetry.counters() == {"chat.cost_usd": 0.25, "chat.turns": 2}
