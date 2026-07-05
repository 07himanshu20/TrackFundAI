/* ============================================================
   env.local.js  —  LOCAL DEV runtime config
   ------------------------------------------------------------
   This file is COMMITTED to git as the canonical local-dev
   config. To use it, copy it over the (git-ignored) active file:

       cp frontend/env.local.js frontend/env.js

   (Or run:  bash scripts/use-env-local.sh)

   Then open the SPA — the browser loads env.js and points at
   your local Django (http://127.0.0.1:8000) which you should
   start with:  TFAI_ENV=local python manage.py runserver 8000

   NEVER edit env.js by hand and NEVER commit it. env.js is the
   per-machine "active" copy; env.local.js and env.staging.js
   are the two source-of-truth templates.
   ============================================================ */
(function () {
  window.APP_CONFIG = Object.freeze({
    API_BASE:    'http://127.0.0.1:8000/api',
    API_ORIGIN:  'http://127.0.0.1:8000',
    ENVIRONMENT: 'local',
    DEBUG:       true,
  });
})();
