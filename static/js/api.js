/* api.js — thin fetch wrapper for SmartCanteen AI */

const API = (() => {
  function token() { return localStorage.getItem('access_token'); }

  function authHeaders() {
    return { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' };
  }

  async function request(method, path, body) {
    const opts = { method, headers: authHeaders() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (res.status === 401) { logout(); return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  }

  function logout() {
    localStorage.clear();
    window.location.href = '/static/login.html';
  }

  return {
    get:    (path)        => request('GET',   path),
    post:   (path, body)  => request('POST',  path, body),
    patch:  (path, body)  => request('PATCH', path, body),
    logout,
    role:   ()            => localStorage.getItem('role'),
    userId: ()            => localStorage.getItem('college_id'),

    /* Auth check — redirect if not logged in or wrong role */
    requireRole(...roles) {
      const r = localStorage.getItem('role');
      if (!token() || (roles.length && !roles.includes(r))) {
        window.location.href = '/static/login.html';
      }
    },

    /* POST /auth/token with form-encoded body */
    async login(username, password) {
      const body = new URLSearchParams({ username, password });
      const res  = await fetch('/auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Login failed');
      localStorage.setItem('access_token', data.access_token);
      localStorage.setItem('role', data.role);
      localStorage.setItem('college_id', data.college_id);
      return data;
    },
  };
})();
