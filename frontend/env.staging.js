/* ============================================================
   env.staging.js  —  STAGING runtime config
   ------------------------------------------------------------
   This file is COMMITTED to git as the canonical staging
   config. It is copied over the (git-ignored) active file
   env.js at deploy time — either manually:

       cp frontend/env.staging.js frontend/env.js
       aws s3 sync frontend/ s3://<staging-bucket>/ --delete

   Or via the safe deploy script (recommended — verifies the
   deployed env.js does NOT contain 'localhost' after sync):

       bash scripts/deploy-staging.sh

   Frontend origin:  https://staging.trackfundai.com
   Backend  origin:  https://staging-api.trackfundai.com
   ============================================================ */
(function () {
  window.APP_CONFIG = Object.freeze({
    API_BASE:    'https://staging-api.trackfundai.com/api',
    API_ORIGIN:  'https://staging-api.trackfundai.com',
    ENVIRONMENT: 'staging',
    DEBUG:       false,
  });
})();
