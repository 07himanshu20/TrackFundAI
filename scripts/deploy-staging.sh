#!/usr/bin/env bash
# =============================================================================
# deploy-staging.sh — deploy the STAGING frontend to S3 with a safety net.
# -----------------------------------------------------------------------------
# What this does (all-or-nothing — aborts on ANY step failure):
#   1. Copy frontend/env.staging.js over frontend/env.js locally.
#   2. Verify the copy contains 'staging-api.trackfundai.com' and does NOT
#      contain '127.0.0.1' or 'localhost'.  (Guards against the operator
#      running this script while env.staging.js has been edited wrong.)
#   3. aws s3 sync frontend/ to the staging bucket, with tight cache on
#      env.js and long cache on hashed assets.
#   4. Invalidate CloudFront cache for /* so the new env.js goes live now.
#   5. curl https://staging.trackfundai.com/env.js and verify it does NOT
#      contain '127.0.0.1'. This is the last line of defense — if it does,
#      the script exits non-zero and prints the offending line.
#
# REQUIRED environment variables (set in your shell before running):
#   STAGING_S3_BUCKET     e.g. staging-trackfundai-frontend
#   STAGING_CF_DIST_ID    CloudFront distribution ID (E123XXXX...)
#   STAGING_URL           full URL of staging env.js
#                          e.g. https://staging.trackfundai.com/env.js
#
# USAGE
#   export STAGING_S3_BUCKET=my-bucket
#   export STAGING_CF_DIST_ID=E1XX...
#   export STAGING_URL=https://staging.trackfundai.com/env.js
#   bash scripts/deploy-staging.sh
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$ROOT/frontend"

# ---- 0. Pre-flight ----
: "${STAGING_S3_BUCKET:?STAGING_S3_BUCKET env var required (see comments)}"
: "${STAGING_CF_DIST_ID:?STAGING_CF_DIST_ID env var required (see comments)}"
: "${STAGING_URL:?STAGING_URL env var required (see comments)}"

command -v aws >/dev/null || { echo "ERROR: aws CLI not installed" >&2; exit 1; }
command -v curl >/dev/null || { echo "ERROR: curl not installed" >&2; exit 1; }

# ---- 1. Copy staging env over active env.js ----
if [[ ! -f "$FRONTEND/env.staging.js" ]]; then
  echo "ERROR: $FRONTEND/env.staging.js missing." >&2
  exit 1
fi
cp "$FRONTEND/env.staging.js" "$FRONTEND/env.js"
echo "✓ Copied env.staging.js → env.js"

# ---- 2. Verify env.js content pre-upload ----
# Match only actual localhost URLs (://.../ patterns) — not the word
# 'localhost' appearing inside doc comments explaining the check itself.
if grep -qE "://[^'\"[:space:]]*(127\.0\.0\.1|localhost)" "$FRONTEND/env.js"; then
  echo "ABORT: env.js contains a localhost URL after copy — env.staging.js is broken." >&2
  grep -nE "://[^'\"[:space:]]*(127\.0\.0\.1|localhost)" "$FRONTEND/env.js" >&2
  exit 2
fi
if ! grep -q "staging-api.trackfundai.com" "$FRONTEND/env.js"; then
  echo "ABORT: env.js missing 'staging-api.trackfundai.com' — did the source change?" >&2
  exit 2
fi
echo "✓ Local env.js content verified"

# ---- 3. Upload to S3 (tight cache on env.js, long cache on hashed assets) ----
# Upload everything except env.js with a modest cache.
aws s3 sync "$FRONTEND/" "s3://$STAGING_S3_BUCKET/" \
  --delete \
  --exclude "env.js" \
  --exclude "env.local.js" \
  --exclude "env.staging.js" \
  --exclude "*.md" \
  --exclude ".env*" \
  --exclude "Dockerfile" \
  --exclude "docker-compose.yml" \
  --exclude "nginx.conf" \
  --exclude ".dockerignore" \
  --cache-control "public, max-age=3600"

# Upload env.js separately with no-cache.
aws s3 cp "$FRONTEND/env.js" "s3://$STAGING_S3_BUCKET/env.js" \
  --cache-control "no-cache, no-store, must-revalidate" \
  --content-type "application/javascript"

echo "✓ Uploaded to s3://$STAGING_S3_BUCKET/"

# ---- 4. Invalidate CloudFront so env.js and index.html reload immediately ----
INVAL=$(aws cloudfront create-invalidation \
  --distribution-id "$STAGING_CF_DIST_ID" \
  --paths "/env.js" "/index.html" "/*.html" \
  --query 'Invalidation.Id' --output text)
echo "✓ CloudFront invalidation created: $INVAL"

# ---- 5. Post-deploy verification — most important check ----
# Wait a few seconds for the S3 write to propagate, then curl the CDN.
sleep 5
DEPLOYED=$(curl -fsSL --max-time 10 "$STAGING_URL")
# Match only actual URLs to avoid tripping on the doc comment.
if echo "$DEPLOYED" | grep -qE "://[^'\"[:space:]]*(127\.0\.0\.1|localhost)"; then
  echo "ABORT: Deployed env.js at $STAGING_URL contains a localhost URL!" >&2
  echo "Contents:" >&2
  echo "$DEPLOYED" >&2
  echo "" >&2
  echo "Possible causes: CDN still serving cached copy, or S3 upload silently failed." >&2
  echo "Wait for invalidation $INVAL to complete, then re-curl $STAGING_URL manually." >&2
  exit 3
fi
if ! echo "$DEPLOYED" | grep -q "staging-api.trackfundai.com"; then
  echo "WARNING: Deployed env.js does not contain staging-api.trackfundai.com" >&2
  echo "Contents:" >&2
  echo "$DEPLOYED" >&2
  exit 3
fi
echo "✓ Deployed env.js verified at $STAGING_URL"
echo ""
echo "STAGING DEPLOY COMPLETE."
