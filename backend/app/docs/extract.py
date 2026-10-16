"""PDF text extraction (pypdfium2) plus clean-up of what pdfium emits.

The text is what the PDF's text layer provides, page by page: nothing is summarised or
dropped, only encoding debris is normalised (see `clean_text`). Pages without a text layer
(scans) come back empty and are reported by `scanned_pages`; there is no OCR.
"""

import re
import unicodedata

import pypdfium2 as pdfium


class ExtractionError(ValueError):
    """The bytes are not a readable PDF (corrupt, encrypted, not a PDF at all)."""


# pdfium writes U+FFFE where a line-ending hyphen was absorbed ("non￾individual" is
# "non-individual", "25-Jun￾2026" is "25-Jun-2026", and it even sits inside URLs),
# so it is restored as a hyphen rather than dropped.
_HYPHEN_MARK = "￾"
_LINE_BREAKS = re.compile(r"\r\n|[\r\x0b\x0c\x85  ]")
# C0/C1 controls other than \t and \n; NUL in particular is rejected by Postgres text columns.
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x84\x86-\x9f￿]")
# Soft hyphen, zero-width characters, BOM.
_INVISIBLE = re.compile(r"[­​-‍⁠﻿]")
# Wingdings/Symbol bullets and checkboxes land in the Private Use Area.
_PRIVATE_USE = re.compile(r"[-]")
_LIGATURES = re.compile(r"[ﬀ-ﬆ]")
_HSPACE = re.compile(r"[ \t]+")


def clean_text(raw: str) -> str:
    """Normalise one page of pdfium text: LF line breaks, no control/invisible characters,
    ligatures expanded, bullets unified, runs of spaces collapsed, blank lines dropped."""
    s = _LINE_BREAKS.sub("\n", raw)
    s = s.replace(_HYPHEN_MARK, "-")
    s = _CONTROLS.sub("", s)
    s = _INVISIBLE.sub("", s)
    s = _PRIVATE_USE.sub("•", s)
    s = _LIGATURES.sub(lambda m: unicodedata.normalize("NFKC", m.group()), s)
    # Unicode spaces (NBSP, thin space, ideographic space...) become plain spaces.
    s = "".join(" " if ch != " " and ch != "\n" and unicodedata.category(ch) == "Zs" else ch for ch in s)
    lines = (_HSPACE.sub(" ", line).strip() for line in s.split("\n"))
    return "\n".join(line for line in lines if line)


def extract_pdf(data: bytes) -> list[str]:
    """Text of each page, in order (index 0 is page 1). Scanned pages are empty strings."""
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:  # pypdfium2 raises PdfiumError, but also ValueError/TypeError on junk input
        raise ExtractionError(f"unreadable PDF: {exc}") from exc
    try:
        pages: list[str] = []
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                try:
                    pages.append(clean_text(textpage.get_text_range()))
                finally:
                    textpage.close()
            except pdfium.PdfiumError as exc:
                raise ExtractionError(f"page {i + 1}: {exc}") from exc
            finally:
                page.close()
        return pages
    finally:
        pdf.close()


def scanned_pages(pages: list[str]) -> list[int]:
    """1-based numbers of pages with no text layer (image-only scans, or genuinely blank)."""
    return [i for i, text in enumerate(pages, 1) if not text.strip()]


def extractor_name() -> str:
    """Recorded in Document.meta so a text-quality question can be traced to the extractor."""
    return f"pypdfium2 {pdfium.version.PYPDFIUM_INFO} (pdfium {pdfium.version.PDFIUM_INFO})"
