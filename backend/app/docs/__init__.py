"""Document layer (Phase 1): keep source documents in full and make them searchable.

    archive.py  original bytes, exactly as downloaded (local dir or Supabase Storage)
    extract.py  PDF text layer, page by page
    chunk.py    ordered, non-overlapping passages with page numbers and headings
    store.py    idempotent ingest into Document / DocumentChunk
    sources.py  discovery + download of the public regulatory texts and AMFI monthly reports
    search.py   full-text search over the passages, for quoting with citations
"""
