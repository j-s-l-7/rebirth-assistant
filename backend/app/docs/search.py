"""Full-text search over document chunks, so the copilot can quote passages with citations.

Postgres uses the generated `tsv` column (heading weighted A, text B) and its GIN index:
`websearch_to_tsquery` ranked with `ts_rank_cd`, snippets from `ts_headline`. That query
requires every term, so when it returns fewer than `limit` passages the list is topped up from
a relaxed any-term query, ranked by how many of the terms a passage covers (strict matches
always come first; scores above 1 are strict matches).

SQLite has no tsvector: candidates are LIKE matches on stemmed query terms, scored in Python
by term overlap (distinct terms matched, repeats, heading hits, proximity). Both dialects
return the same result shape.
"""

import math
import re

from sqlalchemy import bindparam, func, or_, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Document, DocumentChunk

MAX_LIMIT = 50
SNIPPET_CHARS = 320

_STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i if in into is it its of on or that the their there these this "
    "to was what when where which who whom why will with about any all my your you should would could may might must "
    "not no than then them they we".split()
)
_SUFFIXES = ("ements", "ement", "ations", "ation", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "s", "e", "y")


def _words(query: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", query.lower())


def _stem(word: str) -> str:
    """Crude suffix stripping, enough that 'distributors' finds 'distributor' and
    'advertisements' finds 'advertising' (all reduce to the prefix 'advertis')."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def query_stems(query: str) -> list[str]:
    seen: list[str] = []
    for w in _words(query):
        if len(w) >= 2 and w not in _STOPWORDS and (s := _stem(w)) not in seen:
            seen.append(s)
    return seen


# --------------------------------------------------------------------------- shared


def _hit(row: dict, snippet: str, score: float) -> dict:
    published = row["published_on"]
    return {
        "title": row["title"],
        "doc_type": row["doc_type"],
        "source": row["source"],
        "url": row["url"],
        "published_on": published.isoformat() if published else None,
        "page_from": row["page_from"],
        "page_to": row["page_to"],
        "heading": row["heading"],
        "snippet": snippet,
        "score": round(float(score), 4),
        "document_id": row["document_id"],
        "chunk_id": row["chunk_id"],
    }


def _flat(s: str) -> str:
    return " ".join(s.split())


# --------------------------------------------------------------------------- Postgres

_PG_SELECT = """
SELECT top.chunk_id, top.document_id, top.title, top.doc_type, top.source, top.url, top.published_on,
       top.page_from, top.page_to, top.heading, top.score,
       ts_headline('english', top.text, top.tsq, 'MaxWords=55, MinWords=25, ShortWord=2, MaxFragments=0') AS snippet
FROM (
    SELECT c.id AS chunk_id, c.document_id, c.seq, c.page_from, c.page_to, c.heading, c.text,
           d.title, d.doc_type, d.source, d.url, d.published_on,
           {score} AS score, q.tsq
    FROM {schema}.document_chunk c
    JOIN {schema}.document d ON d.id = c.document_id
    CROSS JOIN (SELECT {tsquery} AS tsq{term_queries}) q
    WHERE c.tsv @@ q.tsq{filters}
    ORDER BY score DESC, d.published_on DESC NULLS LAST, c.seq
    LIMIT :limit
) top
ORDER BY top.score DESC, top.published_on DESC NULLS LAST, top.seq
"""
_RANK = "ts_rank_cd(c.tsv, q.tsq, 33)"  # 33 = divide by 1 + log(length), then squash into 0..1
_TYPES = " AND d.doc_type IN :doc_types"
_EXCLUDE = " AND c.id NOT IN :exclude"
MAX_TERMS = 8


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def pg_sql(schema: str, *, loose: bool, doc_types: bool, exclude: bool, terms: int = 0) -> str:
    """The Postgres statement. Only fixed fragments and the quoted schema are interpolated; every
    user-supplied value is a bind parameter.

    Strict: `websearch_to_tsquery` (all terms, quotes, -negation), score = 1 + ts_rank_cd, so a
    score above 1 means every term matched. Loose (`terms` > 0): any of the terms, score =
    ts_rank_cd x (share of the terms present)^2, which is below 1, so a passage that repeats one
    common word cannot outrank one that covers more of the query."""
    if loose:
        matched = " + ".join(f"(c.tsv @@ q.t{i})::int" for i in range(terms))
        term_queries = "".join(f", to_tsquery('english', :t{i}) AS t{i}" for i in range(terms))
        score, tsquery = f"{_RANK} * power(({matched})::float8 / {terms}, 2)", "to_tsquery('english', :q)"
    else:
        term_queries, score, tsquery = "", f"1 + {_RANK}", "websearch_to_tsquery('english', :q)"
    filters = (_TYPES if doc_types else "") + (_EXCLUDE if exclude else "")
    return _PG_SELECT.format(schema=quote_ident(schema), tsquery=tsquery, term_queries=term_queries, score=score, filters=filters)


def _pg_query(db: Session, q: str, *, terms: list[str], limit: int, doc_types: list[str] | None, exclude: list[str]) -> list[dict]:
    sql = text(pg_sql(get_settings().db_schema, loose=bool(terms), doc_types=bool(doc_types), exclude=bool(exclude), terms=len(terms)))
    params: dict = {"q": q, "limit": limit, **{f"t{i}": t for i, t in enumerate(terms)}}
    if doc_types:
        sql = sql.bindparams(bindparam("doc_types", expanding=True))
        params["doc_types"] = list(doc_types)
    if exclude:
        sql = sql.bindparams(bindparam("exclude", expanding=True))
        params["exclude"] = exclude
    hits = []
    for row in db.execute(sql, params).mappings():
        snippet = _flat(re.sub(r"</?b>", "", row["snippet"] or ""))
        hits.append(_hit(row, snippet, row["score"]))
    return hits


def _search_postgres(db: Session, query: str, limit: int, doc_types: list[str] | None) -> list[dict]:
    hits = _pg_query(db, query, terms=[], limit=limit, doc_types=doc_types, exclude=[])
    terms = list(dict.fromkeys(w for w in _words(query) if len(w) >= 2 and w not in _STOPWORDS))[:MAX_TERMS]
    if len(hits) < limit and len(terms) > 1:
        hits += _pg_query(db, " | ".join(terms), terms=terms, limit=limit - len(hits), doc_types=doc_types,
                          exclude=[h["chunk_id"] for h in hits])
    return hits


# --------------------------------------------------------------------------- SQLite (and other)


def _pattern(stem: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(stem), re.I)


def _overlap_score(heading: str, body: str, patterns: list[re.Pattern], stems: list[str]) -> float:
    matched, weight = 0, 0.0
    for rx in patterns:
        in_body, in_heading = len(rx.findall(body)), len(rx.findall(heading))
        if in_body or in_heading:
            matched += 1
            weight += (1 + math.log(in_body) if in_body else 0.0) + (2.5 if in_heading else 0.0)
    if not matched:
        return 0.0
    for a, b in zip(stems, stems[1:]):  # neighbouring query terms close together read as a phrase
        if re.search(rf"\b{re.escape(a)}\w*(?:\W+\w+){{0,3}}?\W+{re.escape(b)}", body, re.I):
            weight += 0.75
    words = max(1, len(body.split()))
    return (matched / len(patterns)) ** 2 * weight / (1 + math.log(1 + words / 300))


def _snippet(body: str, patterns: list[re.Pattern], width: int = SNIPPET_CHARS) -> str:
    """The `width`-char window holding the most distinct query terms, starting at a sentence if one is near."""
    flat = _flat(body)
    if len(flat) <= width:
        return flat
    spots = sorted({m.start() for rx in patterns for m in rx.finditer(flat)})[:300]
    if not spots:
        return flat[:width].rsplit(" ", 1)[0] + " …"
    best_start, best_terms = 0, -1
    for p in spots:
        start = max(0, p - 60)
        terms = sum(1 for rx in patterns if rx.search(flat, start, start + width))
        if terms > best_terms:
            best_start, best_terms = start, terms
    start = best_start
    sentence_starts = list(re.finditer(r"[.!?]\s+(?=[A-Z0-9“\"(])", flat[max(0, start - 100) : start]))
    if sentence_starts:
        start = max(0, start - 100) + sentence_starts[-1].end()
    elif start:
        start = flat.find(" ", start) + 1 or start
    end = start + width
    if end < len(flat):
        end = flat.rfind(" ", start, end) if " " in flat[start:end] else end
    return ("… " if start else "") + flat[start:end].strip() + (" …" if end < len(flat) else "")


def _search_overlap(db: Session, query: str, limit: int, doc_types: list[str] | None) -> list[dict]:
    stems = query_stems(query)
    if not stems:
        return []
    patterns = [_pattern(s) for s in stems]
    like = [func.lower(col).like(f"%{s}%") for s in stems for col in (DocumentChunk.text, DocumentChunk.heading)]
    stmt = select(DocumentChunk, Document).join(Document, Document.id == DocumentChunk.document_id).where(or_(*like))
    if doc_types:
        stmt = stmt.where(Document.doc_type.in_(doc_types))
    scored = []
    for chunk, doc in db.execute(stmt):
        score = _overlap_score(chunk.heading or "", chunk.text, patterns, stems)
        if score > 0:
            scored.append((score, doc.published_on.toordinal() if doc.published_on else 0, -chunk.seq, chunk, doc))
    scored.sort(key=lambda t: t[:3], reverse=True)
    hits = []
    for score, _, _, chunk, doc in scored[:limit]:
        row = {
            "title": doc.title, "doc_type": doc.doc_type, "source": doc.source, "url": doc.url,
            "published_on": doc.published_on, "page_from": chunk.page_from, "page_to": chunk.page_to,
            "heading": chunk.heading, "document_id": doc.id, "chunk_id": chunk.id,
        }
        hits.append(_hit(row, _snippet(chunk.text, patterns), score))
    return hits


# --------------------------------------------------------------------------- public


def search_documents(db: Session, query: str, limit: int = 5, doc_types: list[str] | None = None) -> list[dict]:
    """Best-matching passages for `query`, best first, as
    `{title, doc_type, source, url, published_on, page_from, page_to, heading, snippet, score,
    document_id, chunk_id}` (`published_on` is an ISO date or None; pages are PDF page numbers).
    `doc_types` limits the search to those document types; empty or None means all."""
    query = query.strip()
    limit = max(0, min(limit, MAX_LIMIT))
    if not query or not limit:
        return []
    if db.get_bind().dialect.name == "postgresql":
        return _search_postgres(db, query, limit, doc_types)
    return _search_overlap(db, query, limit, doc_types)
