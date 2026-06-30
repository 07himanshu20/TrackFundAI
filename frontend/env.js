/* ============================================================
   env.js  —  runtime config for the static frontend
   ------------------------------------------------------------
   This file is the bridge between .env.* files (which the
   browser cannot read) and the JS code that runs in the page.

   • In LOCAL dev: this file is loaded as-is. Defaults below
     point at http://127.0.0.1:8000 (Django runserver).
   • In Amplify builds: amplify.yaml REGENERATES this file
     from the env vars set in the Amplify Console for that
     branch, before publishing.

   Anything you put on window.APP_CONFIG is public — it ships
   to every browser that loads the site. NEVER put secrets here.
   ============================================================ */
(function () {
  window.APP_CONFIG = Object.freeze({
    API_BASE:   'http://127.0.0.1:8000/api',
    API_ORIGIN: 'http://127.0.0.1:8000',
    ENVIRONMENT: 'development',
    DEBUG:      true,
  });
})();
