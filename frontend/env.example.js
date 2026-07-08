/* ============================================================
   env.example.js  —  frontend runtime config template
   ------------------------------------------------------------
   COMMITTED to git as a blueprint. Real per-environment configs
   are gitignored:
       frontend/env.local.js         (localhost backend)
       frontend/env.development.js   (dev-api.trackfundai.com)
       frontend/env.production.js    (api.trackfundai.com)
       frontend/env.js               (per-machine ACTIVE copy)

   Deploy step (copy the right file into env.js):
       cp frontend/env.local.js       frontend/env.js   # for local
       cp frontend/env.development.js frontend/env.js   # for dev host
       cp frontend/env.production.js  frontend/env.js   # for prod host
   ============================================================ */
(function () {
  window.APP_CONFIG = Object.freeze({
    API_BASE:    'CHANGEME-https://your-backend-host/api',
    API_ORIGIN:  'CHANGEME-https://your-backend-host',
    ENVIRONMENT: 'CHANGEME-local|development|production',
    DEBUG:       false,
  });
})();
