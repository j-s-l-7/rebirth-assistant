from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Export .env into os.environ too: the Anthropic SDK reads ANTHROPIC_API_KEY from there.
load_dotenv(BACKEND_DIR / ".env", override=False)
load_dotenv(BACKEND_DIR.parent / ".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR / ".env", BACKEND_DIR.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage: Supabase Postgres (DATABASE_URL) in staging/prod; SQLite file for local dev.
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'ra.db'}"
    db_schema: str = "mf"

    # Public market-data sources (AMFI is authoritative; mfapi.in is a per-scheme fallback)
    amfi_nav_url: str = "https://www.amfiindia.com/spages/NAVAll.txt"
    amfi_history_url: str = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
    mfapi_base_url: str = "https://api.mfapi.in/mf"
    http_timeout_s: float = 120.0
    nav_history_years: int = 6

    # LLM (PRD §10.2 "Fable advisory"): Fable reasons, Haiku classifies.
    copilot_model: str = "claude-fable-5-1"
    copilot_effort: str = "medium"
    guardrail_model: str = "claude-haiku-5-5"
    copilot_max_tool_rounds: int = 8
    copilot_refusal_fallbacks: bool = True

    # Security: field-level encryption for PAN (Fernet key) and an HMAC pepper for lookups.
    # Dev defaults are generated per machine if unset; never use them in production.
    field_encryption_key: str | None = None
    pan_hash_pepper: str = "dev-only-pepper-change-me"

    # Compliance identity shown in UI and disclosures.
    distributor_name: str = "[DistCo] Pvt Ltd"
    arn: str = "ARN-PENDING"
    euin: str = "E000000"

    # Default tax slab used only to *estimate* slab-taxed gains (labelled as an estimate).
    default_tax_slab_pct: float = 30.0

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # When set, /admin/* endpoints require the X-Admin-Token header.
    admin_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
