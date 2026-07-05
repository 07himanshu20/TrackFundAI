# TrackFundAI — Frontend

Vanilla-JS SPA served as static files (S3+CloudFront in staging, `python -m http.server` or nginx locally).

## Runtime configuration — `env.js`

The browser loads **one** file at boot to know which backend to talk to:
`frontend/env.js`. That file sets `window.APP_CONFIG.{API_BASE, API_ORIGIN, ENVIRONMENT, DEBUG}` and every other JS module reads from it.

`env.js` is **per-machine** — it is **NOT committed to git**. Instead, two
committed source-of-truth templates exist:

| File | Committed | Purpose |
| --- | --- | --- |
| `env.local.js` | yes | Points at `http://127.0.0.1:8000` (local Django) |
| `env.staging.js` | yes | Points at `https://staging-api.trackfundai.com` (staging backend) |
| `env.js` | **no** (`.gitignore`) | The active per-machine copy the browser actually loads |

### How to switch environments

**Local development** (do once on a fresh checkout, or when switching back from staging):
```bash
bash scripts/use-env-local.sh
# or, equivalently:
cp frontend/env.local.js frontend/env.js
```
Then start Django:
```bash
cd backend && TFAI_ENV=local python manage.py runserver 8000
```

**Deploying to staging** — use the safe script. It copies `env.staging.js`
over `env.js`, syncs to S3, invalidates CloudFront, and curl-verifies the
deployed `env.js` does NOT contain `localhost`. Aborts non-zero on any
failure so the localhost URL can never leak to staging silently.
```bash
export STAGING_S3_BUCKET=<your-staging-bucket>
export STAGING_CF_DIST_ID=<your-cloudfront-distribution-id>
export STAGING_URL=https://staging.trackfundai.com/env.js
bash scripts/deploy-staging.sh
```

### Why this design

- **Explicit over implicit** — no runtime `window.location` sniffing.
  The file the browser loads is the file you chose to copy.
- **Two committed source files** are reviewable in every PR.
- **The active file is gitignored** — a stale local `env.js` can never
  reach a staging deploy through git.
- **The deploy script verifies the deployed URL** — if S3/CDN somehow
  served the wrong `env.js`, the deploy exits non-zero.

## Backend URL contract

- Local dev: `http://127.0.0.1:8000` (Django runserver)
- Staging: `https://staging-api.trackfundai.com`
- Frontend origin (staging): `https://staging.trackfundai.com`

If your real staging backend URL differs, edit **exactly one line** in
`env.staging.js` — the `API_BASE` value.
