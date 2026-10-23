# RA (Rebirth Assistant): notes for Claude

Chat assistant for Indian mutual funds, run by an AMFI-registered distributor. It is an assistant, never an investment adviser: facts and comparisons only, no buy/sell/switch calls.

## Working rules (token budget matters)
- Use the `ponytail` and `karpathy-guidelines` skills: the smallest complete change, no speculative features, surgical edits.
- Write commit messages with `caveman-commit`; use `caveman-explore` for broad searches.
- Run one subagent at a time, on the Opus model. Give it the exact files and the success check up front.
- Read only the lines you need (grep first). Don't re-read files you just edited.
- Never commit secrets: `.env` is git-ignored and the repo is public. Push feature branches, not `main` (Render deploys `main`).

## Map
- `backend/app/data/`: AMFI loaders. `amfi.py` (NAV), `scheme_details.py`, `ter.py` (expense ratio; the one TER reader is `latest_ter`), `linking.py` (names → AMFI codes), `disclosures.py` (holdings CSV).
- `backend/app/analytics/`: `performance.py` (rank-v0.2 per category), `fund_lens.py` (one fund vs its category, `resolve_funds`), `navbook.py`.
- `backend/app/copilot/`: `prompts.py`, `tools.py`, `guardrails.py`, `compliance.py`, `orchestrator.py` (Claude), `openai_orchestrator.py`.
- `backend/app/api/`: investors, chat (SSE: status/text/done/error), admin. `backend/app/http.py`: the shared HTTP client and retry.
- `backend/app/models.py`: `SchemeHolding` (table `scheme_portfolio`) holds look-through holdings under each scheme's Regular-Growth code.
- `frontend/`: Next.js 16 chat. Read `node_modules/next/dist/docs` before Next code (see `frontend/AGENTS.md`).

## Checks
- `cd backend && uv run pytest -q` (offline, synthetic market)
- `uvx ruff check --select F,E9 backend`
- `cd frontend && npm run typecheck && npx eslint .`
- Local data: `backend/data/ra.db` (SQLite with real AMFI data, git-ignored). Run the CLI with `uv run python -m app.cli <cmd>`.
