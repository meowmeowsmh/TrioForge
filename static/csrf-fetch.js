/* CSRF token for the standalone feature pages (Notes, Cork Board).

   The chat shell (templates/index.html) wraps window.fetch itself and sends the
   token on every state-changing request. That wrapper belongs to the SHELL's
   document - a page loaded in an iframe has its own window - so the Cork Board and
   Notes pages were sending nothing and every POST/PUT/DELETE from them came back
   "403 invalid or missing CSRF token". In practice, since the CSRF work landed,
   nothing on those two pages could be saved: new pins, pin edits, deletions,
   threads, AI assist, note saves, note deletes.

   Same contract as the shell: fetch /api/csrf once, cache it, and attach it to
   anything that is not GET/HEAD/OPTIONS. Served as a file so both pages (and any
   future one) share exactly one implementation. */
(function () {
    if (typeof window.fetch !== 'function' || window.__CSRF_WRAPPED__) { return; }
    window.__CSRF_WRAPPED__ = true;

    var orig = window.fetch;
    var pending = null;

    function csrf() {
        if (window.__CSRF__) { return Promise.resolve(window.__CSRF__); }
        if (!pending) {
            pending = orig.call(window, '/api/csrf', { credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.json() : {}; })
                .then(function (d) { window.__CSRF__ = d.token || ''; return window.__CSRF__; })
                .catch(function () { return ''; });
        }
        return pending;
    }

    window.fetch = function (input, init) {
        init = init || {};
        var method = String(
            init.method || (typeof input === 'object' && input && input.method) || 'GET'
        ).toUpperCase();
        if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') {
            return orig.call(window, input, init);
        }
        return csrf().then(function (tok) {
            var headers = new Headers(init.headers || {});
            if (tok) { headers.set('X-CSRF-Token', tok); }
            init.headers = headers;
            return orig.call(window, input, init);
        });
    };

    // A page that has already painted does not wait for a token: warm it now so the
    // first click is not the request that discovers it.
    csrf();
})();
