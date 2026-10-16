#!/bin/sh
# Container entrypoint for RA's API.
# Temporary guard: refuse to serve until the build includes the email gate (REQUIRE_ACCESS
# support), so the API is never public without it.
if ! python -c "import sys; from app.config import Settings; sys.exit('require_access' not in Settings.model_fields)"; then
  echo "RA: this build has no email gate yet; refusing to start."
  exit 1
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
