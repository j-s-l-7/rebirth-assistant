"""ORM models: the MVP subset of the PRD ERD (§14), plus goals and escalations.

Money is stored as NUMERIC and read back as float (asdecimal=False): exact enough
for display/analytics, and Postgres keeps NUMERIC precision at rest.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def Money():  # noqa: N802 - column factory
    return Numeric(18, 2, asdecimal=False)


def Units():  # noqa: N802
    return Numeric(18, 4, asdecimal=False)


def Nav():  # noqa: N802
    return Numeric(14, 4, asdecimal=False)


# --------------------------------------------------------------------------- identity


class Investor(Base):
    __tablename__ = "investor"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # Shown in the UI only. Never sent to the LLM (PRD §14.5 LLM boundary).
    display_name: Mapped[str] = mapped_column(String(120), default="Investor")
    pan_enc: Mapped[bytes | None] = mapped_column()
    pan_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    pan_masked: Mapped[str | None] = mapped_column(String(16))
    risk_bucket: Mapped[str | None] = mapped_column(String(32))
    tax_slab_pct: Mapped[float | None] = mapped_column(Float)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --------------------------------------------------------------------------- market data


class Scheme(Base):
    """One row per AMFI scheme code (a plan+option combination)."""

    __tablename__ = "scheme"

    amfi_code: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255), index=True)
    amc_name: Mapped[str] = mapped_column(String(120), index=True)
    raw_category: Mapped[str] = mapped_column(String(160))
    category_key: Mapped[str] = mapped_column(String(40), index=True)
    plan_type: Mapped[str] = mapped_column(String(12))  # regular | direct
    option_type: Mapped[str] = mapped_column(String(12))  # growth | idcw
    isin_growth: Mapped[str | None] = mapped_column(String(12), index=True)
    isin_reinvest: Mapped[str | None] = mapped_column(String(12), index=True)
    latest_nav: Mapped[float | None] = mapped_column(Nav())
    latest_nav_date: Mapped[date | None] = mapped_column(Date)
    riskometer: Mapped[str] = mapped_column(String(20))
    riskometer_source: Mapped[str] = mapped_column(String(20), default="category_default")
    in_universe: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_benchmark_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    nav_history_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NavPoint(Base):
    __tablename__ = "nav_history"

    amfi_code: Mapped[int] = mapped_column(ForeignKey("scheme.amfi_code"), primary_key=True)
    nav_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav: Mapped[float] = mapped_column(Nav())


class SchemeHolding(Base):
    """Monthly portfolio disclosure lines (stock-level overlap). PRD: SCHEME_PORTFOLIO."""

    __tablename__ = "scheme_portfolio"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    amfi_code: Mapped[int] = mapped_column(ForeignKey("scheme.amfi_code"), index=True)
    disclosure_month: Mapped[date] = mapped_column(Date)
    security_isin: Mapped[str | None] = mapped_column(String(12))
    security_name: Mapped[str] = mapped_column(String(160))
    sector: Mapped[str | None] = mapped_column(String(80))
    weight_pct: Mapped[float] = mapped_column(Float)


class SchemeMetric(Base):
    __tablename__ = "scheme_metric"
    __table_args__ = (UniqueConstraint("amfi_code", "as_of", "model_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    amfi_code: Mapped[int] = mapped_column(ForeignKey("scheme.amfi_code"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    model_version: Mapped[str] = mapped_column(String(20))
    metrics: Mapped[dict] = mapped_column(JSON)
    composite_score: Mapped[float | None] = mapped_column(Float)
    tier: Mapped[str | None] = mapped_column(String(12))  # core | satellite | watch | exit


class SchemeDetail(Base):
    """AMFI scheme master (DownloadSchemeData): every column of every row, keyed by AMFI code."""

    __tablename__ = "scheme_detail"

    amfi_code: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    amc_full_name: Mapped[str | None] = mapped_column(String(160))
    scheme_base_name: Mapped[str | None] = mapped_column(String(255), index=True)
    scheme_type: Mapped[str | None] = mapped_column(String(40))
    scheme_category: Mapped[str | None] = mapped_column(String(160))
    nav_name: Mapped[str | None] = mapped_column(String(255))
    min_amount: Mapped[float | None] = mapped_column(Money())
    launch_date: Mapped[date | None] = mapped_column(Date)
    closure_date: Mapped[date | None] = mapped_column(Date)
    isins: Mapped[list | None] = mapped_column(JSON)
    raw: Mapped[dict] = mapped_column(JSON)  # the source row, untouched
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SchemeTer(Base):
    """AMFI total expense ratio disclosure, stored as change points: a row is written only when
    any published value changes, so the full daily history is reconstructable without storing
    ~60k unchanged rows a month. Keyed by the NSDL scheme code (shared by a scheme's plans)."""

    __tablename__ = "scheme_ter"
    __table_args__ = (UniqueConstraint("nsdl_code", "effective_from"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    nsdl_code: Mapped[str] = mapped_column(String(40), index=True)
    mf_id: Mapped[int | None] = mapped_column(Integer)
    scheme_name: Mapped[str] = mapped_column(String(255))
    scheme_category: Mapped[str | None] = mapped_column(String(160))
    scheme_type: Mapped[str | None] = mapped_column(String(40))
    effective_from: Mapped[date] = mapped_column(Date)
    r_ber: Mapped[float | None] = mapped_column(Float)
    r_brokerage: Mapped[float | None] = mapped_column(Float)
    r_transaction: Mapped[float | None] = mapped_column(Float)
    r_statutory: Mapped[float | None] = mapped_column(Float)
    r_ter: Mapped[float | None] = mapped_column(Float)
    d_ber: Mapped[float | None] = mapped_column(Float)
    d_brokerage: Mapped[float | None] = mapped_column(Float)
    d_transaction: Mapped[float | None] = mapped_column(Float)
    d_statutory: Mapped[float | None] = mapped_column(Float)
    d_ter: Mapped[float | None] = mapped_column(Float)
    # Pre-April-2026 layout: additional expenses under SEBI Reg. 52(6A)(b) and 52(6A)(c).
    r_6a_b: Mapped[float | None] = mapped_column(Float)
    r_6a_c: Mapped[float | None] = mapped_column(Float)
    d_6a_b: Mapped[float | None] = mapped_column(Float)
    d_6a_c: Mapped[float | None] = mapped_column(Float)


class SchemePerformance(Base):
    """AMFI fund-performance feed: returns vs the scheme's *official* benchmark, plus whatever
    else the feed publishes (riskometer, AUM...). `raw` keeps the source record untouched."""

    __tablename__ = "scheme_performance"
    __table_args__ = (UniqueConstraint("external_key", "plan", "as_of"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    external_key: Mapped[str] = mapped_column(String(120), index=True)
    plan: Mapped[str] = mapped_column(String(12))  # regular | direct
    amfi_code: Mapped[int | None] = mapped_column(Integer, index=True)
    scheme_name: Mapped[str] = mapped_column(String(255))
    as_of: Mapped[date] = mapped_column(Date)
    benchmark: Mapped[str | None] = mapped_column(String(255))
    riskometer: Mapped[str | None] = mapped_column(String(30))
    benchmark_riskometer: Mapped[str | None] = mapped_column(String(30))
    aum_cr: Mapped[float | None] = mapped_column(Float)
    returns: Mapped[dict | None] = mapped_column(JSON)  # {"1y": %, "3y": %, "5y": %, "10y": %, "since_launch": %}
    benchmark_returns: Mapped[dict | None] = mapped_column(JSON)
    raw: Mapped[dict] = mapped_column(JSON)


class SchemeLink(Base):
    """How an external feed's scheme identifier maps to AMFI codes (one row per code and source)."""

    __tablename__ = "scheme_link"

    amfi_code: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    source: Mapped[str] = mapped_column(String(20), primary_key=True)  # ter | performance
    external_key: Mapped[str] = mapped_column(String(120), index=True)
    method: Mapped[str] = mapped_column(String(20))  # exact | fuzzy
    score: Mapped[float] = mapped_column(Float)


class Document(Base):
    """An original document (PDF/Excel/...) kept in full: the file is archived as downloaded and
    its complete text is stored as ordered, non-overlapping chunks in DocumentChunk."""

    __tablename__ = "document"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    source: Mapped[str] = mapped_column(String(20))  # amfi | sebi | amc
    doc_type: Mapped[str] = mapped_column(String(30), index=True)  # regulation | circular | faq | monthly_report | sid | kim | factsheet
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text, unique=True)
    published_on: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    sha256: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str | None] = mapped_column(String(80))
    bytes: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)
    pages: Mapped[int | None] = mapped_column(Integer)
    amc_name: Mapped[str | None] = mapped_column(String(120))
    amfi_code: Mapped[int | None] = mapped_column(Integer)
    text_chars: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict | None] = mapped_column(JSON)


class DocumentChunk(Base):
    """Complete document text, split into ordered passages (concatenating them in `seq` order
    reproduces the full text). On Postgres a generated `tsv` column + GIN index (created in
    init_db) powers full-text search."""

    __tablename__ = "document_chunk"
    __table_args__ = (UniqueConstraint("document_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)


class CategoryStat(Base):
    """Peer distribution captured by a category ranking run, so a fund outside the universe
    (e.g. a held Direct plan) can be scored without re-reading every peer's NAV history."""

    __tablename__ = "category_stat"
    __table_args__ = (UniqueConstraint("category_key", "as_of", "model_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    category_key: Mapped[str] = mapped_column(String(40), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    model_version: Mapped[str] = mapped_column(String(20))
    stats: Mapped[dict] = mapped_column(JSON)


# --------------------------------------------------------------------------- advice & chat


class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="New conversation")
    # The system prompt is frozen at session start: Fable 5.1 thinking blocks are bound
    # to the exact prefix, so it must be replayed byte-for-byte on every turn.
    system_prompt: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatMessage(Base):
    """Append-only transcript. `api_content` is replayed verbatim to the Messages API;
    `display_text` is the compliance-processed text the investor sees."""

    __tablename__ = "chat_message"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_session.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(12))  # user | assistant | system
    api_content: Mapped[list | str] = mapped_column(JSON)
    display_text: Mapped[str | None] = mapped_column(Text)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    guardrail: Mapped[dict | None] = mapped_column(JSON)
    disclosures: Mapped[list | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(40))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    usage: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Escalation(Base):
    __tablename__ = "escalation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("chat_session.id", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    """Append-only trail for sensitive actions (PRD §12 Auditability)."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    actor_type: Mapped[str] = mapped_column(String(16))  # investor | system | staff | ai
    actor_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(60))
    entity_name: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
