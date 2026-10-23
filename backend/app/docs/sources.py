"""Discovery and download of the Phase-1 public documents.

  a) regulatory texts for distributors (AMFI Code of Ethics, AMFI Master Circular for MFDs,
     AMFI FAQs on the role of MFDs / advertising, SEBI FAQs on the Investment Adviser regulations)
  b) AMFI monthly industry reports, discovered from https://www.amfiindia.com/research-information/amfi-monthly

Downloads are sequential, with a pause between requests and retries with backoff for transient
failures. If a site answers 401/403 the document is skipped and reported (`blocked`): there is no
retry with other headers and no workaround.
"""

import calendar
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.docs.store import PIPELINE_VERSION, ingest
from app.http import http_client
from app.models import Document

log = logging.getLogger(__name__)

MONTHLY_INDEX_URL = "https://www.amfiindia.com/research-information/amfi-monthly"
PAUSE_S = 1.0  # between consecutive downloads
_sleep = time.sleep  # indirection so tests don't wait


@dataclass(frozen=True)
class SourceDoc:
    url: str
    source: str  # amfi | sebi
    doc_type: str
    title: str
    published_on: date | None = None
    meta: dict = field(default_factory=dict)


REGULATORY_DOCS: tuple[SourceDoc, ...] = (
    SourceDoc(
        url="https://www.amfiindia.com/uploads/AMFI_Codeof_Ethics_2026_c9e1d12ba1.pdf",
        source="amfi", doc_type="regulation", title="AMFI Code of Ethics 2026",
        meta={"edition": "2026"},
    ),
    SourceDoc(
        url="https://www.amfiindia.com/uploads/AMFI_Master_Cicular_for_MF_Ds_3c7f5ee44f.pdf",
        source="amfi", doc_type="circular", title="AMFI Master Circular for Mutual Fund Distributors",
        published_on=date(2026, 1, 14),  # circular date on page 1
        meta={"circular_no": "AMFI/MFD-CIR/32/2025-26"},
    ),
    SourceDoc(
        url="https://www.amfiindia.com/Themes/Theme1/downloads/FAQsonRoleofMFDsAdvts.pdf",
        source="amfi", doc_type="faq", title="AMFI FAQs on Do's and Don'ts for MFDs (role, advertisements, nomenclature)",
    ),
    SourceDoc(
        url="https://www.sebi.gov.in/sebi_data/attachdocs/1424862077270.pdf",
        source="sebi", doc_type="faq", title="SEBI FAQs on SEBI (Investment Advisers) Regulations, 2013",
        published_on=date(2015, 2, 25),  # SEBI's attachment id is a Unix-ms timestamp; the text still counts down to April 2015
        meta={"edition_note": "2015 edition; the Investment Advisers regulations have been amended since"},
    ),
)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_NAMES = [calendar.month_name[i] for i in range(1, 13)]
# amsep2026repo.pdf; the odd one out is amjan2022reporevised.pdf
_MONTHLY_FILE = re.compile(r"/am(?P<mon>[a-z]{3})(?P<year>\d{4})repo(?P<suffix>[a-z]*)\.pdf$", re.I)


class _Hrefs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def monthly_doc(url: str, year: int, month: int) -> SourceDoc:
    return SourceDoc(
        url=url, source="amfi", doc_type="monthly_report",
        title=f"AMFI Monthly Report, {_MONTH_NAMES[month - 1]} {year}",
        published_on=date(year, month, calendar.monthrange(year, month)[1]),  # end of the month reported on
        meta={"period": f"{year}-{month:02d}"},
    )


def parse_monthly_index(html: str, base_url: str = MONTHLY_INDEX_URL) -> list[SourceDoc]:
    """Monthly report PDFs linked from the AMFI page, newest first, one per month (a file marked
    'revised' replaces the original). Month and year come from the file name."""
    parser = _Hrefs()
    parser.feed(html)
    by_month: dict[tuple[int, int], tuple[bool, str]] = {}
    for href in parser.hrefs:
        url = urljoin(base_url, href)
        m = _MONTHLY_FILE.search(url)
        if not m or m.group("mon").lower() not in _MONTHS:
            continue
        key = (int(m.group("year")), _MONTHS.index(m.group("mon").lower()) + 1)
        revised = bool(m.group("suffix"))
        if key not in by_month or (revised and not by_month[key][0]):
            by_month[key] = (revised, url)
    return [monthly_doc(url, y, mo) for (y, mo), (_, url) in sorted(by_month.items(), reverse=True)]


# --------------------------------------------------------------------------- download


class SourceError(RuntimeError):
    def __init__(self, url: str, message: str, status: int | None = None):
        super().__init__(f"{url}: {message}")
        self.url, self.status = url, status


class SourceBlocked(SourceError):
    """The site refuses automated clients (401/403). Reported, never worked around."""


def fetch(client: httpx.Client, url: str, *, pdf: bool = True, attempts: int = 4) -> tuple[bytes, str]:
    """GET with retries and backoff for transient trouble (network errors, 429/5xx, an HTML error
    page served where a PDF should be). Returns (body, content type)."""
    problem = "unknown error"
    for attempt in range(attempts):
        try:
            resp = client.get(url)
        except httpx.TransportError as exc:
            problem = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code in (401, 403):
                raise SourceBlocked(url, f"HTTP {resp.status_code} (blocked for automated clients)", resp.status_code)
            if resp.status_code in (404, 410):
                raise SourceError(url, f"HTTP {resp.status_code} (gone)", resp.status_code)
            if resp.is_success and (not pdf or b"%PDF-" in resp.content[:1024]):
                return resp.content, resp.headers.get("content-type", "application/pdf" if pdf else "text/html")
            problem = f"HTTP {resp.status_code}" if not resp.is_success else "response is not a PDF"
        log.warning("fetch %s attempt %d failed: %s", url, attempt + 1, problem)
        if attempt < attempts - 1:
            _sleep(2 * (attempt + 1))
    raise SourceError(url, f"failed after {attempts} attempts: {problem}")


# --------------------------------------------------------------------------- sync


def sync_documents(db: Session, monthly_reports: int = 24, progress: Callable[[int, int, str, str], None] | None = None) -> dict:
    """Fetch the regulatory texts and the latest `monthly_reports` AMFI monthly reports and ingest
    whatever is new or changed. Monthly reports are final once published, so ones already stored
    are not downloaded again; the regulatory texts are re-downloaded and compared by hash.

    `progress(done, total, title, status)` is called after each document, status being one of
    new | updated | unchanged | skipped | failed | blocked.
    Returns counts plus `failed` / `blocked` lists of {url, title, error}.
    """
    summary: dict = {"documents": 0, "new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": [], "blocked": [],
                     "pages": 0, "chunks": 0, "text_chars": 0, "scanned_pages": {}}

    def note(title: str, url: str, status: str, error: str | None = None) -> None:
        summary["documents"] += 1
        if status in ("failed", "blocked"):
            summary[status].append({"url": url, "title": title, "error": error})
        else:
            summary[status] += 1
        if progress:
            progress(summary["documents"], total, title, status)

    with http_client() as client:
        monthly: list[SourceDoc] = []
        index_problem: SourceError | None = None
        if monthly_reports > 0:
            try:
                page, _ = fetch(client, MONTHLY_INDEX_URL, pdf=False)
                monthly = parse_monthly_index(page.decode("utf-8", errors="replace"))[:monthly_reports]
                if not monthly:
                    index_problem = SourceError(MONTHLY_INDEX_URL, "no monthly report links found (page layout changed?)")
            except SourceError as exc:
                index_problem = exc
        docs = list(REGULATORY_DOCS) + monthly
        total = len(docs)
        if index_problem:  # not a document; report it and carry on with the regulatory texts
            kind = "blocked" if isinstance(index_problem, SourceBlocked) else "failed"
            summary[kind].append({"url": MONTHLY_INDEX_URL, "title": "AMFI monthly reports index", "error": str(index_problem)})

        known = {d.url: d for d in db.scalars(select(Document).where(Document.doc_type == "monthly_report"))}
        fetched_any = monthly_reports > 0  # the index page counts as a request
        for doc in docs:
            existing = known.get(doc.url)
            stored_meta = existing.meta or {} if existing is not None else {}
            if stored_meta.get("pipeline") == PIPELINE_VERSION and "extraction_error" not in stored_meta:
                note(doc.title, doc.url, "skipped")
                continue
            try:
                if fetched_any:
                    _sleep(PAUSE_S)
                fetched_any = True
                data, content_type = fetch(client, doc.url)
                stored, status = ingest(
                    db, url=doc.url, source=doc.source, doc_type=doc.doc_type, title=doc.title, data=data,
                    content_type=content_type, published_on=doc.published_on, meta=dict(doc.meta),
                )
            except SourceBlocked as exc:
                note(doc.title, doc.url, "blocked", str(exc))
                continue
            except Exception as exc:  # one bad document must not stop the sync
                db.rollback()
                log.exception("sync of %s failed", doc.url)
                note(doc.title, doc.url, "failed", f"{type(exc).__name__}: {exc}")
                continue
            if status != "unchanged":
                meta = stored.meta or {}
                summary["pages"] += meta.get("pages") or 0
                summary["chunks"] += meta.get("chunks") or 0
                summary["text_chars"] += stored.text_chars
                if meta.get("scanned_pages"):
                    summary["scanned_pages"][stored.title] = meta["scanned_pages"]
            note(doc.title, doc.url, status)
    return summary
