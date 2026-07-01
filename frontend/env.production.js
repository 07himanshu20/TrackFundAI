/* ============================================================
   env.production.js  —  runtime config for S3 + CloudFront
   ------------------------------------------------------------
   DEPLOYMENT INSTRUCTIONS
   ------------------------------------------------------------
   Before `aws s3 sync frontend/ s3://<bucket>/`, overwrite the
   default env.js with this file:

       cp frontend/env.production.js frontend/env.js
       aws s3 sync frontend/ s3://<bucket>/ --delete \
           --cache-control "no-cache, must-revalidate" \
           --exclude "*.js" --exclude "*.css" --exclude "assets/*"
       aws s3 sync frontend/ s3://<bucket>/ \
           --cache-control "public, max-age=31536000" \
           --exclude "*" --include "*.js" --include "*.css"
       aws s3 cp frontend/env.js s3://<bucket>/env.js \
           --cache-control "no-cache, must-revalidate"
       aws cloudfront create-invalidation \
           --distribution-id <ID> --paths "/*"

   ARCHITECTURE (Option A)
   ------------------------------------------------------------
   • CloudFront distribution has TWO origins:
       1. S3 bucket   → default behavior (/* path pattern)
       2. Backend ALB → path pattern /api/*
                       Origin: staging-api.trackfundai.com
                       (HTTPS-only, Origin Protocol: HTTPS Only)
   • Because /api/* is same-origin from the browser's view,
     API_BASE stays relative ('/api') — no CORS setup required.
   ============================================================ */
(function () {
  window.APP_CONFIG = Object.freeze({
    API_BASE:   '/api',
    API_ORIGIN: '',
    ENVIRONMENT: 'production',
    DEBUG:      false,
  });
})();
