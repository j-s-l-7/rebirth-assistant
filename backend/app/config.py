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
    # anthropic | openai. New chat sessions use this provider; existing sessions keep theirs.
    copilot_provider: str = "anthropic"
    openai_model: str = "gpt-5.5"
    openai_max_tokens: int = 2000
    copilot_refusal_fallbacks: bool = True

    # Compliance identity shown in UI and disclosures.
    distributor_name: str = "[DistCo] Pvt Ltd"
    arn: str = "ARN-PENDING"

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # /admin/* endpoints require this value in the X-Admin-Token header; unset, they are disabled.
    admin_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
