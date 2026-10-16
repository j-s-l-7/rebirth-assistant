"""Document layer: extraction, chunking, archive, idempotent ingest, search, source sync.

Everything is offline. PDFs are generated here (a minimal hand-written PDF that pdfium reads),
HTTP goes through httpx.MockTransport, and the archive is redirected to a temp directory.
"""

import hashlib
import random
import re
import textwrap
from collections import Counter
from datetime import date

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.docs import archive, search, sources, store
from app.docs.chunk import assemble, chunk_pages, find_headings, median_line_width, normalize_whitespace
from app.docs.extract import ExtractionError, clean_text, extract_pdf, scanned_pages
from app.models import Document, DocumentChunk

# --------------------------------------------------------------------------- helpers


def make_pdf(pages: list[list[str]]) -> bytes:
    """A tiny valid PDF: one Helvetica text line per list item; an empty list is a page with no text."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, lines in enumerate(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>".encode())
        body = ("BT /F1 11 Tf 13 TL 50 740 Td " + " T* ".join(f"({esc(line)}) Tj" for line in lines) + " ET") if lines else ""
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream".encode())
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


_WORDS = "the distributor shall disclose commission and fees to investors before accepting any application form".split()


def prose_pages(n_pages: int = 6, sentences_per_page: int = 12, seed: int = 1, tag: bool = False) -> list[str]:
    """Hard-wrapped pages of random sentences. With `tag`, sentence k of page p carries the token Sp_k."""
    rnd = random.Random(seed)
    pages = []
    for p in range(1, n_pages + 1):
        sentences = []
        for k in range(sentences_per_page):
            body = " ".join(rnd.choice(_WORDS) for _ in range(rnd.randint(8, 24)))
            sentences.append((f"S{p}x{k} " if tag else "") + body.capitalize() + ".")
        pages.append("\n".join(textwrap.wrap(" ".join(sentences), 80)))
    return pages


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    monkeypatch.setattr(archive, "RAW_DIR", root)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    return root


@pytest.fixture
def docs_db(db, archive_dir):
    def wipe():
        db.rollback()
        db.execute(delete(DocumentChunk))
        db.execute(delete(Document))
        db.commit()

    wipe()
    yield db
    wipe()


def add_doc(db, url, title, doc_type, chunks, published_on=None, source="amfi") -> Document:
    """A Document with chunks given as (heading, text) pairs, bypassing PDF handling."""
    doc = Document(source=source, doc_type=doc_type, title=title, url=url, published_on=published_on, sha256="0" * 64,
                   bytes=1, storage_path="local://raw/x", pages=len(chunks), text_chars=sum(len(t) for _, t in chunks))
    db.add(doc)
    db.flush()
    for seq, (heading, body) in enumerate(chunks):
        db.add(DocumentChunk(document_id=doc.id, seq=seq, page_from=seq + 1, page_to=seq + 1, heading=heading, text=body))
    db.commit()
    return doc


def chunk_ids(db, doc) -> list[str]:
    return list(db.scalars(select(DocumentChunk.id).where(DocumentChunk.document_id == doc.id).order_by(DocumentChunk.seq)))


# --------------------------------------------------------------------------- extraction


def test_clean_text_normalises_pdfium_output():
    raw = "non￾individual\r\n\xa0a  b\x00\x02c  x ﬁnal\r\n\r\n  ​z­"
    assert clean_text(raw) == "non-individual\na bc • x final\nz"


def test_extract_pdf_returns_text_per_page_and_flags_pages_without_text():
    pages = extract_pdf(make_pdf([["Hello page one", "second line (with parens)"], [], ["Third page"]]))
    assert len(pages) == 3
    assert pages[0] == "Hello page one\nsecond line (with parens)"
    assert pages[1] == ""
    assert pages[2] == "Third page"
    assert scanned_pages(pages) == [2]


def test_extract_pdf_rejects_non_pdf():
    with pytest.raises(ExtractionError):
        extract_pdf(b"this is not a pdf")


# --------------------------------------------------------------------------- chunking


@pytest.mark.parametrize("target", [150, 600, 1800])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_chunks_reproduce_the_full_text(target, seed):
    pages = prose_pages(seed=seed)
    chunks = chunk_pages(pages, target)
    text, _ = assemble(pages)
    assert len(chunks) > 1
    # exact partition: contiguous slices of the assembled text, in order
    assert "".join(c["text"] for c in chunks) == text
    # and therefore equal to the extracted pages once whitespace is normalised, however they are joined
    assert normalize_whitespace(" ".join(c["text"] for c in chunks)) == normalize_whitespace("\n".join(pages))
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))
    assert all(0 < len(c["text"]) <= target for c in chunks)
    assert all(1 <= c["page_from"] <= c["page_to"] <= len(pages) for c in chunks)
    assert [c["page_from"] for c in chunks] == sorted(c["page_from"] for c in chunks)


@pytest.mark.parametrize("target", [600, 1800])
def test_chunks_end_on_sentence_boundaries(target):
    chunks = chunk_pages(prose_pages(), target)
    assert all(c["text"].rstrip().endswith(".") for c in chunks[:-1])


def test_page_ranges_follow_the_text():
    pages = ["Alpha one. Alpha two.", "Beta one. Beta two.", "Gamma one. Gamma two."]
    chunks = chunk_pages(pages, 25)
    assert len(chunks) >= 3
    page_of = {"Alpha": 1, "Beta": 2, "Gamma": 3}
    for c in chunks:
        names = re.findall(r"Alpha|Beta|Gamma", c["text"])
        assert (c["page_from"], c["page_to"]) == (page_of[names[0]], page_of[names[-1]]), c


def test_page_ranges_skip_pages_without_text():
    chunks = chunk_pages(["First page text.", "", "Third page text."], 1800)
    assert len(chunks) == 1
    assert (chunks[0]["page_from"], chunks[0]["page_to"]) == (1, 3)
    assert chunk_pages(["", "   "]) == [] and chunk_pages([]) == []


def test_unbreakable_text_is_still_cut_to_size():
    token = "x" * 2500
    chunks = chunk_pages([f"{token}\nshort tail."], 1000)
    assert "".join(c["text"] for c in chunks) == f"{token}\nshort tail."
    assert all(len(c["text"]) <= 1000 for c in chunks)


def test_abbreviations_and_list_numbers_do_not_end_sentences():
    # no real sentence ends inside the first 60 characters, so the cut must not land after "Rs." / "Sr." / "No."
    page = "Pay Rs. 5,000 as per Sr. No. 4 of the schedule that is long enough to pass the window before any real sentence ends. Done."
    first = chunk_pages([page], 60)[0]["text"].rstrip()
    assert not first.endswith(("Rs.", "Sr.", "No.")) and first.endswith("long")
    # a real sentence end is used when one fits, and "1." at a line start is a list number, not a sentence end
    page = "Pay Rs. 5,000 now. Then wait a while.\n1. First item continues here. Done."
    assert [c["text"] for c in chunk_pages([page], 45)][0].rstrip().endswith("Then wait a while.")


@pytest.mark.parametrize(
    "line,next_line,expected",
    [
        ("4.2 Advertisement code", "Distributors must display their ARN.", True),
        ("1. Applicability", "This Code shall be applicable to all Members.", True),
        ("CHAPTER 4: Advertising", "", True),
        ("Chapter 7: Code of Conduct for MFDs", "", True),
        ("Annexure A", "", True),
        ("Annexure 1(a)", "", True),
        ("APPENDIX", "", True),
        ("CODE OF ETHICS", "", True),
        ("FREQUENTLY ASKED QUESTIONS (FAQs)", "", True),
        ("Preamble", "", True),
        ("II Equity Schemes", "i Multi Cap Fund 33 1,20,74,365 6,254.86", True),
        ("B. Obligations of the MFDs", "1. Fiduciary Duty", True),
        ("12.Whether pension advisor is exempted from registration under IA Regulations?", "", True),
        # not headings
        ("2.1. Members and their Key Personnel shall act as fiduciaries and, in the conduct of their", "business, directly", False),
        ("1. As per extant SEBI regulations governing mutual funds, all mutual fund intermediaries", "(i.e., distributors", False),
        ("9. A copy of this Master Circular is available on AMFI website at", "https://www.amfiindia.com", False),
        ("Chapter 1: Guidelines for Registration as a Mutual Fund Distributor......... 4", "", False),
        ("Section 15.7 of the SEBI Master Circular for Mutual Funds dated", "June 27, 2024", False),
        ("Annexure A for details.", "", False),
        ("AMFI/MFD-CIR/32/2025-26 January 14, 2026", "", False),
        ("AMFI/MFD-CIR/32/2025-26", "", False),
        ("Page 3 of 89", "", False),
        ("LAKSHYA OVERNIGHT FUND 1 28", "", False),
        ("i Overnight Fund 40 8,20,060 7,20,356.25 7,27,637.44", "", False),
        ("Pay Rs. 5,000 as per the schedule.", "", False),
        ("ARN", "", False),
        ("10. I am fully aware and agree that –", "", False),
        ("1. X", "", False),
        ("5 Lakh or more", "", False),
    ],
)
def test_heading_detection(line, next_line, expected):
    lines = [line, next_line] if next_line else [line]
    found = find_headings(lines, typical_width=86.0)
    assert bool(found and found[0][0] == 0) is expected, found


def test_question_headings_merge_wrapped_lines():
    lines = ["Q.2 Are MFDs permitted to provide any investment advice while", "choosing MF schemes?",
             "a) MFD are permitted only incidental advice.", "4.Who is required to apply for registration under IA", "Regulations?"]
    assert find_headings(lines) == [
        (0, 2, "Q.2 Are MFDs permitted to provide any investment advice while choosing MF schemes?"),
        (3, 5, "4.Who is required to apply for registration under IA Regulations?"),
    ]


def test_median_line_width_ignores_table_like_text():
    assert median_line_width(["x" * 80] * 5) == 80.0
    assert median_line_width(["Fund 1 234"] * 5) is None


def _sectioned_pages() -> list[str]:
    body = lambda n: " ".join(f"Sentence number {i} explains how a distributor discloses commission." for i in range(n))  # noqa: E731
    return [
        "CODE OF ETHICS\n" + textwrap.fill("Preamble text. " + body(3), 70),
        "4.2 Advertisement code\n" + textwrap.fill(body(14), 70),
        textwrap.fill(body(8), 70) + "\n5.1 Disclosures\n" + textwrap.fill(body(4), 70),
        "Chapter 6: Records\n" + textwrap.fill(body(5), 70),
    ]


def test_chunks_carry_the_heading_they_sit_under():
    chunks = chunk_pages(_sectioned_pages(), 700)
    headings = [c["heading"] for c in chunks]
    assert {"CODE OF ETHICS", "4.2 Advertisement code", "5.1 Disclosures", "Chapter 6: Records"} <= set(headings)
    # sections are contiguous runs of chunks, in document order
    assert [h for i, h in enumerate(headings) if i == 0 or h != headings[i - 1]] == [
        "CODE OF ETHICS", "4.2 Advertisement code", "5.1 Disclosures", "Chapter 6: Records"]
    # a section longer than one chunk: only the first chunk contains the heading line, later ones inherit it
    section = [c for c in chunks if c["heading"] == "4.2 Advertisement code"]
    assert len(section) >= 2
    assert section[0]["text"].startswith("4.2 Advertisement code")
    assert all("4.2 Advertisement code" not in c["text"] for c in section[1:])
    # a chunk is labelled with a heading that it contains or continues
    for prev, c in zip([None] + chunks, chunks):
        assert c["heading"] in c["text"] or (prev and prev["heading"] == c["heading"])


def test_a_chunk_spanning_two_sections_takes_the_heading_of_the_larger_part():
    body = " ".join(f"Sentence {i} says that the distributor discloses commission." for i in range(12))
    page = "1. Introduction\nA short opening remark.\n2. Main section\n" + textwrap.fill(body, 70)
    chunks = chunk_pages([page], 2000)
    assert len(chunks) == 1 and chunks[0]["heading"] == "2. Main section"


@pytest.mark.parametrize("target", [300, 500, 700, 900, 1200])
def test_a_heading_is_never_left_at_the_end_of_a_chunk(target):
    chunks = chunk_pages(_sectioned_pages(), target)
    heading_lines = {"CODE OF ETHICS", "4.2 Advertisement code", "5.1 Disclosures", "Chapter 6: Records"}
    for c in chunks[:-1]:
        assert c["text"].rstrip().splitlines()[-1] not in heading_lines


def test_running_headers_do_not_stretch_page_ranges():
    pages = []
    for p in range(1, 9):
        sentences = " ".join(f"S{p}x{k} The distributor shall disclose the commission paid." for k in range(7))
        pages.append(f"Master Circular for Distributors Page {p} of 8\n" + textwrap.fill(sentences, 80))
    chunks = chunk_pages(pages, 900)
    page_of_token = lambda t: int(re.match(r"S(\d+)x", t).group(1))  # noqa: E731
    for c in chunks:
        tokens = re.findall(r"S\d+x\d+", c["text"])
        assert (c["page_from"], c["page_to"]) == (page_of_token(tokens[0]), page_of_token(tokens[-1]))
    # the next page's header starts the next chunk instead of trailing the previous one
    assert not any(c["text"].rstrip().splitlines()[-1].startswith("Master Circular") for c in chunks[:-1])


def test_headings_with_table_of_contents_leaders_are_not_picked_up():
    toc = ["Table of Contents", "Chapter 1: Registration ........ 4", "Chapter 2: Overseas Distributors ........ 12"]
    assert [h for _, _, h in find_headings(toc)] == ["Table of Contents"]


# --------------------------------------------------------------------------- archive


def test_archive_local_stores_exact_bytes(archive_dir):
    data = bytes(range(256)) * 8
    path = archive.archive_file("regulation", "AMFI_Master.pdf", data, "application/pdf", day=date(2026, 10, 16))
    assert path == "local://raw/regulation/2026-10-16/AMFI_Master.pdf"
    assert archive.local_path(path).read_bytes() == data
    assert not list(archive_dir.rglob("*.part"))
    # default day is today, file names are made safe
    path = archive.archive_file("monthly_report", "../../etc/pass wd?.pdf", b"x", "application/pdf")
    assert re.fullmatch(r"local://raw/monthly_report/\d{4}-\d{2}-\d{2}/pass_wd_.pdf", path)
    assert archive.archive_file("../evil", "a.txt", b"x", "text/plain", day=date(2026, 1, 1)) == "local://raw/evil/2026-01-01/a.txt"
    with pytest.raises(ValueError):
        archive.local_path("local://raw/../../outside")
    with pytest.raises(ValueError):
        archive.local_path("supabase://ra-raw/x")


class _Supabase:
    """Records requests and plays the Storage API."""

    def __init__(self, bucket_response=None, upload_statuses=(200,)):
        self.requests: list[httpx.Request] = []
        self.bucket_response = bucket_response or httpx.Response(200, json={"name": "ra-raw"})
        self.upload_statuses = list(upload_statuses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/storage/v1/bucket":
            return self.bucket_response
        status = self.upload_statuses.pop(0) if len(self.upload_statuses) > 1 else self.upload_statuses[0]
        return httpx.Response(status, json={"Key": "ra-raw/x"} if status == 200 else {"message": f"boom {request.headers['apikey']}"})


@pytest.fixture
def supabase(monkeypatch, archive_dir):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co/")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_TESTKEY")
    monkeypatch.setattr(archive, "_bucket_ready", set())
    monkeypatch.setattr(archive, "_sleep", lambda s: None)

    def install(api: _Supabase) -> _Supabase:
        monkeypatch.setattr(archive, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(api)))
        return api

    return install


def test_archive_uploads_to_supabase_storage(supabase, archive_dir):
    api = supabase(_Supabase())
    path = archive.archive_file("monthly_report", "amsep2026repo.pdf", b"%PDF-bytes", "application/pdf", day=date(2026, 10, 16))
    assert path == "supabase://ra-raw/monthly_report/2026-10-16/amsep2026repo.pdf"
    bucket, upload = api.requests
    assert (bucket.method, str(bucket.url)) == ("POST", "https://proj.supabase.co/storage/v1/bucket")
    assert bucket.read() == b'{"id":"ra-raw","name":"ra-raw","public":false}'
    assert (upload.method, str(upload.url)) == ("POST", "https://proj.supabase.co/storage/v1/object/ra-raw/monthly_report/2026-10-16/amsep2026repo.pdf")
    assert upload.headers["apikey"] == "sb_secret_TESTKEY"
    assert upload.headers["authorization"] == "Bearer sb_secret_TESTKEY"
    assert upload.headers["x-upsert"] == "true"
    assert upload.headers["content-type"] == "application/pdf"
    assert upload.read() == b"%PDF-bytes"
    assert not archive_dir.exists()  # nothing written locally
    # the bucket is only created once per process
    archive.archive_file("monthly_report", "other.pdf", b"x", "application/pdf", day=date(2026, 10, 16))
    assert [r.url.path for r in api.requests].count("/storage/v1/bucket") == 1


def test_archive_accepts_an_existing_bucket(supabase):
    api = supabase(_Supabase(bucket_response=httpx.Response(400, json={"statusCode": "409", "error": "Duplicate", "message": "The resource already exists"})))
    assert archive.archive_file("faq", "a.pdf", b"x", "application/pdf", day=date(2026, 1, 1)).startswith("supabase://ra-raw/faq/")
    assert len(api.requests) == 2


def test_archive_retries_then_fails_without_leaking_the_key(supabase):
    api = supabase(_Supabase(upload_statuses=(500, 500, 500)))
    with pytest.raises(archive.ArchiveError) as err:
        archive.archive_file("faq", "a.pdf", b"x", "application/pdf", day=date(2026, 1, 1))
    assert "sb_secret_TESTKEY" not in str(err.value) and "***" in str(err.value)
    assert [r.url.path for r in api.requests].count("/storage/v1/object/ra-raw/faq/2026-01-01/a.pdf") == 3
    api = supabase(_Supabase(upload_statuses=(500, 200)))
    assert archive.archive_file("faq", "a.pdf", b"x", "application/pdf", day=date(2026, 1, 1)).startswith("supabase://")
    api = supabase(_Supabase(upload_statuses=(403,)))
    with pytest.raises(archive.ArchiveError):  # client errors are not retried
        archive.archive_file("faq", "b.pdf", b"x", "application/pdf", day=date(2026, 1, 1))
    assert sum(r.url.path.endswith("b.pdf") for r in api.requests) == 1


def test_supabase_needs_both_settings(monkeypatch, archive_dir):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    assert archive.archive_file("faq", "a.pdf", b"x", "application/pdf", day=date(2026, 1, 1)).startswith("local://")


# --------------------------------------------------------------------------- ingest


URL = "https://www.amfiindia.com/uploads/sample_circular.pdf"


def _ingest(db, data, **kw):
    args = dict(url=URL, source="amfi", doc_type="circular", title="Sample circular", data=data, content_type="application/pdf")
    return store.ingest_document(db, **{**args, **kw})


def test_ingest_stores_original_text_and_metadata(docs_db, archive_dir):
    pdf = make_pdf([["1. Applicability", "Distributors must display their ARN in all advertisements."], [], ["2. Integrity", "Act with care."]])
    doc = _ingest(docs_db, pdf, published_on=date(2026, 1, 14), meta={"circular_no": "X/1"})
    assert doc.sha256 == hashlib.sha256(pdf).hexdigest() and doc.bytes == len(pdf) and doc.mime == "application/pdf"
    assert doc.storage_path.startswith("local://raw/circular/") and doc.storage_path.endswith(".pdf")
    assert archive.local_path(doc.storage_path).read_bytes() == pdf
    assert (doc.pages, doc.published_on, doc.doc_type, doc.source) == (3, date(2026, 1, 14), "circular", "amfi")
    chunks = docs_db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc.id).order_by(DocumentChunk.seq)).all()
    assert doc.text_chars == sum(len(c.text) for c in chunks) > 0
    assert normalize_whitespace(" ".join(c.text for c in chunks)) == "1. Applicability Distributors must display their ARN in all advertisements. 2. Integrity Act with care."
    assert (chunks[0].page_from, chunks[-1].page_to) == (1, 3)
    assert doc.meta["pages"] == 3 and doc.meta["text_chars"] == doc.text_chars and doc.meta["scanned_pages"] == [2]
    assert doc.meta["circular_no"] == "X/1" and doc.meta["chunks"] == len(chunks)
    assert doc.meta["source_terms"] == "https://www.amfiindia.com/terms-of-use"


def test_ingest_is_idempotent_and_replaces_changed_files(docs_db, archive_dir):
    first = _ingest(docs_db, make_pdf([["Original text about commission disclosure."]]))
    ids, files = chunk_ids(docs_db, first), sorted(archive_dir.rglob("*.pdf"))
    fetched_at, path, sha = first.fetched_at, first.storage_path, first.sha256  # the session hands back one object
    assert len(ids) == 1 and len(files) == 1

    same = _ingest(docs_db, make_pdf([["Original text about commission disclosure."]]))  # identical bytes
    assert same.id == first.id and same.fetched_at == fetched_at and same.storage_path == path
    assert chunk_ids(docs_db, same) == ids and sorted(archive_dir.rglob("*.pdf")) == files

    changed = _ingest(docs_db, make_pdf([["Revised text about advertisement code."], ["Second page."]]))
    assert changed.sha256 != sha and changed.pages == 2 and changed.fetched_at >= fetched_at
    assert changed.storage_path != path and len(sorted(archive_dir.rglob("*.pdf"))) == 2  # old original is kept
    new_ids = chunk_ids(docs_db, changed)
    assert new_ids and not set(new_ids) & set(ids)
    assert docs_db.scalar(select(func.count()).select_from(Document).where(Document.url == URL)) == 1
    assert docs_db.scalar(select(func.count()).select_from(DocumentChunk)) == len(new_ids)
    assert "Revised text" in docs_db.scalars(select(DocumentChunk.text)).first()


def test_ingest_refreshes_descriptive_fields_without_redoing_the_work(docs_db, archive_dir):
    pdf = make_pdf([["Some text."]])
    first = _ingest(docs_db, pdf)
    ids = chunk_ids(docs_db, first)
    again = _ingest(docs_db, pdf, title="Better title", published_on=date(2026, 2, 2), meta={"note": "n"})
    assert again.id == first.id and again.title == "Better title" and again.published_on == date(2026, 2, 2)
    assert again.meta["note"] == "n" and again.meta["pages"] == 1
    assert chunk_ids(docs_db, again) == ids and len(list(archive_dir.rglob("*.pdf"))) == 1


def test_every_document_records_the_terms_of_its_source_site(docs_db):
    pdf = make_pdf([["Some text."]])
    sebi = _ingest(docs_db, pdf, url="https://www.sebi.gov.in/a.pdf", source="sebi")
    assert sebi.meta["source_terms"] == "https://www.sebi.gov.in/website-policy.html"
    amc = _ingest(docs_db, pdf, url="https://amc.example/sid.pdf", source="amc")
    assert "source_terms" in amc.meta and amc.meta["source_terms"] is None  # explicit gap, queryable by legal
    explicit = _ingest(docs_db, pdf, url="https://amc.example/kim.pdf", source="amc", meta={"source_terms": "https://amc.example/terms"})
    assert explicit.meta["source_terms"] == "https://amc.example/terms"
    # a value set after review is not overwritten by a later sync of the unchanged file
    sebi.meta = {**sebi.meta, "source_terms": "https://legal.example/reviewed"}
    docs_db.commit()
    assert _ingest(docs_db, pdf, url="https://www.sebi.gov.in/a.pdf", source="sebi").meta["source_terms"] == "https://legal.example/reviewed"


def test_ingest_reports_what_happened(docs_db):
    kw = dict(url=URL, source="amfi", doc_type="faq", title="T", content_type="application/pdf")
    assert store.ingest(docs_db, data=make_pdf([["a text"]]), **kw)[1] == "new"
    assert store.ingest(docs_db, data=make_pdf([["a text"]]), **kw)[1] == "unchanged"
    assert store.ingest(docs_db, data=make_pdf([["other text"]]), **kw)[1] == "updated"


def test_new_pipeline_version_rechunks_unchanged_files_without_rearchiving(docs_db, archive_dir, monkeypatch):
    pdf = make_pdf([["Some text."]])
    first = _ingest(docs_db, pdf)
    ids, path = chunk_ids(docs_db, first), first.storage_path
    monkeypatch.setattr(store, "PIPELINE_VERSION", store.PIPELINE_VERSION + 1)
    again = _ingest(docs_db, pdf)
    assert again.storage_path == path and len(list(archive_dir.rglob("*.pdf"))) == 1
    assert not set(chunk_ids(docs_db, again)) & set(ids) and again.meta["pipeline"] == store.PIPELINE_VERSION


def test_ingest_keeps_files_it_cannot_read_as_text(docs_db, archive_dir):
    html = _ingest(docs_db, b"<html>hello</html>", url="https://example.org/page.html", content_type="text/html; charset=utf-8")
    assert html.mime == "text/html" and html.pages is None and html.text_chars == 0
    assert html.meta["extraction"].startswith("unsupported") and not chunk_ids(docs_db, html)
    assert archive.local_path(html.storage_path).read_bytes() == b"<html>hello</html>"

    broken = _ingest(docs_db, b"%PDF-1.4 truncated garbage", url="https://example.org/broken.pdf")
    assert "extraction_error" in broken.meta and not chunk_ids(docs_db, broken)
    assert archive.local_path(broken.storage_path).read_bytes() == b"%PDF-1.4 truncated garbage"


def test_ingest_records_pages_without_a_text_layer(docs_db):
    doc = _ingest(docs_db, make_pdf([[], []]))
    assert doc.pages == 2 and doc.text_chars == 0 and doc.meta["scanned_pages"] == [1, 2] and not chunk_ids(docs_db, doc)


# --------------------------------------------------------------------------- search (SQLite fallback)


@pytest.fixture
def corpus(docs_db):
    faq = add_doc(docs_db, "https://x/faq.pdf", "AMFI FAQs", "faq", [
        ("Q.8 Are MFDs permitted to advertise?", "MFDs may advertise their distribution services. Every advertisement must display the ARN of the distributor and must not promise returns."),
        ("Q.15 Records", "Distributors must keep records of documents for their clients."),
    ])
    circular = add_doc(docs_db, "https://x/circular.pdf", "AMFI Master Circular", "circular", [
        ("Commission structure", "Commission payable to a distributor depends on the plan. The distributor must disclose all commission received or receivable to the investor. Commission disclosure must be prominent."),
        ("Code of conduct", "Advertisements by distributors must follow SEBI guidelines on advertisement. Gossip about schemes is prohibited."),
        (None, "Registration of SIP and other systematic plans requires an ARN."),
    ], published_on=date(2026, 1, 14))
    monthly = add_doc(docs_db, "https://x/amsep2026repo.pdf", "AMFI Monthly Report, September 2026", "monthly_report", [
        ("II Equity Schemes", "Net inflow into equity schemes was 38,306 crore in September 2026. SIP inflows continued."),
    ], published_on=date(2026, 9, 30))
    ids = {(d.title, i): chunk_ids(docs_db, d)[i] for d, n in ((faq, 2), (circular, 3), (monthly, 1)) for i in range(n)}
    return docs_db, faq, circular, monthly, ids


def test_query_stems():
    assert search.query_stems("advertisement guidelines for distributors") == ["advertis", "guidelin", "distributor"]
    assert search.query_stems("SIP inflows") == ["sip", "inflow"]
    assert search.query_stems("the of and") == []


def test_search_ranks_by_term_overlap_and_returns_the_citation_fields(corpus):
    db, faq, circular, _, ids = corpus
    hits = search.search_documents(db, "advertisement guidelines for distributors")
    assert [h["chunk_id"] for h in hits[:2]] == [ids["AMFI Master Circular", 1], ids["AMFI FAQs", 0]]
    top = hits[0]
    assert set(top) == {"title", "doc_type", "source", "url", "published_on", "page_from", "page_to", "heading", "snippet",
                        "score", "document_id", "chunk_id"}
    assert (top["title"], top["doc_type"], top["source"], top["url"]) == ("AMFI Master Circular", "circular", "amfi", "https://x/circular.pdf")
    assert (top["published_on"], top["page_from"], top["page_to"], top["heading"]) == ("2026-01-14", 2, 2, "Code of conduct")
    assert top["document_id"] == circular.id and top["score"] > hits[1]["score"] > 0
    assert "guidelines" in top["snippet"] and "\n" not in top["snippet"]
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)


def test_search_prefers_more_terms_and_heading_hits(corpus):
    db, *_ , ids = corpus
    hits = search.search_documents(db, "commission disclosure")
    assert hits[0]["chunk_id"] == ids["AMFI Master Circular", 0] and hits[0]["heading"] == "Commission structure"
    assert len(hits) == 1  # no other passage mentions either term


def test_search_matches_words_not_substrings(corpus):
    db, *_, ids = corpus
    hits = search.search_documents(db, "SIP")
    assert {h["chunk_id"] for h in hits} == {ids["AMFI Master Circular", 2], ids["AMFI Monthly Report, September 2026", 0]}  # not "Gossip"
    both = search.search_documents(db, "SIP inflows")
    assert both[0]["chunk_id"] == ids["AMFI Monthly Report, September 2026", 0]


def test_search_filters_by_document_type_and_limit(corpus):
    db, *_ = corpus
    only_faq = search.search_documents(db, "distributor advertisement", doc_types=["faq"])
    assert only_faq and {h["doc_type"] for h in only_faq} == {"faq"}
    assert len(search.search_documents(db, "distributor", limit=1)) == 1
    assert {h["doc_type"] for h in search.search_documents(db, "distributor", doc_types=[])} >= {"faq", "circular"}
    assert search.search_documents(db, "distributor", doc_types=["sid"]) == []


def test_search_edge_cases(corpus):
    db, *_ = corpus
    for query in ("", "   ", "the of and", "zzzqqq"):
        assert search.search_documents(db, query) == []
    assert search.search_documents(db, "distributor", limit=0) == []
    assert search.search_documents(db, "x'; DROP TABLE zzqq; --") == []  # quotes are inert
    assert db.scalar(select(func.count()).select_from(Document)) == 3


def test_search_snippet_is_a_window_around_the_match(docs_db):
    filler = "Unrelated sentence about registration of the entity. " * 40
    add_doc(docs_db, "https://x/long.pdf", "Long", "circular", [("Heading", filler + "The ARN must be displayed in every advertisement. " + filler)])
    (hit,) = search.search_documents(docs_db, "ARN advertisement")
    assert "ARN must be displayed" in hit["snippet"] and len(hit["snippet"]) <= search.SNIPPET_CHARS + 10


def test_search_over_ingested_pdfs(docs_db):
    _ingest(docs_db, make_pdf([["4.2 Advertisement code", "Distributors must display their ARN in all advertisements."]]),
            url="https://x/a.pdf", title="Code", published_on=date(2026, 1, 1))
    _ingest(docs_db, make_pdf([["Something unrelated about folios."]]), url="https://x/b.pdf", title="Other")
    (hit,) = search.search_documents(docs_db, "ARN in advertisements")
    assert hit["title"] == "Code" and hit["heading"] == "4.2 Advertisement code" and (hit["page_from"], hit["page_to"]) == (1, 1)


# --------------------------------------------------------------------------- search (Postgres statement)


def test_postgres_statement_respects_schema_and_binds_user_values():
    sql = search.pg_sql("mf", loose=False, doc_types=True, exclude=False)
    assert '"mf".document_chunk' in sql and '"mf".document ' in sql
    assert "websearch_to_tsquery('english', :q)" in sql and "1 + ts_rank_cd(c.tsv" in sql and "ts_headline('english'" in sql
    assert "c.tsv @@ q.tsq AND d.doc_type IN :doc_types" in sql and ":exclude" not in sql and ":t0" not in sql
    loose = search.pg_sql('we"ird', loose=True, doc_types=False, exclude=True, terms=3)
    assert '"we""ird".document_chunk' in loose and "c.id NOT IN :exclude" in loose
    assert "websearch_to_tsquery" not in loose and "doc_types" not in loose and "1 + ts_rank_cd" not in loose
    # one bind parameter per term; coverage = terms present / terms asked
    assert all(f"to_tsquery('english', :t{i}) AS t{i}" in loose for i in range(3)) and ":t3" not in loose
    assert "(c.tsv @@ q.t0)::int + (c.tsv @@ q.t1)::int + (c.tsv @@ q.t2)::int" in loose and "/ 3, 2)" in loose


# --------------------------------------------------------------------------- sources


INDEX_HTML = """
<html><body>
<a href="https://portal.amfiindia.com/spages/amsep2026repo.pdf">PDF</a>
<a href="https://portal.amfiindia.com/spages/amsep2026repo.xls">Excel</a>
<a href="https://portal.amfiindia.com/spages/amaug2026repo.pdf">PDF</a>
<a href="/spages/amjul2026repo.pdf">relative</a>
<a href="https://portal.amfiindia.com/spages/amjan2022repo.pdf">PDF</a>
<a href="https://portal.amfiindia.com/spages/amjan2022reporevised.pdf">PDF</a>
<a href="https://portal.amfiindia.com/spages/amdec2021reporevised.pdf">PDF</a>
<a href="https://portal.amfiindia.com/spages/amapr2008repo.htm">old</a>
<a href="https://portal.amfiindia.com/spages/amxyz2026repo.pdf">not a month</a>
<a href="https://www.amfiindia.com/privacy-notice">privacy</a>
<a href="https://portal.amfiindia.com/spages/amsep2026repo.pdf">duplicate</a>
</body></html>
"""


def test_parse_monthly_index_infers_months_from_file_names():
    docs = sources.parse_monthly_index(INDEX_HTML)
    assert [d.meta["period"] for d in docs] == ["2026-09", "2026-08", "2026-07", "2022-01", "2021-12"]
    assert docs[0].url == "https://portal.amfiindia.com/spages/amsep2026repo.pdf"
    assert docs[2].url == "https://www.amfiindia.com/spages/amjul2026repo.pdf"  # relative link resolved against the page
    assert docs[3].url.endswith("amjan2022reporevised.pdf")  # a revised file replaces the original
    first = docs[0]
    assert (first.source, first.doc_type, first.title, first.published_on) == (
        "amfi", "monthly_report", "AMFI Monthly Report, September 2026", date(2026, 9, 30))
    assert sources.parse_monthly_index("<html></html>") == []


def test_the_four_regulatory_sources_are_declared():
    kinds = {d.title: (d.source, d.doc_type) for d in sources.REGULATORY_DOCS}
    assert len(kinds) == 4 and set(kinds.values()) == {("amfi", "regulation"), ("amfi", "circular"), ("amfi", "faq"), ("sebi", "faq")}
    assert all(d.url.startswith("https://") and d.url.endswith(".pdf") for d in sources.REGULATORY_DOCS)


@pytest.fixture
def no_waiting(monkeypatch):
    monkeypatch.setattr(sources, "_sleep", lambda s: None)


def _client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_fetch_retries_transient_failures(no_waiting):
    calls = Counter()
    responses = [httpx.Response(503), httpx.Response(200, text="<html>error page</html>"), httpx.Response(200, content=b"%PDF-1.4 ok", headers={"content-type": "application/pdf"})]

    def handler(request):
        calls[str(request.url)] += 1
        return responses.pop(0)

    data, content_type = sources.fetch(_client_for(handler), "https://x/doc.pdf")
    assert data == b"%PDF-1.4 ok" and content_type == "application/pdf" and calls["https://x/doc.pdf"] == 3


def test_fetch_retries_network_errors_then_gives_up(no_waiting):
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(sources.SourceError, match="failed after 2 attempts"):
        sources.fetch(_client_for(handler), "https://x/doc.pdf", attempts=2)


@pytest.mark.parametrize("status,error", [(403, sources.SourceBlocked), (401, sources.SourceBlocked), (404, sources.SourceError)])
def test_fetch_does_not_retry_or_work_around_refusals(no_waiting, status, error):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(status)

    with pytest.raises(error) as err:
        sources.fetch(_client_for(handler), "https://x/doc.pdf")
    assert len(seen) == 1 and err.value.status == status
    assert isinstance(err.value, sources.SourceBlocked) == (status in (401, 403))


class _Web:
    """A fake of the four regulatory sites plus the AMFI monthly page."""

    def __init__(self):
        self.hits: Counter = Counter()
        self.status: dict[str, int] = {}
        self.index = INDEX_HTML

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] += 1
        if url in self.status:
            return httpx.Response(self.status[url])
        if url == sources.MONTHLY_INDEX_URL:
            return httpx.Response(200, text=self.index, headers={"content-type": "text/html"})
        return httpx.Response(200, content=make_pdf([[f"Document at {url}", "Distributors must disclose commission."]]),
                              headers={"content-type": "application/pdf"})

    def count(self, fragment: str) -> int:
        return sum(n for u, n in self.hits.items() if fragment in u)


@pytest.fixture
def web(monkeypatch, no_waiting, docs_db):
    site = _Web()
    monkeypatch.setattr(sources, "_client", lambda: _client_for(site))
    return site


def test_sync_documents_ingests_new_and_skips_unchanged(web, docs_db):
    blocked_url = next(d.url for d in sources.REGULATORY_DOCS if d.source == "sebi")
    web.status[blocked_url] = 403
    events = []
    first = sources.sync_documents(docs_db, monthly_reports=2, progress=lambda *a: events.append(a))
    assert (first["documents"], first["new"], first["unchanged"], first["skipped"]) == (6, 5, 0, 0)
    assert first["failed"] == [] and [b["url"] for b in first["blocked"]] == [blocked_url]
    assert first["chunks"] >= 5 and first["pages"] == 5 and first["text_chars"] > 0
    assert [e[:2] for e in events] == [(i, 6) for i in range(1, 7)]
    assert Counter(e[3] for e in events) == {"new": 5, "blocked": 1}
    stored = docs_db.scalars(select(Document).order_by(Document.title)).all()
    assert [d.doc_type for d in stored].count("monthly_report") == 2
    assert {d.title for d in stored if d.doc_type == "monthly_report"} == {"AMFI Monthly Report, September 2026", "AMFI Monthly Report, August 2026"}
    assert all(d.meta["source_terms"] == store.SOURCE_TERMS["amfi"] for d in stored)

    second = sources.sync_documents(docs_db, monthly_reports=2)
    assert (second["documents"], second["new"], second["updated"], second["unchanged"], second["skipped"]) == (6, 0, 0, 3, 2)
    assert len(second["blocked"]) == 1
    # published reports are final: downloaded once, not again; the regulatory texts are re-checked every time
    assert web.count("amsep2026repo.pdf") == 1 and web.count("amaug2026repo.pdf") == 1 and web.count("AMFI_Master") == 2

    web.status.clear()
    third = sources.sync_documents(docs_db, monthly_reports=2)
    assert (third["new"], third["unchanged"], third["skipped"], third["blocked"]) == (1, 3, 2, [])


def test_sync_documents_reports_a_blocked_index_and_carries_on(web, docs_db):
    web.status[sources.MONTHLY_INDEX_URL] = 403
    summary = sources.sync_documents(docs_db, monthly_reports=5)
    assert summary["documents"] == 4 and summary["new"] == 4
    assert [(b["url"], "403" in b["error"]) for b in summary["blocked"]] == [(sources.MONTHLY_INDEX_URL, True)]


def test_sync_documents_survives_one_bad_document(web, docs_db):
    bad = sources.REGULATORY_DOCS[0].url
    web.status[bad] = 404
    summary = sources.sync_documents(docs_db, monthly_reports=0)
    assert summary["documents"] == 4 and summary["new"] == 3
    assert [f["url"] for f in summary["failed"]] == [bad] and "404" in summary["failed"][0]["error"]


def test_sync_documents_picks_the_most_recent_months(web, docs_db):
    sources.sync_documents(docs_db, monthly_reports=3)
    periods = sorted(d.meta["period"] for d in docs_db.scalars(select(Document).where(Document.doc_type == "monthly_report")))
    assert periods == ["2026-07", "2026-08", "2026-09"]
