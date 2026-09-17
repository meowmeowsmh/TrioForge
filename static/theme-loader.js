/* ── Theme loader ─────────────────────────────────────────────────────────────
   Included by every page (chat, notes, corkboard) right after themes.css. It reads
   the stored choice and sets  document.documentElement.dataset.theme  immediately,
   so the page paints in the right theme instead of flashing the default first.

   localStorage is shared across pages on this origin, which is what makes one choice
   apply everywhere. The app also mirrors it into its saved settings, so the choice
   survives a cleared browser profile — that value is applied as a fallback a moment
   later if localStorage is empty.
*/
(function () {
    var THEMES = ['midnight', 'premium', 'galaxy', 'odyssey', 'ember', 'forest', 'sakura', 'paper', 'custom'];
    // Which themes are LIGHT. The app has its own light-mode stylesheet (body.light-mode
    // with ~170 rules across the three pages), so a light theme has to switch that on as
    // well - otherwise half the page keeps dark colours and the result looks like a
    // patchwork, which is exactly how the light/dark inconsistency showed up.
    var LIGHT = ['paper'];
    var KEY = 'trio_theme';
    var LAST_DARK_KEY = 'trio_theme_last_dark';
    // The app's pre-existing key. Kept in sync so the top-bar toggle's knob, and the
    // notes and corkboard pages, all agree with whatever theme is chosen.
    var LEGACY_KEY = 'theme';
    var KEYS = { accent: 'trio_theme_accent', bg: 'trio_theme_bg', accent2: 'trio_theme_accent2' };

    function read(key) {
        try { return localStorage.getItem(key) || ''; } catch (e) { return ''; }
    }
    function write(key, value) {
        try { localStorage.setItem(key, value); } catch (e) {}
    }

    function isLight(name) { return LIGHT.indexOf(name) !== -1; }

    function applyCustom() {
        var root = document.documentElement;
        var accent = read(KEYS.accent) || '#7c5cff';
        var accent2 = read(KEYS.accent2) || accent;
        var bg = read(KEYS.bg) || '#0f0f16';
        root.style.setProperty('--tf-accent', accent);
        root.style.setProperty('--tf-accent-2', accent2);
        root.style.setProperty('--tf-accent-glow', accent + '80');
        root.style.setProperty('--tf-accent-soft', accent + '2e');
        root.style.setProperty('--tf-bg', bg);
        root.style.setProperty('--tf-bg-art',
            'radial-gradient(circle at 18% 14%, ' + accent + '33, transparent 45%),' +
            'radial-gradient(circle at 84% 84%, ' + accent2 + '26, transparent 48%),' +
            'linear-gradient(160deg, ' + bg + ', #07070b)');
    }

    // The single place light/dark is decided. Both the theme attribute (this file's
    // rules) and the app's own light-mode class follow from it, so the two systems can
    // no longer disagree.
    function applyScheme(name) {
        var light = isLight(name);
        var add = function (el) { if (el) { el.classList.toggle('light-mode', light); } };
        add(document.documentElement);
        add(document.body);
        write(LEGACY_KEY, light ? 'light' : 'dark');
    }

    function apply(name) {
        if (THEMES.indexOf(name) === -1) { name = 'midnight'; }
        if (name === 'midnight') {
            // Midnight IS the app's own design, so the theme layer steps aside for it:
            // no data-theme attribute means none of the override rules match and the
            // original stylesheets apply untouched. Choosing the default can therefore
            // never change how the app already looked.
            delete document.documentElement.dataset.theme;
        } else {
            document.documentElement.dataset.theme = name;
            if (name === 'custom') { applyCustom(); }
        }
        applyScheme(name);
    }

    // Exposed so the picker in the chat page (and any page) can switch instantly.
    window.tfSetTheme = function (name, custom) {
        if (custom) {
            if (custom.accent) { write(KEYS.accent, custom.accent); }
            if (custom.accent2) { write(KEYS.accent2, custom.accent2); }
            if (custom.bg) { write(KEYS.bg, custom.bg); }
        }
        if (!isLight(name)) { write(LAST_DARK_KEY, name); }
        write(KEY, name);
        apply(name);
    };
    window.tfCurrentTheme = function () {
        if (document.documentElement.classList.contains('light-mode')) {
            // The app's own toggle may have set light-mode before this file ran.
            return read(KEY) && isLight(read(KEY)) ? read(KEY) : 'paper';
        }
        return document.documentElement.dataset.theme || 'midnight';
    };
    window.tfIsLight = function () { return document.documentElement.classList.contains('light-mode'); };
    // Used by the app's existing moon/sun toggle, so it moves the theme instead of
    // setting a second, competing flag.
    window.tfSetLight = function (light) {
        if (light === window.tfIsLight()) { apply(read(KEY) || 'midnight'); return; }
        if (light) {
            var current = document.documentElement.dataset.theme;
            if (current && !isLight(current)) { write(LAST_DARK_KEY, current); }
            window.tfSetTheme('paper');
        } else {
            window.tfSetTheme(read(LAST_DARK_KEY) || 'midnight');
        }
    };
    window.tfCustomColors = function () {
        return { accent: read(KEYS.accent) || '#7c5cff',
                 accent2: read(KEYS.accent2) || read(KEYS.accent) || '#7c5cff',
                 bg: read(KEYS.bg) || '#0f0f16' };
    };

    var stored = read(KEY);
    if (!stored) {
        // The app injects its saved settings into the page (window.__ui_settings) before
        // this script runs, but only MIRRORS them into localStorage further down the body.
        // Reading localStorage alone therefore saw nothing, decided "dark", applied it -
        // and then the mirror wrote theme=light afterwards. The setting said light while
        // the page was dark, on every single load: that was the inconsistency. Asking the
        // injected settings directly closes the gap without waiting for the mirror.
        var injected = window.__ui_settings || {};
        stored = injected.trio_theme || injected.theme || '';
        if (stored === 'light') { stored = 'paper'; }        // the old light/dark key
        if (stored === 'dark') { stored = 'midnight'; }
    }
    if (!stored) { stored = read(LEGACY_KEY) === 'light' ? 'paper' : 'midnight'; }
    apply(stored);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { applyScheme(stored); });
    }
})();
