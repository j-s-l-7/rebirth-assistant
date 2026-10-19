"""Idempotent ingest of one document: archive the original, store its full text as chunks.

Identity is the URL. Same URL and same sha256 is "unchanged" and costs nothing (apart from a
metadata refresh); new bytes under a known URL are re-archived and the chunks replaced. The
file is archived first, then the Document row and all its chunks are written in a single
transaction, so a failure never leaves a document half-chunked.
"""

import hashlib
import logging
import os
from datetime import date, datetime, timezone
from urllib.parse import unquote, urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.docs.archive import archive_file
from app.docs.chunk import chunk_pages
from app.docs.extract import ExtractionError, extract_pdf, extractor_name, scanned_pages
from app.models import Document, DocumentChunk

log = logging.getLogger(__name__)

# Bump when extraction or chunking changes: the next ingest of a known file then re-chunks it
# (from the bytes in hand; the archived original is kept as is).
PIPELINE_VERSION = 1

# Where each source site's usage terms live, recorded in Document.meta["source_terms"] so legal
# can review what is stored. AMFI: "personal and non-commercial use only", no storing of a
# significant portion of the site. SEBI: reproduction after prior permission, with attribution.
SOURCE_TERMS: dict[str, str] = {
    "amfi": "https://www.amfiindia.com/terms-of-use",
    "sebi": "https://www.sebi.gov.in/website-policy.html",
}


def _is_pdf(data: bytes, content_type: str) -> bool:
    return b"%PDF-" in data[:1024] or "pdf" in content_type.lower()


def _archive_name(url: str, sha: str, content_type: str) -> str:
    """Original file name plus a short content hash: different files never share an archive path,
    and a changed file keeps its predecessor on disk."""
    base = unquote(urlparse(url).path).rsplit("/", 1)[-1] or "document"
    stem, ext = os.path.splitext(base)
    if not ext and "pdf" in content_type.lower():
        ext = ".pdf"
    return f"{stem}-{sha[:8]}{ext}"


def _is_current(doc: Document) -> bool:
    meta = doc.meta or {}
    return meta.get("pipeline") == PIPELINE_VERSION and "extraction_error" not in meta


def _refresh_metadata(doc: Document, *, source: str, doc_type: str, title: str, published_on: date | None,
                      amc_name: str | None, amfi_code: int | None, meta: dict | None) -> bool:
    """Bring descriptive fields of an unchanged document up to date. Returns True if anything changed."""
    changed = False
    wanted = {"source": source[:20], "doc_type": doc_type[:30], "title": title[:300]}
    wanted.update({k: v for k, v in (("published_on", published_on), ("amc_name", amc_name), ("amfi_code", amfi_code)) if v is not None})
    for field, value in wanted.items():
        if getattr(doc, field) != value:
            setattr(doc, field, value)
            changed = True
    current = doc.meta or {}
    merged = {**current, "source_terms": current.get("source_terms") or SOURCE_TERMS.get(source), **(meta or {})}
    if merged != current:
        doc.meta = merged
        changed = True
    return changed


def ingest(db: Session, *, url: str, source: str, doc_type: str, title: str, data: bytes, content_type: str,
           published_on: date | None = None, amc_name: str | None = None, amfi_code: int | None = None,
           meta: dict | None = None) -> tuple[Document, str]:
    """Archive `data` and store its full text as ordered chunks, replacing earlier chunks if the file at `url`
    changed. Returns the document and what happened: "new", "updated" or "unchanged" (same url and sha256)."""
    sha = hashlib.sha256(data).hexdigest()
    doc = db.scalar(select(Document).where(Document.url == url))
    if doc is not None and doc.sha256 == sha and _is_current(doc):
        if _refresh_metadata(doc, source=source, doc_type=doc_type, title=title, published_on=published_on,
                             amc_name=amc_name, amfi_code=amfi_code, meta=meta):
            db.commit()
        return doc, "unchanged"

    mime = content_type.split(";")[0].strip()[:80] or None
    same_bytes = doc is not None and doc.sha256 == sha
    storage_path = doc.storage_path if same_bytes else archive_file(doc_type, _archive_name(url, sha, content_type), data, content_type)

    pages: list[str] | None = None
    error: str | None = None
    if _is_pdf(data, content_type):
        try:
            pages = extract_pdf(data)
        except ExtractionError as exc:
            error = str(exc)[:300]
            log.warning("could not extract text from %s: %s", url, exc)
    chunks = chunk_pages(pages) if pages else []
    text_chars = sum(len(c["text"]) for c in chunks)
    info: dict = {
        "pipeline": PIPELINE_VERSION,
        "pages": len(pages) if pages is not None else None,
        "text_chars": text_chars,
        "chunks": len(chunks),
        "scanned_pages": scanned_pages(pages) if pages is not None else [],
        "extractor": extractor_name() if pages is not None else None,
    }
    if error:
        info["extraction_error"] = error
    elif pages is None:
        info["extraction"] = f"unsupported content type: {mime}"

    fields = dict(
        source=source[:20], doc_type=doc_type[:30], title=title[:300], published_on=published_on,
        fetched_at=datetime.now(timezone.utc), sha256=sha, mime=mime, bytes=len(data), storage_path=storage_path,
        pages=info["pages"], amc_name=amc_name, amfi_code=amfi_code, text_chars=text_chars,
        meta={"source_terms": SOURCE_TERMS.get(source), **(meta or {}), **info},
    )
    status = "new" if doc is None else "updated"
    try:
        if doc is None:
            doc = Document(url=url, **fields)
            db.add(doc)
            db.flush()
        else:
            for field, value in fields.items():
                setattr(doc, field, value)
            db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
        db.add_all(DocumentChunk(document_id=doc.id, **c) for c in chunks)
        db.commit()
    except Exception:
        db.rollback()
        raise
    log.info("%s %s: %d pages, %d chunks, %d chars", status, url, info["pages"] or 0, len(chunks), text_chars)
    return doc, status
