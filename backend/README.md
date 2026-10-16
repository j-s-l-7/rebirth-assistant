# RA backend

FastAPI service behind the RA chat: AMFI data pipeline, peer ranking, the fund lens and the copilot. See the [root README](../README.md) for setup and what's in the database.

```
app/
  api/          REST + SSE routes (investors, chat, admin)
  analytics/    NAV access, peer ranking (rank-v0.2), fund lens
  copilot/      system prompt, tools, guardrails, compliance post-processor, orchestrators (OpenAI, Claude)
  data/         AMFI loaders: scheme master, NAV history, scheme details, TER; universe, name linking, disclosures
  docs/         official documents: archive, text extraction, chunking, full-text search
  reference/    SEBI categories, benchmark proxies, name normalisation
sql/schema.sql  Postgres DDL (generated: python -m app.cli print-ddl)
tests/          offline tests on a synthetic market
```

```bash
uv sync && uv run pytest -q
uv run uvicorn app.main:app --reload --port 8000   # API docs at http://localhost:8000/docs
```
