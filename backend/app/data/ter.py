"""AMFI total expense ratio (TER) disclosure: one row per scheme per day, stored as change points.

Source: https://www.amfiindia.com/api/ (the endpoints behind the TER page)
    populate-ter-month?year=YYYY-YYYY      months with data. Each financial year answers with an overlapping,
                                           ragged window (strays, gaps), so the answers are unioned.
    populate-te-rdata-revised?MF_ID=All&Month=MM-YYYY&strCat=-1&strType=-1&page=1&pageSize=N[&excel=true]

What the data endpoint really does:
- JSON silently caps pageSize at 100 (meta.pageSize says so) and takes ~5 s per request regardless: a
  60k-row month is ~620 requests. `&excel=true` ignores paging and returns the whole month as one xlsx
  (4 MB, 15-30 s), so that is what sync uses; the JSON `meta.total` (one 1-row request) verifies the row count.
- The xlsx has no MF_ID, TER_Year or Month columns (the JSON has). `mf_id` is therefore only stored when rows
  come from JSON; TER_Year/Month are derivable from the date.
- History starts June 2018 (a few stray rows in Jan/Apr/May 2018; nothing earlier). Rows are daily (weekends
  included for most schemes; some AMCs publish business days only) and are not sorted.
- Two layouts. Until March 2026: Base TER, 52(6A)(b), 52(6A)(c), GST, Total TER. From April 2026 (SEBI MF
  Regulations 2026): BER, brokerage, transaction cost, statutory levies, Total TER. The old layout is mapped
  Base TER -> ber, GST -> statutory, Total -> ter, with brokerage/transaction left NULL; 52(6A)(b)/(c) are
  stored as r_6a_b/r_6a_c/d_6a_b/d_6a_c.
- NSDL scheme codes only exist from November 2024; earlier rows have none. They are resolved by exact scheme
  name from rows that do have one, else get a stable key `NA/<hash of the name>`.
- A TER_Date is a calendar date written as UTC midnight ("2026-09-01T00:00:00.000Z"); xlsx stores a serial.

Change points: for each scheme (NSDL code) a row is written only when a published value (name, category,
type or any TER figure) differs from that scheme's previous record. A month is stored by replacing that
calendar month's rows, comparing its first record with the value in effect before it, and dropping the next
stored change point if it has become redundant, so re-syncing is idempotent and months can arrive in any
order (`sync_ter` also refreshes the next stored month so a gap-fill or revision cannot leave a boundary stale).
Lossless for values: the TER on any date is the latest change point at or before it. Days on which a scheme
published nothing, and the end of a discontinued scheme, are not recorded.
"""

import hashlib
import io
import logging
import re
import time
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import httpx
from rapidfuzz import fuzz
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from app.data.bulk import upsert
from app.data.linking import SchemeNameIndex, norm_name
from app.http import http_client, with_retries
from app.models import Scheme, SchemeDetail, SchemeLink, SchemeTer

log = logging.getLogger(__name__)

API = "https://www.amfiindia.com/api"
MONTHS_URL = f"{API}/populate-ter-month"
DATA_URL = f"{API}/populate-te-rdata-revised"
LINK_SOURCE = "ter"

_PLAN_FIELDS = ("ber", "brokerage", "transaction", "statutory", "ter", "6a_b", "6a_c")
# record field -> API key suffixes (new layout first, then the layout used until March 2026)
_API_SUFFIX = {
    "ber": ("BER", "BaseTER"),
    "brokerage": ("BrokerageCost",),
    "transaction": ("TransactionCost",),
    "statutory": ("StatutoryLevies", "GST"),
    "ter": ("TER",),
    "6a_b": ("6A_B",),
    "6a_c": ("6A_C",),
}
_TABLE_COLS = {c.name for c in SchemeTer.__table__.columns}
# Columns that make up a published value (compared for change points). mf_id is metadata, not a value.
VALUE_COLS = tuple(
    c
    for c in ("scheme_name", "scheme_category", "scheme_type", *(f"{p}_{f}" for p in "rd" for f in _PLAN_FIELDS))
    if c in _TABLE_COLS
)

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_FIXED_HEADERS = {
    "nsdl scheme code": "NSDLSchemeCode",
    "scheme name": "Scheme_Name",
    "scheme type": "SchemeType_Desc",
    "scheme category": "SchemeCat_Desc",
    "ter date": "TER_Date",
}
# (substring of a "Regular/Direct Plan - ..." header, API key suffix); order matters
_PLAN_HEADERS = (
    ("total", "TER"),
    ("statutory", "StatutoryLevies"),
    ("base expense", "BER"),
    ("base ter", "BaseTER"),
    ("brokerage", "BrokerageCost"),
    ("transaction", "TransactionCost"),
    ("52(6a)(b)", "6A_B"),
    ("52(6a)(c)", "6A_C"),
    ("gst", "GST"),
)


# --------------------------------------------------------------------------- parsing (pure)


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def synthetic_code(name: str) -> str:
    """Stable stand-in for a missing NSDL code (fits String(40))."""
    return "NA/" + hashlib.sha1(name_key(name).encode()).hexdigest()[:16]


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace(",", "").strip()) if isinstance(v, str) else float(v)
    except ValueError:
        return None
    return x if x == x else None  # NaN -> None


def _ter_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):  # xlsx serial day
        return date(1899, 12, 30) + timedelta(days=int(v)) if v > 0 else None
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def build_code_map(rows: Iterable[dict]) -> dict[str, str]:
    """name -> NSDL code from API-shaped rows; names shared by several codes are left out."""
    seen: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        code, name = (r.get("NSDLSchemeCode") or "").strip(), (r.get("Scheme_Name") or "").strip()
        if code and name:
            seen[name_key(name)].add(code)
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def parse_ter_rows(rows: list[dict], code_by_name: dict[str, str] | None = None) -> list[dict]:
    """API rows (JSON, or xlsx rows from `read_xlsx_rows`) -> `scheme_ter`-shaped dicts, one per scheme-day.

    Handles both layouts; `code_by_name` (see `build_code_map`) fills a missing NSDL code. Rows without a
    scheme name or a readable date are dropped.
    """
    out = []
    for r in rows:
        name = (r.get("Scheme_Name") or "").strip()
        day = _ter_date(r.get("TER_Date"))
        if not name or day is None:
            continue
        mf_id = r.get("MF_ID")
        rec = {
            "nsdl_code": (r.get("NSDLSchemeCode") or "").strip() or (code_by_name or {}).get(name_key(name)) or synthetic_code(name),
            "mf_id": int(mf_id) if str(mf_id or "").isdigit() else None,
            "scheme_name": name[:255],
            "scheme_category": (r.get("SchemeCat_Desc") or "").strip()[:160] or None,
            "scheme_type": (r.get("SchemeType_Desc") or "").strip()[:40] or None,
            "effective_from": day,
        }
        for plan in "RD":
            for field, suffixes in _API_SUFFIX.items():
                vals = [_num(r.get(f"{plan}_{s}")) for s in suffixes]
                rec[f"{plan.lower()}_{field}"] = next((v for v in vals if v is not None), None)
        out.append(rec)
    return out


def _key(rec) -> tuple:
    return tuple(rec[c] if isinstance(rec, dict) else getattr(rec, c) for c in VALUE_COLS)


def change_points(records: list[dict], before: dict[str, tuple] | None = None) -> list[dict]:
    """Keep a record only if its published values differ from the same scheme's previous record.

    `before` maps nsdl_code -> value key in effect before the first record (the previous month's tail),
    so a month boundary is not mistaken for a change. Input order does not matter; the same scheme-day
    twice keeps the last one. Output is sorted by (code, date).
    """
    days: dict[str, dict[date, dict]] = defaultdict(dict)
    for r in records:
        days[r["nsdl_code"]][r["effective_from"]] = r
    out = []
    for code in sorted(days):
        prev = (before or {}).get(code)
        for d in sorted(days[code]):
            k = _key(days[code][d])
            if k != prev:
                out.append(days[code][d])
                prev = k
    return out


# --------------------------------------------------------------------------- xlsx (stdlib only)


def _header_key(header: str) -> str | None:
    s = header.strip().lower()
    if s in _FIXED_HEADERS:
        return _FIXED_HEADERS[s]
    prefix = "R_" if s.startswith("regular plan") else "D_" if s.startswith("direct plan") else None
    if prefix:
        for needle, suffix in _PLAN_HEADERS:
            if needle in s:
                return prefix + suffix
    return None


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def read_xlsx_rows(content: bytes) -> list[dict]:
    """The AMFI TER workbook -> API-shaped dicts (keys as in the JSON, dates as ISO strings).

    The sheet is 40+ MB of XML, so it is streamed. The 12 disclaimer rows under the data (merged cells,
    no date) are dropped.
    """
    rows: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.parse(z.open("xl/sharedStrings.xml")).getroot().iter(_XLSX_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))
        sheet = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))[0]
        keys: dict[int, str] = {}
        with z.open(sheet) as f:
            for _, el in ET.iterparse(f, events=("end",)):
                if el.tag != _XLSX_NS + "row":
                    continue
                cells: dict[int, object] = {}
                for c in el.findall(_XLSX_NS + "c"):
                    t, v = c.get("t"), c.find(_XLSX_NS + "v")
                    if t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(_XLSX_NS + "t"))
                    elif v is None or v.text is None:
                        continue
                    elif t == "s":
                        val = shared[int(v.text)]
                    elif t in ("str", "e"):
                        val = v.text
                    elif t == "b":
                        val = v.text == "1"
                    else:
                        val = float(v.text)
                    cells[_col(c.get("r"))] = val
                el.clear()
                if not keys:
                    keys = {i: k for i, h in cells.items() if isinstance(h, str) and (k := _header_key(h))}
                    if not {"Scheme_Name", "TER_Date"} <= set(keys.values()):
                        raise ValueError(f"Unrecognised TER workbook header: {list(cells.values())!r}")
                    continue
                row = {keys[i]: v for i, v in cells.items() if i in keys}
                day = _ter_date(row.get("TER_Date")) if "TER_Date" in row else None
                if row.get("Scheme_Name") and day:
                    row["TER_Date"] = day.isoformat()
                    rows.append(row)
    return rows


# --------------------------------------------------------------------------- fetching


def _retry(what: str, fn: Callable, attempts: int = 4):
    return with_retries(f"AMFI TER {what}", fn, attempts=attempts, retry_on=(httpx.HTTPError, zipfile.BadZipFile, ValueError, KeyError))


def month_sort_key(month: str) -> tuple[int, int]:
    mm, yy = month.split("-")
    return int(yy), int(mm)


def month_window(month: str) -> tuple[date, date]:
    y, m = month_sort_key(month)
    first = date(y, m, 1)
    return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def list_months(client: httpx.Client, months: int = 36, today: date | None = None) -> list[str]:
    """The latest `months` month codes ("MM-YYYY", newest first) that AMFI lists, unioned over financial years."""
    today = today or date.today()
    fy = today.year if today.month >= 4 else today.year - 1
    found: set[str] = set()
    for y in range(fy, fy - months // 12 - 2, -1):
        body = _retry(f"months {y}", lambda y=y: _json(client.get(MONTHS_URL, params={"year": f"{y}-{y + 1}"})))
        found |= {m["MonthNumber"] for m in body}
    current = (today.year, today.month)
    return sorted((m for m in found if month_sort_key(m) <= current), key=month_sort_key, reverse=True)[:months]


def _json(resp: httpx.Response):
    resp.raise_for_status()
    return resp.json()


def fetch_month_total(client: httpx.Client, month: str) -> int:
    """Rows AMFI says the month has (JSON meta.total of a 1-row page)."""
    params = {"MF_ID": "All", "Month": month, "strCat": -1, "strType": -1, "page": 1, "pageSize": 1}
    return int(_retry(f"total {month}", lambda: _json(client.get(DATA_URL, params=params))["meta"]["total"]))


def fetch_month_rows(client: httpx.Client, month: str) -> list[dict]:
    """One month as API-shaped rows, via the xlsx export; the row count must equal AMFI's own total."""
    params = {"MF_ID": "All", "Month": month, "strCat": -1, "strType": -1, "page": 1, "pageSize": 100, "excel": "true"}

    def once() -> list[dict]:
        resp = client.get(DATA_URL, params=params)
        resp.raise_for_status()
        rows = read_xlsx_rows(resp.content)
        expected = fetch_month_total(client, month)
        if len(rows) != expected:
            raise ValueError(f"{month}: workbook has {len(rows)} rows, AMFI reports {expected}")
        return rows

    return _retry(f"month {month}", once, attempts=3)


# --------------------------------------------------------------------------- storing


def _edge_rows(db: Session, cols: list, boundary: date, *, before: bool) -> dict:
    """Per code, the stored row closest to `boundary` on one side (latest before it / earliest after it)."""
    T = SchemeTer
    agg, cond = (func.max, T.effective_from < boundary) if before else (func.min, T.effective_from > boundary)
    sub = select(T.nsdl_code.label("c"), agg(T.effective_from).label("d")).where(cond).group_by(T.nsdl_code).subquery()
    q = select(*cols).join(sub, and_(T.nsdl_code == sub.c.c, T.effective_from == sub.c.d))
    return {r.nsdl_code: r for r in db.execute(q)}


def store_month(db: Session, month: str, records: list[dict]) -> dict:
    """Replace one calendar month of change points with the compression of `records` (parsed daily rows)."""
    T = SchemeTer
    start, end = month_window(month)
    inwin = [r for r in records if start <= r["effective_from"] <= end]
    cols = [T.id, T.nsdl_code, T.effective_from, T.mf_id, *(getattr(T, c) for c in VALUE_COLS)]

    pred = _edge_rows(db, cols, start, before=True)
    nxt = _edge_rows(db, cols, end, before=False)
    existing = {(r.nsdl_code, r.effective_from): r for r in db.execute(select(*cols).where(T.effective_from.between(start, end)))}

    target = {(r["nsdl_code"], r["effective_from"]): r for r in change_points(inwin, {c: _key(r) for c, r in pred.items()})}
    drop = [r.id for k, r in existing.items() if k not in target]

    write, unchanged = [], 0
    for k, r in target.items():
        old = existing.get(k)
        mf_id = r["mf_id"] if r["mf_id"] is not None else (old.mf_id if old else None)
        if old is not None and _key(old) == _key(r) and old.mf_id == mf_id:
            unchanged += 1
            continue
        write.append({"id": uuid.uuid4().hex, "nsdl_code": r["nsdl_code"], "effective_from": r["effective_from"], "mf_id": mf_id, **{c: r[c] for c in VALUE_COLS}})

    # The first stored change point after this month is redundant when it equals the month's last value.
    last: dict[str, dict] = {}
    for r in sorted(inwin, key=lambda r: r["effective_from"]):
        last[r["nsdl_code"]] = r
    drop += [row.id for code, row in nxt.items() if code in last and _key(row) == _key(last[code])]

    for i in range(0, len(drop), 500):
        db.execute(delete(T).where(T.id.in_(drop[i : i + 500])))
    upsert(db, T, write, ["nsdl_code", "effective_from"], ["mf_id", *VALUE_COLS])
    db.commit()
    updated = sum(1 for w in write if (w["nsdl_code"], w["effective_from"]) in existing)
    return {
        "month": month,
        "rows": len(inwin),
        "out_of_window": len(records) - len(inwin),
        "schemes": len(last),
        "change_points": len(target),
        "inserted": len(write) - updated,
        "updated": updated,
        "unchanged": unchanged,
        "deleted": len(drop),
    }


def _next_stored_month(db: Session, after: date) -> str | None:
    d = db.scalar(select(func.min(SchemeTer.effective_from)).where(SchemeTer.effective_from > after))
    return f"{d.month:02d}-{d.year}" if d else None


def _db_code_map(db: Session) -> dict[str, str]:
    seen: dict[str, set[str]] = defaultdict(set)
    for name, code in db.execute(select(SchemeTer.scheme_name, SchemeTer.nsdl_code).distinct().where(~SchemeTer.nsdl_code.like("NA/%"))):
        seen[name_key(name)].add(code)
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


# --------------------------------------------------------------------------- linking


_PAREN = re.compile(r"\([^)]*\)")
_FUZZY_MIN_RATIO = 90.0  # plain string similarity a fuzzy match must also reach (token_set_ratio scores any subset as 100)


class _NameMatcher:
    """TER scheme name -> AMFI codes: exact, then safe normalisations, then guarded fuzzy.

    Passes, in order (score): normalised name in the NAVAll index (100, exact); the same with spaces
    removed, which absorbs "NIFTY50" vs "Nifty 50" (99, fuzzy); the scheme master's own base name (100 / 99);
    all of these again after dropping parenthetical notes such as "(scheme has Two segregated portfolios)"
    (98); finally `SchemeNameIndex.match`, accepted only if the names are also >= 90% similar as plain strings,
    which rejects "Navi ELSS Tax Saver Nifty 50 Index Fund" -> "Navi ELSS Tax Saver Fund".
    """

    def __init__(self, db: Session, min_score: float):
        self.min_score = min_score
        self.idx = SchemeNameIndex(db)
        self.names = dict(db.execute(select(Scheme.amfi_code, Scheme.name)).all())
        base: dict[str, set[int]] = defaultdict(set)
        for code, name in db.execute(select(SchemeDetail.amfi_code, SchemeDetail.scheme_base_name)):
            if name and code in self.names:
                base[norm_name(name)].add(code)
        # (name -> codes, keys have spaces removed, method, score)
        self.tables = [(self.idx.by_name, False, "exact", 100.0), (self._compact(self.idx.by_name), True, "fuzzy", 99.0),
                       (base, False, "exact", 100.0), (self._compact(base), True, "fuzzy", 99.0)]

    @staticmethod
    def _compact(table: dict[str, set[int]]) -> dict[str, set[int]]:
        out: dict[str, set[int]] = defaultdict(set)
        for k, v in table.items():
            out[k.replace(" ", "")] |= v
        return out

    def match(self, name: str) -> tuple[set[int], str, float] | None:
        variants = [(name, 0.0)]
        stripped = _PAREN.sub(" ", name)
        if norm_name(stripped) != norm_name(name):
            variants.append((stripped, 2.0))  # a dropped note makes even an exact hit "fuzzy"
        for variant, penalty in variants:
            key = norm_name(variant)
            for table, compact, method, score in self.tables:
                hit = table.get(key.replace(" ", "") if compact else key)
                if hit:
                    return set(hit), "fuzzy" if penalty else method, score - penalty
        m = self.idx.match(name, self.min_score)
        if m and m.method == "fuzzy":
            key = norm_name(name)
            if max(fuzz.ratio(key, norm_name(self.names[c])) for c in m.codes) >= _FUZZY_MIN_RATIO:
                return m.codes, "fuzzy", m.score
        return None


def link_ter_codes(db: Session, min_score: float = 95.0) -> dict:
    """Map every stored TER scheme to AMFI codes by name and write `scheme_link` rows (source="ter").

    See `_NameMatcher` for the passes. An AMFI code claimed by several TER schemes goes to an exact match
    over a fuzzy one, then the higher score, then the most recently disclosed scheme.
    """
    latest = _latest(db)
    matcher = _NameMatcher(db, min_score)

    claims: dict[int, tuple] = {}  # amfi_code -> rank tuple, nsdl_code, method, score
    unmatched, methods, matched = [], defaultdict(int), {}
    for code, row in latest.items():
        found = matcher.match(row.scheme_name)
        if found is None:
            unmatched.append(row.scheme_name)
            continue
        codes, method, score = found
        methods[method] += 1
        matched[code] = row.scheme_name
        rank = (method == "exact", score, row.effective_from, code)
        for amfi in codes:
            if amfi not in claims or rank > claims[amfi][0]:
                claims[amfi] = (rank, code, method, score)

    rows = [{"amfi_code": a, "source": LINK_SOURCE, "external_key": c[1], "method": c[2], "score": c[3]} for a, c in claims.items()]
    stale = sorted(set(db.scalars(select(SchemeLink.amfi_code).where(SchemeLink.source == LINK_SOURCE))) - claims.keys())
    for i in range(0, len(stale), 500):
        db.execute(delete(SchemeLink).where(SchemeLink.source == LINK_SOURCE, SchemeLink.amfi_code.in_(stale[i : i + 500])))
    upsert(db, SchemeLink, rows, ["amfi_code", "source"], ["external_key", "method", "score"])
    db.commit()
    winners = {c[1] for c in claims.values()}
    shadowed = sorted(name for code, name in matched.items() if code not in winners)  # name matched, every code went to a sibling
    return {
        "ter_schemes": len(latest),
        "matched": len(matched),
        "exact": methods["exact"],
        "fuzzy": methods["fuzzy"],
        "unmatched": len(unmatched),
        "unmatched_examples": sorted(unmatched)[:15],
        "shadowed": len(shadowed),
        "shadowed_examples": shadowed[:15],
        "amfi_codes_linked": len(claims),
        "stale_links_removed": len(stale),
    }


# --------------------------------------------------------------------------- sync


def sync_ter(db: Session, months: int = 36, progress: Callable | None = None) -> dict:
    """Download the latest `months` months of TER, store change points, relink.

    Months are stored oldest to newest (the newest is fetched first, once, because only recent months carry
    NSDL codes). A month that fails to download is reported in `failed_months`, and one that comes back with
    no rows in `empty_months`; both are skipped (stored data is kept) and a later run fills them.
    `progress(done, total, month, rows, change_points)` is called after each stored month.
    """
    t0 = time.time()
    results, failed, empty = [], {}, []
    with http_client() as client:
        wanted = list_months(client, months)
        if not wanted:
            return {"months": [], "error": "AMFI lists no TER months"}
        code_map = _db_code_map(db)
        cache: dict[str, list[dict]] = {}

        def load(month: str) -> list[dict] | None:
            if month in failed:
                return None
            try:
                rows = cache.pop(month) if month in cache else fetch_month_rows(client, month)
            except RuntimeError as exc:
                failed[month] = str(exc)
                return None
            if not rows:  # storing replaces the month, so an empty answer must never reach the database
                empty.append(month)
                return None
            return rows

        newest = load(wanted[0])
        if newest is not None:
            cache[wanted[0]] = newest
            code_map.update(build_code_map(newest))

        def run(month: str) -> dict | None:
            rows = load(month)
            if rows is None:
                return None
            res = store_month(db, month, parse_ter_rows(rows, code_map))
            results.append(res)
            return res

        order = sorted(wanted, key=month_sort_key)
        for done, month in enumerate(order, 1):
            res = run(month)
            if progress and res:
                progress(done, len(order), month, res["rows"], res["change_points"])

        # A stored month beyond a (re)synced one has a boundary that depended on the old data: refresh it.
        refreshed: set[str] = set()
        for res in list(results):
            nxt = _next_stored_month(db, month_window(res["month"])[1])
            if nxt and nxt not in wanted and nxt not in refreshed:
                refreshed.add(nxt)
                run(nxt)

    links = link_ter_codes(db)
    return {
        "months": sorted((r["month"] for r in results), key=month_sort_key),
        "failed_months": failed,
        "empty_months": sorted(set(empty), key=month_sort_key),
        "boundary_months_refreshed": sorted(refreshed, key=month_sort_key),
        "daily_rows_read": sum(r["rows"] for r in results),
        "change_points_in_months": sum(r["change_points"] for r in results),
        "inserted": sum(r["inserted"] for r in results),
        "updated": sum(r["updated"] for r in results),
        "deleted": sum(r["deleted"] for r in results),
        "stored_rows": db.scalar(select(func.count()).select_from(SchemeTer)),
        "stored_schemes": db.scalar(select(func.count(func.distinct(SchemeTer.nsdl_code)))),
        "links": links,
        "elapsed_s": round(time.time() - t0, 1),
    }


# --------------------------------------------------------------------------- reading


def _latest(db: Session, nsdl_codes: Iterable[str] | None = None) -> dict:
    """Per NSDL code, the change point currently in force (the latest one)."""
    T = SchemeTer
    out: dict = {}
    keys = None if nsdl_codes is None else sorted(set(nsdl_codes))
    for chunk in [None] if keys is None else [keys[i : i + 900] for i in range(0, len(keys), 900)]:
        sub = select(T.nsdl_code.label("c"), func.max(T.effective_from).label("d")).group_by(T.nsdl_code)
        if chunk is not None:
            sub = sub.where(T.nsdl_code.in_(chunk))
        sub = sub.subquery()
        q = select(T).join(sub, and_(T.nsdl_code == sub.c.c, T.effective_from == sub.c.d))
        out.update({r.nsdl_code: r for r in db.scalars(q)})
    return out


def _plan_values(row, plan: str) -> dict | None:
    p = plan[0]
    ter = getattr(row, f"{p}_ter")
    if ter is None or ter <= 0:  # 0 means "no such plan" in AMFI's sheet, not a free fund
        return None
    return {
        "ter": ter,
        "ber": getattr(row, f"{p}_ber"),
        "brokerage": getattr(row, f"{p}_brokerage"),
        "transaction": getattr(row, f"{p}_transaction"),
        "statutory": getattr(row, f"{p}_statutory"),
    }


def current_ter_rows(db: Session, amfi_codes: Iterable[int]) -> dict[int, SchemeTer]:
    """The change point in force for each AMFI code's scheme, via its `ter` link. One row serves all of a
    scheme's codes (it carries both the Regular and the Direct figures); codes without a link are absent."""
    codes = sorted(set(amfi_codes))
    links: dict[int, str] = {}
    for i in range(0, len(codes), 900):
        chunk = codes[i : i + 900]
        links.update(db.execute(select(SchemeLink.amfi_code, SchemeLink.external_key).where(SchemeLink.source == LINK_SOURCE, SchemeLink.amfi_code.in_(chunk))).all())
    latest = _latest(db, links.values())
    return {code: latest[key] for code, key in links.items() if key in latest}


def latest_ter(db: Session, amfi_codes: Iterable[int], plan: str | None = None) -> dict[int, dict]:
    """Current TER per AMFI code: the `plan` ("regular" / "direct") figures for every code or, when `plan`
    is None, those of each code's own `Scheme.plan_type`. The ranking asks for plan="regular" so a Direct
    plan is costed like for like with its Regular-plan peers.

    Codes with no TER link, or whose plan shows no TER (AMFI writes 0 for "no such plan"), are absent.
    `effective_from` is when these figures started, not when they were last confirmed. brokerage/transaction
    are None for figures disclosed in the layout used until March 2026.
    """
    rows = current_ter_rows(db, amfi_codes)
    plans: dict[int, str] = {}
    if plan is None:
        codes = sorted(rows)
        for i in range(0, len(codes), 900):
            plans.update(db.execute(select(Scheme.amfi_code, Scheme.plan_type).where(Scheme.amfi_code.in_(codes[i : i + 900]))).all())
    out = {}
    for code, row in rows.items():
        p = plan or plans.get(code)
        vals = _plan_values(row, p) if p in ("regular", "direct") else None
        if vals:
            out[code] = {**vals, "plan": p, "effective_from": row.effective_from, "nsdl_code": row.nsdl_code}
    return out

