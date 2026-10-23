"""AMC monthly portfolio disclosures (.xlsx / .xls workbooks, or a .zip of them) -> scheme_portfolio.

Layouts differ by AMC, so the parser looks for structure instead of fixed cells:
- A table starts at a header row holding an instrument-name, an ISIN and a % to net assets / NAV column
  (quantity, market value and industry / rating columns are picked up when present). A sheet may hold one
  scheme (often next to an index sheet, which has no such header and is skipped) or several stacked ones.
- The scheme's name is the closest fund-like text above its header; the disclosure date is the first date
  there, else at the top of the sheet, else in the file name.
- Heading rows ("Equity & Equity related", "Money Market Instruments", ...) set the asset class of the rows
  under them; TREPS / repo / net current assets / cash margin rows are cash whatever their heading, rows
  whose industry says futures / options are derivatives, and physical gold under a generic heading is gold.
- Sub-totals are skipped and the table ends at the grand total, so tables printed after it (derivative
  detail, NAV notes) are not read.
- Weights given as fractions (summing to about 1) become %. Repeated ISINs are aggregated.

Nothing is repaired silently: a malformed ISIN is dropped from its row and reported, and a scheme whose
weights (every row read, derivatives included, as in the disclosed grand total) do not sum to 100 +/- 3 is
quarantined with the reason. Linking stores a portfolio under the scheme's Regular-Growth AMFI code, matched by name
with the TER matcher; names that do not match are reported.
"""

import calendar
import io
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import openpyxl
import xlrd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data.ter import _NameMatcher
from app.models import Scheme, SchemeHolding

WEIGHT_TOLERANCE = 3.0  # a scheme's weights must sum to 100 +/- this many percentage points

# --------------------------------------------------------------------------- reference data

# AMFI industry -> sector roll-up. Keys are the canonical industry names stored in `industry`.
SECTOR_BY_INDUSTRY = {
    "Banks": "Financial Services",
    "Finance": "Financial Services",
    "Capital Markets": "Financial Services",
    "Insurance": "Financial Services",
    "Financial Technology (Fintech)": "Financial Services",
    "IT - Software": "Information Technology",
    "IT - Services": "Information Technology",
    "IT - Hardware": "Information Technology",
    "Pharmaceuticals & Biotechnology": "Healthcare",
    "Healthcare Services": "Healthcare",
    "Healthcare Equipment & Supplies": "Healthcare",
    "Automobiles": "Automobile and Auto Components",
    "Auto Components": "Automobile and Auto Components",
    "Aerospace & Defense": "Capital Goods",
    "Agricultural, Commercial & Construction Vehicles": "Capital Goods",
    "Electrical Equipment": "Capital Goods",
    "Industrial Manufacturing": "Capital Goods",
    "Industrial Products": "Capital Goods",
    "Petroleum Products": "Oil, Gas & Consumable Fuels",
    "Oil": "Oil, Gas & Consumable Fuels",
    "Gas": "Oil, Gas & Consumable Fuels",
    "Consumable Fuels": "Oil, Gas & Consumable Fuels",
    "Diversified FMCG": "Fast Moving Consumer Goods",
    "Food Products": "Fast Moving Consumer Goods",
    "Beverages": "Fast Moving Consumer Goods",
    "Personal Products": "Fast Moving Consumer Goods",
    "Household Products": "Fast Moving Consumer Goods",
    "Agricultural Food & other Products": "Fast Moving Consumer Goods",
    "Cigarettes & Tobacco Products": "Fast Moving Consumer Goods",
    "Retailing": "Consumer Services",
    "Leisure Services": "Consumer Services",
    "Other Consumer Services": "Consumer Services",
    "Consumer Durables": "Consumer Durables",
    "Ferrous Metals": "Metals & Mining",
    "Non - Ferrous Metals": "Metals & Mining",
    "Diversified Metals": "Metals & Mining",
    "Minerals & Mining": "Metals & Mining",
    "Metals & Minerals Trading": "Metals & Mining",
    "Power": "Power",
    "Other Utilities": "Utilities",
    "Cement & Cement Products": "Construction Materials",
    "Other Construction Materials": "Construction Materials",
    "Chemicals & Petrochemicals": "Chemicals",
    "Fertilizers & Agrochemicals": "Chemicals",
    "Telecom - Services": "Telecommunication",
    "Telecom - Equipment & Accessories": "Telecommunication",
    "Construction": "Construction",
    "Realty": "Realty",
    "Transport Services": "Services",
    "Transport Infrastructure": "Services",
    "Commercial Services & Supplies": "Services",
    "Textiles & Apparels": "Textiles",
    "Entertainment": "Media, Entertainment & Publication",
    "Media": "Media, Entertainment & Publication",
    "Printing & Publication": "Media, Entertainment & Publication",
    "Paper, Forest & Jute Products": "Forest Materials",
    "Diversified": "Diversified",
}
# Spellings seen in AMC files that differ from the canonical name by more than case and punctuation.
_INDUSTRY_ALIASES = {
    "software": "IT - Software",
    "pharmaceuticals": "Pharmaceuticals & Biotechnology",
    "aerospace and defence": "Aerospace & Defense",
    "telecom services": "Telecom - Services",
    "fintech": "Financial Technology (Fintech)",
}

# Heading text -> asset class of the rows under it; first match wins.
_SECTIONS = (
    (re.compile(r"deriv|future|option|swap"), "derivative"),
    (re.compile(r"reit|invit|real estate investment|infrastructure investment"), "reit_invit"),
    (re.compile(r"alternative investment"), "other"),
    (re.compile(r"mutual fund|fund units|units of|exchange traded fund|\betfs?\b|investment funds?\b"), "mf_units"),
    (re.compile(r"foreign|overseas|international|\badrs?\b|\bgdrs?\b"), "foreign_equity"),
    (re.compile(r"\bgold\b"), "gold"),
    (re.compile(r"treps|tri-?party|repo\b|cblo"), "cash"),
    (re.compile(r"money market|certificate of deposit|commercial paper|treasury bill|t-bill|tbill"), "money_market"),
    (re.compile(r"debt|bond|debenture|\bncds?\b|government securit|g-sec|gilt|state development|\bsdls?\b|securiti[sz]ed|pass through"), "debt"),
    (re.compile(r"equity|shares|arbitrage"), "equity"),
    (re.compile(r"deposit|cash|net current|receivable"), "cash"),
)
_CASH_ROW = re.compile(r"treps|tri-?party|repo\b|cblo|net current asset|net receivable|net payable|cash margin|margin money|"
                       r"cash and cash equivalent|cash & cash equivalent|^cash\b")
_GRAND_TOTAL = re.compile(r"^(grand total|total net assets|net assets)\b")
_SUBTOTAL = re.compile(r"^(sub[\s-]*total|total)\b")
_DERIVATIVE_ROW = re.compile(r"futures?|options?|swaps?", re.I)
_MAX_NAME = 100  # longer fund-like text above a table is a note, not a scheme name
_FUNDLIKE = re.compile(r"\b(fund|fof|etf|bees|plan|series|portfolio)\b", re.I)
_NOT_NAME = re.compile(r"portfolio statement|monthly portfolio|portfolio as on|statement of portfolio|\bas on\b|^\(?\d+\)|as follows", re.I)
_DESCRIPTION = re.compile(r"\(\s*an?\s", re.I)  # "(An open ended equity scheme ...)" after the name
_RATING = re.compile(r"\b(crisil|icra|care|ind|fitch|brickwork|acuite|infomerics|sovereign|sov|unrated)\b|^[a-d]{1,3}[+-]?$|^a[1-4]\+?$", re.I)
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_DATE_PATTERNS = (
    re.compile(r"(?P<d>\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*(?P<m>[a-z]{3})[a-z]*\.?[\s\-/.,]*(?P<y>\d{4}|\d{2}\b)", re.I),
    re.compile(r"(?P<m>[a-z]{3})[a-z]*\.?\s*(?P<d>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(?P<y>\d{4})", re.I),
    re.compile(r"(?P<d>\d{1,2})[./-](?P<mm>\d{1,2})[./-](?P<y>\d{4})"),
)


# --------------------------------------------------------------------------- parsed records


@dataclass
class Holding:
    name: str
    isin: str | None
    asset_class: str
    weight_pct: float
    industry: str | None = None
    sector: str | None = None
    rating: str | None = None
    market_value_inr: float | None = None
    quantity: float | None = None


@dataclass
class ParsedScheme:
    name: str
    sheet: str
    month: date | None
    holdings: list[Holding]
    weight_sum: float = 0.0
    issues: list[str] = field(default_factory=list)  # reported, not fatal
    quarantine: str | None = None  # why the scheme is not loaded


# --------------------------------------------------------------------------- small rules


def isin_ok(isin: str) -> bool:
    """ISO 6166: 2 letters, 9 alphanumerics, 1 check digit (Luhn over the letters-as-numbers expansion)."""
    if not _ISIN.fullmatch(isin):
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 == 0 else 1)
        total += n - 9 if n > 9 else n
    return (10 - total % 10) % 10 == int(isin[-1])


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("&", " and "))).strip()


_INDUSTRY_BY_KEY = {_norm(k): k for k in SECTOR_BY_INDUSTRY} | {_norm(k): v for k, v in _INDUSTRY_ALIASES.items()}


def normalise_industry(raw: str | None) -> tuple[str | None, str | None]:
    """Disclosed industry -> (canonical industry, sector). Unknown industries keep their spelling and get no sector."""
    if not raw or not str(raw).strip():
        return None, None
    text = re.sub(r"\s+", " ", str(raw)).strip(" *#")
    canonical = _INDUSTRY_BY_KEY.get(_norm(text))
    return (canonical, SECTOR_BY_INDUSTRY[canonical]) if canonical else (text, None)


def section_class(heading: str) -> str | None:
    h = heading.lower()
    return next((cls for rx, cls in _SECTIONS if rx.search(h)), None)


def _number(v) -> float | None:
    """A numeric cell, or a number written as text ("1,234.5", "$0.00%"); None for "NIL", "*", blanks."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*[$*#^~]*\s*(\(?-?[\d,]*\.?\d+\)?)\s*%?\s*[$*#^~]*\s*", str(v))
    if not m:
        return None
    s = m.group(1).replace(",", "")
    return -float(s.strip("()")) if s.startswith("(") else float(s)


def _text(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def find_date(texts) -> date | None:
    """The month-end of the first date found in `texts` (datetime cells or text such as 'September 30, 2026')."""
    for t in texts:
        if isinstance(t, (datetime, date)):
            return _month_end(t.year, t.month)
        if not isinstance(t, str):
            continue
        for rx in _DATE_PATTERNS:
            for m in rx.finditer(t):
                g = m.groupdict()
                month = int(g["mm"]) if g.get("mm") else _MONTHS.get(g["m"][:3].lower())
                year = int(g["y"]) + (2000 if len(g["y"]) == 2 else 0)
                if month and 1 <= month <= 12 and 1 <= int(g["d"]) <= 31 and 2000 <= year <= 2100:
                    return _month_end(year, month)
    return None


def scheme_name(text: str) -> str:
    """'Nippon India Large Cap Fund (An open ended ...)\\nSegregated ...' -> 'Nippon India Large Cap Fund'."""
    text = _DESCRIPTION.split(text, 1)[0]
    text = re.sub(r"^\s*(scheme name|name of the scheme)\s*[:\-]\s*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -:")


# --------------------------------------------------------------------------- workbook reading


def read_sheets(content: bytes) -> list[tuple[str, list[list]]]:
    """Every sheet as (name, rows of cell values). The format is sniffed from the bytes: AMCs often
    serve .xlsx files named .xls."""
    if content[:2] == b"PK":
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
        finally:
            wb.close()
    book = xlrd.open_workbook(file_contents=content)
    out = []
    for sh in book.sheets():
        rows = []
        for i in range(sh.nrows):
            row = []
            for c in sh.row(i):
                if c.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(c.value, book.datemode))
                elif c.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(c.value)
            rows.append(row)
        out.append((sh.name, rows))
    return out


def workbooks(content: bytes, source: str) -> list[tuple[str, bytes]]:
    """(name, bytes) of each workbook in a file: the file itself, or every .xls/.xlsx inside a .zip."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = z.namelist()
            if "[Content_Types].xml" not in names:  # a plain zip, not an .xlsx package
                return [(n, z.read(n)) for n in sorted(names) if n.lower().endswith((".xls", ".xlsx")) and not n.startswith("__MACOSX")]
    return [(source, content)]


# --------------------------------------------------------------------------- parsing


def _header_columns(row: list) -> dict[str, int] | None:
    cols: dict[str, int] = {}
    for i, v in enumerate(row):
        h = _text(v).lower()
        if not h:
            continue
        if "%" in h and ("nav" in h or "net asset" in h or "aum" in h):
            cols.setdefault("weight", i)
        elif h.startswith("isin"):
            cols.setdefault("isin", i)
        elif "market" in h or "fair value" in h:
            cols.setdefault("value", i)
            cols.setdefault("value_unit", 10**7 if re.search(r"crore|\bcr\b", h) else 10**5 if re.search(r"lakh|lac", h)
                            else 10**3 if "thousand" in h else 1 if re.search(r"\brs\b|inr|rupee|₹", h) else 0)
        elif "quantity" in h or h in ("qty", "units", "no. of shares"):
            cols.setdefault("quantity", i)
        elif "industry" in h or "sector" in h:
            cols.setdefault("industry", i)
            if "rating" in h:
                cols.setdefault("rating", i)
        elif "rating" in h:
            cols.setdefault("rating", i)
        elif "name" in h or "instrument" in h or "issuer" in h or "security" in h:
            cols.setdefault("name", i)
    return cols if {"name", "isin", "weight"} <= cols.keys() else None


def _find_name(rows: list[list]) -> str | None:
    for row in reversed(rows):  # the closest to the table: notes of the previous scheme come first
        for v in row:
            t = str(v).strip() if isinstance(v, str) else ""
            if t and _FUNDLIKE.search(t) and not _NOT_NAME.search(t) and 2 <= len((name := scheme_name(t)).split()) and len(name) <= _MAX_NAME:
                return name
    return None


def _is_group_total(below: list[list], weight_col: int, weight: float, name_col: int, isin_col: int | None) -> bool:
    """True when the unbroken run of rows under a row has weights that add up to the row's own weight.

    Tried twice: counting every row, then ignoring nested non-cash headings ("Money Market" > "Treasury Bills" > bills)."""
    for skip_nested in (False, True):
        run = 0.0
        for r in below:
            if not any(v not in (None, "") for v in r):
                continue  # blank spacer row
            if any(isinstance(v, str) and (_SUBTOTAL.match(v.strip().lower()) or _GRAND_TOTAL.match(v.strip().lower())) for v in r):
                break  # a "Total" row under a lone holding repeats its weight; that is not a group heading
            w = _number(r[weight_col]) if weight_col < len(r) else None
            if w is None:
                if weight_col < len(r) and _text(r[weight_col]).lower() in ("^", "nil"):  # "^" = under 0.005%, "Nil" = none; both count as zero
                    continue
                break
            label = _text(r[name_col]).lower() if name_col < len(r) else ""
            has_isin = isin_col is not None and isin_col < len(r) and _text(r[isin_col]) != ""
            if skip_nested and not has_isin and not _CASH_ROW.search(label):
                continue
            run += w
            if abs(run - weight) <= 2e-3 * abs(weight) + 1e-9:
                return True
    return False


def _parse_table(rows: list[list], cols: dict[str, int], scheme: ParsedScheme) -> int:
    """Read holdings from `rows` (just below the header) into `scheme`; returns the number of rows consumed."""
    def cell(row, key):
        i = cols.get(key)
        return row[i] if i is not None and i < len(row) else None

    unit = cols.get("value_unit", 0)
    if "value" in cols and not unit:
        scheme.issues.append("market value unit not stated; values not stored")
    current = "other"
    for n, row in enumerate(rows):
        label = _text(cell(row, "name")) or next((_text(v) for v in row if isinstance(v, str) and v.strip()), "")
        low = label.lower()
        if _GRAND_TOTAL.match(low):
            return n + 1
        if _header_columns(row):  # the next stacked scheme started without a grand total
            return n
        if not label or _SUBTOTAL.match(low):
            continue
        weight = _number(cell(row, "weight"))
        raw_isin = _text(cell(row, "isin")).upper()
        # a group heading that carries its own total (ICICI: "Equity & Equity Related ... 97.9%") is a heading, not a holding
        group_total = weight is not None and not raw_isin and _number(cell(row, "quantity")) is None and (
            _is_group_total(rows[n + 1 :], cols["weight"], weight, cols.get("name", 0), cols.get("isin")))
        if weight is None or group_total:
            if raw_isin:
                scheme.issues.append(f"no numeric weight for {label} ({raw_isin}): {cell(row, 'weight')!r}")
            elif (cls := section_class(label)) is not None:
                current = cls
            continue
        isin = raw_isin or None
        if isin and not isin_ok(isin):
            scheme.issues.append(f"invalid ISIN {isin} for {label}")
            isin = None
        industry_raw = cell(row, "industry")
        rating = cell(row, "rating")
        if cols.get("industry") is not None and cols.get("industry") == cols.get("rating"):
            industry_raw, rating = (None, industry_raw) if _RATING.search(_text(industry_raw)) else (industry_raw, None)
        asset_class = "cash" if _CASH_ROW.search(low) else "gold" if current == "other" and re.match(r"gold\b", low) else current
        if _DERIVATIVE_ROW.search(_text(industry_raw)):
            asset_class, industry_raw = "derivative", None
        industry, sector = normalise_industry(industry_raw)
        value = _number(cell(row, "value"))
        scheme.holdings.append(Holding(
            name=label, isin=isin, asset_class=asset_class, weight_pct=weight,
            industry=industry, sector=sector, rating=_text(rating) or None,
            market_value_inr=value * unit if value is not None and unit else None, quantity=_number(cell(row, "quantity")),
        ))
    return len(rows)


def _finish(scheme: ParsedScheme) -> None:
    """Fractions -> %, aggregate repeated ISINs, check the weight sum."""
    if not scheme.holdings:
        scheme.quarantine = "no holdings found"
        return
    if scheme.month is None:
        scheme.quarantine = "no disclosure date found"
    # AMCs disclose derivatives either inside the net-asset total or beside it (the short leg of an arbitrage):
    # a portfolio is complete if either sum is 100.
    total_all = sum(h.weight_pct for h in scheme.holdings)
    total_ex = sum(h.weight_pct for h in scheme.holdings if h.asset_class != "derivative")
    total = min((total_all, total_ex), key=lambda t: min(abs(t - 1), abs(t - 100)))
    if abs(total - 1) <= WEIGHT_TOLERANCE / 100:
        for h in scheme.holdings:
            h.weight_pct *= 100
        total *= 100
    merged: dict[tuple, Holding] = {}
    out = []
    for h in scheme.holdings:
        key = (h.isin, h.asset_class)
        if h.isin and key in merged:
            m = merged[key]
            m.weight_pct += h.weight_pct
            for f in ("market_value_inr", "quantity"):
                a, b = getattr(m, f), getattr(h, f)
                setattr(m, f, None if a is None or b is None else a + b)
            continue
        if h.isin:
            merged[key] = h
        out.append(h)
    scheme.holdings = out
    scheme.weight_sum = round(total, 4)
    if scheme.quarantine is None and abs(total - 100) > WEIGHT_TOLERANCE:
        scheme.quarantine = f"weights sum to {total:.2f}%, not 100 +/- {WEIGHT_TOLERANCE:g}"


def parse_workbook(content: bytes, source: str = "") -> list[ParsedScheme]:
    """Every scheme portfolio in one workbook."""
    schemes: list[ParsedScheme] = []
    for sheet, rows in read_sheets(content):
        top_date = None
        start = 0  # first row not yet claimed by a table
        i = 0
        while i < len(rows):
            cols = _header_columns(rows[i])
            if not cols:
                i += 1
                continue
            above = rows[start:i]
            if top_date is None:
                top_date = find_date(v for r in rows[:i] for v in r)
            name = _find_name(above)
            month = find_date(v for r in above for v in r) or top_date or find_date([source])
            scheme = ParsedScheme(name=name or "", sheet=sheet, month=month, holdings=[])
            used = _parse_table(rows[i + 1 :], cols, scheme)
            i = start = i + 1 + used
            if not name:
                if scheme.holdings and not any(s.sheet == sheet for s in schemes):  # else: a note table after a scheme
                    scheme.quarantine = "no scheme name found above the table"
                    schemes.append(scheme)
                continue
            _finish(scheme)
            schemes.append(scheme)
    return schemes


# --------------------------------------------------------------------------- linking and loading


def regular_growth_codes(db: Session, matcher: _NameMatcher, name: str) -> tuple[int | None, str]:
    """(Regular-Growth AMFI code, how) for a disclosed scheme name; code is None when unmatched."""
    found = matcher.match(name)
    if not found:
        return None, "no AMFI scheme matches the name"
    codes = found[0]
    rows = db.execute(select(Scheme.amfi_code, Scheme.in_universe).where(
        Scheme.amfi_code.in_(codes), Scheme.plan_type == "regular", Scheme.option_type == "growth")).all()
    if not rows:
        return None, "matched, but the scheme has no Regular-Growth plan"
    return min(rows, key=lambda r: (not r.in_universe, r.amfi_code)).amfi_code, found[1]


def load_portfolios(db: Session, content: bytes, source: str, matcher: _NameMatcher | None = None) -> dict:
    """Parse one file (workbook or zip), link each scheme and store it, replacing each (amfi_code, month) portfolio."""
    matcher = matcher or _NameMatcher(db, 95.0)
    parsed: list[ParsedScheme] = []
    for name, data in workbooks(content, source):
        parsed += parse_workbook(data, name)
    loaded, unlinked, quarantined, issues, unmapped = [], [], [], {}, defaultdict(int)
    claimed: dict[tuple[int, date], str] = {}
    rows = 0
    for s in parsed:
        if s.issues:
            issues[s.name or s.sheet] = s.issues[:5] + ([f"... {len(s.issues) - 5} more"] if len(s.issues) > 5 else [])
        if s.quarantine:
            quarantined.append({"scheme": s.name, "sheet": s.sheet, "reason": s.quarantine})
            continue
        code, how = regular_growth_codes(db, matcher, s.name)
        if code is None:
            unlinked.append({"scheme": s.name, "reason": how})
            continue
        if (code, s.month) in claimed:
            unlinked.append({"scheme": s.name, "reason": f"AMFI code {code} already taken by {claimed[(code, s.month)]} in this file"})
            continue
        claimed[(code, s.month)] = s.name
        for h in s.holdings:
            if h.industry and not h.sector and h.asset_class == "equity":
                unmapped[h.industry] += 1
        db.execute(delete(SchemeHolding).where(SchemeHolding.amfi_code == code, SchemeHolding.disclosure_month == s.month))
        db.add_all(SchemeHolding(
            amfi_code=code, disclosure_month=s.month, security_isin=h.isin, security_name=h.name[:160], industry=(h.industry or "")[:120] or None,
            sector=h.sector, asset_class=h.asset_class, weight_pct=round(h.weight_pct, 6), market_value_inr=h.market_value_inr,
            quantity=h.quantity, rating=(h.rating or "")[:40] or None, source_url=source) for h in s.holdings)
        rows += len(s.holdings)
        loaded.append({"scheme": s.name, "amfi_code": code, "match": how, "month": s.month.isoformat(), "rows": len(s.holdings)})
    db.commit()
    return {
        "file": source,
        "months": sorted({x["month"] for x in loaded}),
        "schemes_parsed": len(parsed),
        "linked": len(loaded),
        "quarantined": len(quarantined),
        "unlinked": len(unlinked),
        "rows": rows,
        "quarantined_schemes": quarantined,
        "unlinked_schemes": unlinked,
        "issues": issues,
        "unmapped_equity_industries": dict(sorted(unmapped.items(), key=lambda kv: -kv[1])),
    }
