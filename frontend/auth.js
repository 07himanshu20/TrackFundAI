/* ============================================================
   auth.js
   JWT token management + auth utilities for TrackFundAI.
   Stores tokens in localStorage. Provides:
     - Auth.login(username, password) -> {user, access, refresh}
     - Auth.logout()
     - Auth.getToken()  -> access token or null
     - Auth.getUser()   -> cached user object or null
     - Auth.isLoggedIn() -> boolean
     - Auth.apiGet(path) / Auth.apiPost(path, body)
     - Auth.requireAuth() -> redirects to login if not authenticated
============================================================ */

(() => {
  const API_BASE = (() => {
    const p = window.location.port;
    const same = (p === '8000' || p === '' || p === '80' || p === '443');
    return same ? '/api' : 'http://127.0.0.1:8000/api';
  })();

  const KEYS = {
    access: 'tfai_access',
    refresh: 'tfai_refresh',
    user: 'tfai_user',
  };

  function _save(access, refresh, user) {
    localStorage.setItem(KEYS.access, access);
    localStorage.setItem(KEYS.refresh, refresh);
    localStorage.setItem(KEYS.user, JSON.stringify(user));
  }

  function _clear() {
    localStorage.removeItem(KEYS.access);
    localStorage.removeItem(KEYS.refresh);
    localStorage.removeItem(KEYS.user);
  }

  function getToken() {
    return localStorage.getItem(KEYS.access) || null;
  }

  function getRefreshToken() {
    return localStorage.getItem(KEYS.refresh) || null;
  }

  function getUser() {
    try {
      return JSON.parse(localStorage.getItem(KEYS.user));
    } catch {
      return null;
    }
  }

  function isLoggedIn() {
    return !!getToken();
  }

  async function login(username, password) {
    const r = await fetch(`${API_BASE}/auth/login/`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password}),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || 'Login failed');
    }
    const data = await r.json();
    _save(data.access, data.refresh, data.user);
    return data;
  }

  function logout() {
    const refresh = getRefreshToken();
    if (refresh) {
      fetch(`${API_BASE}/auth/logout/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${getToken()}`,
        },
        body: JSON.stringify({refresh}),
      }).catch(() => {});
    }
    _clear();
    window.location.href = 'login.html';
  }

  async function _refreshToken() {
    const refresh = getRefreshToken();
    if (!refresh) return false;
    try {
      const r = await fetch(`${API_BASE}/auth/refresh/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({refresh}),
      });
      if (!r.ok) return false;
      const data = await r.json();
      localStorage.setItem(KEYS.access, data.access);
      if (data.refresh) localStorage.setItem(KEYS.refresh, data.refresh);
      return true;
    } catch {
      return false;
    }
  }

  async function _authFetch(url, options = {}) {
    options.headers = options.headers || {};
    options.headers['Authorization'] = `Bearer ${getToken()}`;

    let r = await fetch(url, options);

    // If 401, try refreshing
    if (r.status === 401) {
      const refreshed = await _refreshToken();
      if (refreshed) {
        options.headers['Authorization'] = `Bearer ${getToken()}`;
        r = await fetch(url, options);
      } else {
        _clear();
        window.location.href = 'login.html';
        throw new Error('Session expired');
      }
    }
    return r;
  }

  async function apiGet(path) {
    const r = await _authFetch(`${API_BASE}${path}`);
    if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
    return r.json();
  }

  async function apiPost(path, body) {
    const r = await _authFetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
    return r.json();
  }

  async function apiPut(path, body) {
    const r = await _authFetch(`${API_BASE}${path}`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
    return r.json();
  }

  async function apiDelete(path) {
    const r = await _authFetch(`${API_BASE}${path}`, {method: 'DELETE'});
    if (r.status !== 204 && !r.ok) throw new Error(`API ${path} → ${r.status}`);
    return true;
  }

  async function apiUpload(path, formData) {
    const r = await _authFetch(`${API_BASE}${path}`, {
      method: 'POST',
      body: formData,
      // No Content-Type header — browser sets multipart boundary automatically
    });
    if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
    return r.json();
  }

  function requireAuth() {
    if (!isLoggedIn()) {
      window.location.href = 'login.html';
      return false;
    }
    return true;
  }

  window.Auth = {
    login, logout, getToken, getUser, isLoggedIn, requireAuth,
    apiGet, apiPost, apiPut, apiDelete, apiUpload,
  };
})();
