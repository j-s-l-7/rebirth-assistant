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
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    folios: Mapped[list["Folio"]] = relationship(back_populates="investor", cascade="all, delete-orphan")
    goals: Mapped[list["Goal"]] = relationship(back_populates="investor", cascade="all, delete-orphan")


class Consent(Base):
    __tablename__ = "consent"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(40))  # import_analysis | ai_processing | marketing
    notice_version: Mapped[str] = mapped_column(String(20))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RiskProfile(Base):
    __tablename__ = "risk_profile"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    questionnaire_version: Mapped[str] = mapped_column(String(20))
    answers: Mapped[dict] = mapped_column(JSON)
    score: Mapped[int] = mapped_column(Integer)
    bucket: Mapped[str] = mapped_column(String(32))
    horizon_years: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_till: Mapped[date] = mapped_column(Date)


class Goal(Base):
    __tablename__ = "goal"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    target_amount: Mapped[float] = mapped_column(Money())
    target_date: Mapped[date] = mapped_column(Date)
    current_allocated: Mapped[float] = mapped_column(Money(), default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    investor: Mapped[Investor] = relationship(back_populates="goals")


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


# --------------------------------------------------------------------------- portfolio


class ImportJob(Base):
    __tablename__ = "import_job"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))  # cas_pdf | demo
    status: Mapped[str] = mapped_column(String(20))  # parsing | done | failed
    parser_version: Mapped[str | None] = mapped_column(String(40))
    statement_from: Mapped[date | None] = mapped_column(Date)
    statement_to: Mapped[date | None] = mapped_column(Date)
    summary: Mapped[dict | None] = mapped_column(JSON)
    exceptions: Mapped[list | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Folio(Base):
    __tablename__ = "folio"
    __table_args__ = (UniqueConstraint("investor_id", "amc_name", "folio_no"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    amc_name: Mapped[str] = mapped_column(String(120))
    folio_no: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20))

    investor: Mapped[Investor] = relationship(back_populates="folios")
    holdings: Mapped[list["Holding"]] = relationship(back_populates="folio", cascade="all, delete-orphan")
    txns: Mapped[list["Txn"]] = relationship(back_populates="folio", cascade="all, delete-orphan")


class Holding(Base):
    """Closing position per folio+scheme as of the statement date."""

    __tablename__ = "holding"
    __table_args__ = (UniqueConstraint("folio_id", "amfi_code"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    folio_id: Mapped[str] = mapped_column(ForeignKey("folio.id", ondelete="CASCADE"), index=True)
    amfi_code: Mapped[int] = mapped_column(ForeignKey("scheme.amfi_code"), index=True)
    units: Mapped[float] = mapped_column(Units())
    cost_value: Mapped[float | None] = mapped_column(Money())
    statement_value: Mapped[float | None] = mapped_column(Money())
    as_of: Mapped[date] = mapped_column(Date)

    folio: Mapped[Folio] = relationship(back_populates="holdings")
    scheme: Mapped[Scheme] = relationship()


class Txn(Base):
    __tablename__ = "txn"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    folio_id: Mapped[str] = mapped_column(ForeignKey("folio.id", ondelete="CASCADE"), index=True)
    amfi_code: Mapped[int] = mapped_column(ForeignKey("scheme.amfi_code"), index=True)
    import_job_id: Mapped[str | None] = mapped_column(ForeignKey("import_job.id", ondelete="SET NULL"))
    txn_type: Mapped[str] = mapped_column(String(32))
    txn_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float | None] = mapped_column(Money())
    units: Mapped[float | None] = mapped_column(Units())
    nav: Mapped[float | None] = mapped_column(Nav())
    balance: Mapped[float | None] = mapped_column(Units())
    description: Mapped[str | None] = mapped_column(String(255))

    folio: Mapped[Folio] = relationship(back_populates="txns")


# --------------------------------------------------------------------------- advice & chat


class RecoRun(Base):
    __tablename__ = "reco_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    investor_id: Mapped[str] = mapped_column(ForeignKey("investor.id", ondelete="CASCADE"), index=True)
    risk_profile_id: Mapped[str | None] = mapped_column(ForeignKey("risk_profile.id", ondelete="SET NULL"))
    trigger: Mapped[str] = mapped_column(String(20))  # user | chat | scheduled
    model_version: Mapped[str] = mapped_column(String(20))
    inputs: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    items: Mapped[list["RecoItem"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RecoItem(Base):
    __tablename__ = "reco_item"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("reco_run.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(24))
    amfi_code: Mapped[int | None] = mapped_column(Integer)
    from_amfi_code: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[float | None] = mapped_column(Money())
    priority: Mapped[int] = mapped_column(Integer, default=50)
    suitability_pass: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    card: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    run: Mapped[RecoRun] = relationship(back_populates="items")


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
    cards: Mapped[list | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(40))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    usage: Mapped[dict | None] = mapped_column(JSON)
    reco_run_id: Mapped[str | None] = mapped_column(ForeignKey("reco_run.id", ondelete="SET NULL"))
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
