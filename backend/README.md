# RA backend

FastAPI service: AMFI data pipeline, portfolio analytics, RA Score, recommendation engine and the Claude Fable copilot. See the [root README](../README.md) for setup.

```
app/
  api/          REST + SSE routes (investors, portfolio, chat, admin)
  analytics/    NAV access, XIRR, FIFO lots, SIPs, portfolio view, peer ranking, overlap, RA Score
  copilot/      system prompt, tools, guardrails, compliance post-processor, orchestrator
  data/         AMFI scheme master + NAV history loaders, bulk upserts (COPY on Postgres)
  ingest/       CAS import (casparser), demo portfolio
  insights/     tax summary, nudges, goal maths
  reco/         recommendation engine (diagnosis rules -> suitability-gated cards)
  reference/    categories, tax rules (effective-dated), risk questionnaire, templates, suitability
  services/     analyse(): one call that assembles everything above
sql/schema.sql  Postgres DDL (generated: python -m app.cli print-ddl)
tests/          offline tests on a synthetic market
```

```bash
uv sync && uv run pytest -q
uv run uvicorn app.main:app --reload --port 8000   # API docs at http://localhost:8000/docs
```
