#!/usr/bin/env bash
# =============================================================================
# use-env-local.sh — activate the LOCAL-DEV frontend env config.
# -----------------------------------------------------------------------------
# Copies frontend/env.local.js over frontend/env.js so the browser hits
# your local Django (http://127.0.0.1:8000) instead of staging.
#
# Run this once on a fresh checkout, or any time you switched to staging
# and want to switch back:
#   bash scripts/use-env-local.sh
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/frontend/env.local.js"
DST="$ROOT/frontend/env.js"

if [[ ! -f "$SRC" ]]; then
  echo "ERROR: $SRC not found." >&2
  echo "This script requires frontend/env.local.js to exist (checked into git)." >&2
  exit 1
fi

cp "$SRC" "$DST"
echo "✓ Activated LOCAL env — frontend/env.js now points at 127.0.0.1:8000"
echo ""
echo "Next: start Django with"
echo "  cd backend && TFAI_ENV=local python manage.py runserver 8000"
