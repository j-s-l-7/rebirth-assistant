"""Outbound HTTP for the data loaders: one client configuration and one retry policy."""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

USER_AGENT = "ra-backend/0.1 (+mutual fund data sync)"
_sleep = time.sleep  # indirection so tests don't wait out the backoff

T = TypeVar("T")


def http_client(*, follow_redirects: bool = True) -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        timeout=httpx.Timeout(s.http_timeout_s, connect=20.0),
        follow_redirects=follow_redirects,
        headers={"User-Agent": USER_AGENT},
    )


def with_retries(what: str, fn: Callable[[], T], *, attempts: int = 4,
                 retry_on: tuple[type[BaseException], ...] = (httpx.HTTPError,)) -> T:
    """Call `fn` until it returns, backing off 2 s, 4 s, ... between attempts. When the last attempt
    also fails, raise RuntimeError naming `what` (callers report it and carry on)."""
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            log.warning("%s attempt %d failed: %s", what, attempt + 1, exc)
            if attempt < attempts - 1:
                _sleep(2 * (attempt + 1))
    raise RuntimeError(f"{what} failed after {attempts} attempts: {last}")
