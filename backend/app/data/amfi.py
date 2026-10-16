"""AMFI public data: daily scheme master + NAV (NAVAll.txt) and bulk NAV history.

Both files are semicolon-separated with interleaved section headers:
    Open Ended Schemes(Equity Scheme - Large Cap Fund)   <- category header
    Axis Mutual Fund                                     <- AMC header
    120465;INF846K01DP8;-;Axis Large Cap Fund;Regular Plan;Growth;61.2;14-Oct-2026
Column layouts differ between files and have changed over time, so data rows are
parsed by the header line's column names rather than fixed positions.
"""

import logging
import re
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data.bulk import copy_nav_points, upsert
from app.models import NavPoint, Scheme, SchemeDetail
from app.reference.categories import (
    BENCHMARK_PROXIES,
    EXCLUDED_NAME,
    get_category,
    normalise_category,
    normalise_option,
    normalise_plan,
)

log = logging.getLogger(__name__)

_SECTION = re.compile(r"^(Open Ended|Close Ended|Interval Fund)\s*Schemes?\s*\(", re.I)


@dataclass
class SchemeRow:
    amfi_code: int
    name: str
    amc_name: str
    raw_category: str
    open_ended: bool
    plan_raw: str
    option_raw: str
    isin_growth: str | None
    isin_reinvest: str | None
    nav: float | None
    nav_date: date | None


def _parse_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(s: str) -> float | None:
    try:
        v = float(s.replace(",", "").strip())
        return v if v > 0 else None
    except ValueError:
        return None


def _isin(s: str) -> str | None:
    s = (s or "").strip()
    return s if re.fullmatch(r"INF[A-Z0-9]{9}", s) else None


def _column_index(header: str) -> dict[str, int]:
    cols = [c.strip().lower() for c in header.split(";")]
    idx: dict[str, int] = {}
    for i, c in enumerate(cols):
        if c == "scheme code":
            idx["code"] = i
        elif c in ("scheme name", "nav name"):
            idx["name"] = i
        elif c == "plan":
            idx["plan"] = i
        elif c == "option":
            idx["option"] = i
        elif "isin" in c and ("growth" in c or "payout" in c):
            idx["isin_growth"] = i
        elif "isin" in c and "reinvest" in c:
            idx["isin_reinvest"] = i
        elif c == "net asset value":
            idx["nav"] = i
        elif c == "date":
            idx["date"] = i
    missing = {"code", "name", "nav", "date"} - idx.keys()
    if missing:
        raise ValueError(f"Unrecognised AMFI header, missing {missing}: {header!r}")
    return idx


def iter_rows(lines: Iterable[str]) -> Iterator[SchemeRow]:
    """Parse an AMFI NAV file (NAVAll or history report) line by line."""
    idx: dict[str, int] | None = None
    category, amc, open_ended = "", "", True
    for line in lines:
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        if idx is None:
            if line.lower().startswith("scheme code"):
                idx = _column_index(line)
            continue
        if ";" not in line:
            if _SECTION.match(line):
                category = line
                open_ended = line.lower().startswith("open ended")
            else:
                amc = line
            continue
        parts = line.split(";")
        code = parts[idx["code"]].strip()
        if not code.isdigit():
            continue

        def col(key: str) -> str:
            i = idx.get(key)
            return parts[i] if i is not None and i < len(parts) else ""

        yield SchemeRow(
            amfi_code=int(code),
            name=col("name").strip(),
            amc_name=amc,
            raw_category=category,
            open_ended=open_ended,
            plan_raw=col("plan").strip(),
            option_raw=col("option").strip(),
            isin_growth=_isin(col("isin_growth")),
            isin_reinvest=_isin(col("isin_reinvest")),
            nav=_parse_float(col("nav")),
            nav_date=_parse_date(col("date")),
        )


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        timeout=httpx.Timeout(s.http_timeout_s, connect=20.0),
        follow_redirects=True,
        headers={"User-Agent": "ra-backend/0.1 (+mutual fund data sync)"},
    )


def is_investable(row: SchemeRow, category_key: str, plan: str, option: str, latest_date: date | None = None) -> bool:
    """PRD §11.1 universe filter (the parts knowable from the scheme master). A scheme that
    hasn't published a NAV within 30 days of the file's latest date is dormant."""
    fresh = row.nav is not None and row.nav_date is not None and (latest_date is None or (latest_date - row.nav_date).days <= 30)
    return (
        fresh
        and row.open_ended
        and plan == "regular"
        and option == "growth"
        and category_key not in ("other", "other_debt", "fof_domestic")
        and not EXCLUDED_NAME.search(row.name)
        # ETFs trade on the exchange, not through MF order platforms (BSE StAR MF): use FoFs.
        and not (re.search(r"\bETF\b", row.name, re.I) and not re.search(r"fund of funds?|\bfof\b", row.name, re.I))
    )


def sync_scheme_master(db: Session) -> dict:
    """Download NAVAll.txt, upsert every scheme, and append today's NAV for tracked schemes."""
    s = get_settings()
    with _client() as client:
        resp = client.get(s.amfi_nav_url)
        resp.raise_for_status()
        rows = list(iter_rows(resp.text.splitlines()))

    proxy_codes = {p["amfi_code"] for p in BENCHMARK_PROXIES.values()}
    latest_date = max((r.nav_date for r in rows if r.nav_date), default=None)
    # The 2026 NAV file leaves Plan/Option blank for thousands of codes (base name only);
    # the scheme-details master's NAV name ("... - Regular Growth") fills them in.
    nav_names = dict(db.execute(select(SchemeDetail.amfi_code, SchemeDetail.nav_name)).all())
    scheme_rows, seen = [], set()
    for r in rows:
        if r.amfi_code in seen:
            continue
        seen.add(r.amfi_code)
        key = normalise_category(r.raw_category, r.name)
        label = r.name if (r.plan_raw or r.option_raw) else (nav_names.get(r.amfi_code) or r.name)
        plan, option = normalise_plan(r.plan_raw, label), normalise_option(r.option_raw, label)
        scheme_rows.append(
            {
                "amfi_code": r.amfi_code,
                "name": r.name[:255],
                "amc_name": r.amc_name[:120],
                "raw_category": r.raw_category[:160],
                "category_key": key,
                "plan_type": plan,
                "option_type": option,
                "isin_growth": r.isin_growth,
                "isin_reinvest": r.isin_reinvest,
                "latest_nav": r.nav,
                "latest_nav_date": r.nav_date,
                "riskometer": get_category(key).riskometer,
                "riskometer_source": "category_default",
                "in_universe": is_investable(r, key, plan, option, latest_date),
                "is_benchmark_proxy": r.amfi_code in proxy_codes,
            }
        )

    # AMFI sometimes lists the same plan twice under different codes (legacy plans); keep
    # one per AMC + name + plan + option in the universe - the oldest code, which has the
    # longer history.
    seen_names: set[tuple] = set()
    for r in sorted(scheme_rows, key=lambda r: r["amfi_code"]):
        if r["in_universe"]:
            key = (r["amc_name"].lower(), re.sub(r"\W+", " ", r["name"].lower()).strip(), r["plan_type"], r["option_type"])
            if key in seen_names:
                r["in_universe"] = False
            seen_names.add(key)

    update_cols = [c for c in scheme_rows[0] if c not in ("amfi_code", "riskometer", "riskometer_source")]
    upsert(db, Scheme, scheme_rows, ["amfi_code"], update_cols)

    tracked = tracked_codes(db)
    today_points = [(r["amfi_code"], r["latest_nav_date"], r["latest_nav"]) for r in scheme_rows if r["amfi_code"] in tracked and r["latest_nav"] and r["latest_nav_date"]]
    copy_nav_points(db, today_points)
    db.commit()
    return {
        "schemes": len(scheme_rows),
        "investable": sum(1 for r in scheme_rows if r["in_universe"]),
        "tracked": len(tracked),
        "nav_points_added": len(today_points),
    }


def tracked_codes(db: Session) -> set[int]:
    """Schemes whose NAV history we keep: the investable universe + benchmark proxies."""
    return set(db.scalars(select(Scheme.amfi_code).where(Scheme.in_universe.is_(True) | Scheme.is_benchmark_proxy.is_(True))))


class EmptyWindow(ValueError):
    """AMFI answered without any NAV rows (it does this transiently); retry."""


def _history_window(start: date, end: date, codes: set[int]) -> list[tuple[int, date, float]]:
    s = get_settings()
    params = {"tp": "1", "frmdt": start.strftime("%d-%b-%Y"), "todt": end.strftime("%d-%b-%Y")}
    for attempt in range(4):
        out: list[tuple[int, date, float]] = []
        rows_seen = 0
        try:
            with _client() as client, client.stream("GET", s.amfi_history_url, params=params) as resp:
                resp.raise_for_status()
                for row in iter_rows(resp.iter_lines()):
                    rows_seen += 1
                    # AMFI occasionally returns stray rows dated far outside the requested range.
                    if row.amfi_code in codes and row.nav and row.nav_date and start <= row.nav_date <= end:
                        out.append((row.amfi_code, row.nav_date, row.nav))
            # Every business month has thousands of NAVs; an empty answer is a failed download.
            if rows_seen == 0 and (end - start).days >= 3:
                raise EmptyWindow("no NAV rows in response")
            return out
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("AMFI history %s..%s attempt %d failed: %s", start, end, attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"AMFI history download failed for {start}..{end}")


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows, cur = [], start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        windows.append((cur, min(end, nxt - timedelta(days=1))))
        cur = nxt
    return windows


def _load_windows(db: Session, windows: list[tuple[date, date]], codes: set[int], workers: int, progress=None) -> dict:
    total, failed = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:  # one HTTP client per window/thread
        futures = {pool.submit(_history_window, a, b, codes): (a, b) for a, b in windows}
        for done, fut in enumerate(as_completed(futures), 1):
            a, b = futures[fut]
            try:
                points = fut.result()
            except RuntimeError:
                failed.append(f"{a}..{b}")
                points = []
            copy_nav_points(db, points)
            db.commit()
            total += len(points)
            if progress:
                progress(done, len(windows), a, b, len(points))
    return {"nav_points": total, "failed_windows": sorted(failed)}


def backfill_history(db: Session, years: int | None = None, workers: int = 4, progress=None) -> dict:
    """Load `years` of daily NAVs for tracked schemes from AMFI's history report, a month at a time.
    Failed months are reported (and can be re-run with `repair_history`), not silently skipped."""
    s = get_settings()
    years = years or s.nav_history_years
    codes = tracked_codes(db)
    end = date.today()
    start = date(end.year - years, end.month, 1)
    windows = month_windows(start, end)
    result = _load_windows(db, windows, codes, workers, progress)
    now = datetime.now(timezone.utc)
    db.query(Scheme).filter(Scheme.amfi_code.in_(codes)).update({Scheme.nav_history_synced_at: now}, synchronize_session=False)
    db.commit()
    return {"schemes": len(codes), "months": len(windows), **result}


def history_gaps(db: Session, years: int | None = None) -> list[tuple[date, date]]:
    """Months whose stored NAV count is far below their neighbours' (a missed or partial download)."""
    s = get_settings()
    years = years or s.nav_history_years
    end = date.today()
    start = date(end.year - years, end.month, 1)
    counts = {}
    for a, b in month_windows(start, end):
        counts[(a, b)] = db.query(NavPoint).filter(NavPoint.nav_date >= a, NavPoint.nav_date <= b).count()
    keys = list(counts)
    gaps = []
    for i, k in enumerate(keys):
        neighbours = [counts[keys[j]] for j in (i - 1, i + 1) if 0 <= j < len(keys)]
        typical = max(neighbours) if neighbours else 0
        partial_month = k[1] >= end - timedelta(days=1)  # the current month is naturally short
        if typical and counts[k] < 0.5 * typical and not partial_month:
            gaps.append(k)
    return gaps


def repair_history(db: Session, workers: int = 4, progress=None) -> dict:
    """Re-download months flagged by `history_gaps`, and drop stray points outside the window."""
    s = get_settings()
    cutoff = date(date.today().year - s.nav_history_years, date.today().month, 1)
    stray = db.query(NavPoint).filter(NavPoint.nav_date < cutoff).delete(synchronize_session=False)
    db.commit()
    gaps = history_gaps(db)
    result = _load_windows(db, gaps, tracked_codes(db), workers, progress) if gaps else {"nav_points": 0, "failed_windows": []}
    return {"stray_points_removed": stray, "repaired_months": [f"{a}..{b}" for a, b in gaps], **result}


def backfill_scheme_mfapi(db: Session, amfi_code: int) -> int:
    """Per-scheme fallback (e.g. a held Direct plan outside the universe) via mfapi.in."""
    s = get_settings()
    with _client() as client:
        resp = client.get(f"{s.mfapi_base_url}/{amfi_code}")
        resp.raise_for_status()
        data = resp.json().get("data") or []
    cutoff = date.today() - timedelta(days=365 * (s.nav_history_years + 1))
    points = []
    for p in data:
        d, n = _parse_date(p.get("date", "")), _parse_float(p.get("nav", ""))
        if d and n and d >= cutoff:
            points.append((amfi_code, d, n))
    copy_nav_points(db, points)
    db.query(Scheme).filter(Scheme.amfi_code == amfi_code).update({Scheme.nav_history_synced_at: datetime.now(timezone.utc)})
    db.commit()
    return len(points)


def ensure_history(db: Session, codes: Iterable[int], min_points: int = 30) -> list[int]:
    """Backfill any scheme with too little stored history (best-effort, network permitting)."""
    filled = []
    for code in set(codes):
        n = db.query(NavPoint).filter(NavPoint.amfi_code == code).count()
        if n < min_points:
            try:
                backfill_scheme_mfapi(db, code)
                filled.append(code)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("NAV backfill for %s failed: %s", code, exc)
    return filled
