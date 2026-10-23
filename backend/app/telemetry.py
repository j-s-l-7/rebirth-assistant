"""Structured logs, request IDs and in-process counters.

Every log line can carry the current request's ID (RequestIdMiddleware sets it, and worker threads
inherit it), and `log_event(name, **fields)` writes one structured event. With LOG_FORMAT=json each
line is a single JSON object, ready for the host's log search.

Privacy rule: log IDs, counts, timings and codes, never what a person typed or their email. Fields
whose names suggest free text are redacted by `log_event` as a backstop.

Counters (`count`, `counters`) live in this process only: they reset on restart (Render's free plan
sleeps after 15 idle minutes). The durable record is the log stream plus the chat_message rows.
"""

import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("ra")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")
# Field names that could carry what a person typed or who they are.
_REDACTED_FIELDS = frozenset({"text", "message", "content", "prompt", "question", "answer", "email",
                              "display_text", "api_content", "body"})
_CORE_KEYS = ("ts", "level", "logger", "event", "request_id")


# --------------------------------------------------------------------------- logging


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, event, request_id, then the event's fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or request_id_var.get()
        if rid:
            out["request_id"] = rid
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update({k: v for k, v in fields.items() if k not in _CORE_KEYS})
        if record.exc_info and record.exc_info[0] is not None:
            out["exc_type"] = record.exc_info[0].__name__
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable lines for local development: the same content as JSON, as key=value pairs."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        rid = getattr(record, "request_id", None) or request_id_var.get()
        fields = getattr(record, "fields", None)
        extra = " ".join(f"{k}={v}" for k, v in fields.items()) if isinstance(fields, dict) else ""
        return " ".join(p for p in (line, f"request_id={rid}" if rid else "", extra) if p)


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    """Route every logger (uvicorn's included) through one stdout handler in the chosen format."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt.lower() == "json" else TextFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_ra_handler", False):
            root.removeHandler(h)
    handler._ra_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # One line per outbound HTTP call at INFO is noise; their failures still surface as WARNING+.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """Write one structured event. Free-text-looking fields are redacted (see module docstring)."""
    clean = {k: ("[redacted]" if k in _REDACTED_FIELDS else v) for k, v in fields.items()}
    log.log(level, event, extra={"fields": clean})


# --------------------------------------------------------------------------- counters

_counters: Counter = Counter()
_counters_lock = threading.Lock()


def count(name: str, n: float = 1) -> None:
    with _counters_lock:
        _counters[name] += n


def counters() -> dict[str, float]:
    with _counters_lock:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in sorted(_counters.items())}


def reset_counters() -> None:
    with _counters_lock:
        _counters.clear()


# --------------------------------------------------------------------------- request IDs


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


class RequestIdMiddleware:
    """Gives every HTTP request an ID (a well-formed incoming X-Request-ID is kept), returns it in
    the X-Request-ID response header, makes it available to log lines (also in worker threads),
    and logs one `http.request` line per request: method, path, status and duration. Paths carry
    IDs only; query strings and bodies are never logged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, b"x-request-id")
        rid = incoming if incoming and _SAFE_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        scope.setdefault("state", {})["request_id"] = rid
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, rid)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            path = scope.get("path", "")
            quiet = path.startswith("/api/health") and status < 400  # uptime probes: debug only
            log_event("http.request", logging.DEBUG if quiet else logging.INFO, method=scope.get("method"), path=path,
                      status=status, duration_ms=round((time.perf_counter() - started) * 1000, 1))
            request_id_var.reset(token)
