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
    // Dark only. The light mode was removed by request: it never looked right next to
    // the app's own palette, and the moon/sun toggle that drove it is gone from the UI.
    // 'paper' is deliberately absent from this list, so a profile that had it selected
    // falls through to midnight instead of loading a theme that no longer exists.
    var THEMES = ['midnight', 'galaxy', 'odyssey', 'ember', 'forest', 'sakura', 'custom'];
    var LIGHT = [];
    var KEY = 'trio_theme';
    var LAST_DARK_KEY = 'trio_theme_last_dark';
    // The app's pre-existing key. Kept in sync (always 'dark' now) so the notes and
    // corkboard pages, which still read it, agree with the chosen theme.
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

    // Light mode is gone, so this now only ever asserts dark. It also clears a
    // light-mode class left on the page by an older session, which is what would
    // otherwise leave half the UI in the light palette.
    function applyScheme() {
        var off = function (el) { if (el) { el.classList.remove('light-mode'); } };
        off(document.documentElement);
        off(document.body);
        write(LEGACY_KEY, 'dark');
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
        applyScheme();
    }

    // Exposed so the picker in the chat page (and any page) can switch instantly.
    window.tfSetTheme = function (name, custom) {
        if (custom) {
            if (custom.accent) { write(KEYS.accent, custom.accent); }
            if (custom.accent2) { write(KEYS.accent2, custom.accent2); }
            if (custom.bg) { write(KEYS.bg, custom.bg); }
        }
        if (THEMES.indexOf(name) === -1) { name = 'midnight'; }
        if (!isLight(name)) { write(LAST_DARK_KEY, name); }
        write(KEY, name);
        apply(name);
    };
    window.tfCurrentTheme = function () {
        var stored = read(KEY);
        if (stored && THEMES.indexOf(stored) !== -1) { return stored; }
        return document.documentElement.dataset.theme || 'midnight';
    };
    window.tfIsLight = function () { return false; };
    // The app's moon/sun toggle still calls this if it is somehow still on the page:
    // it simply re-asserts the dark theme rather than switching to a light one.
    window.tfSetLight = function () {
        var stored = read(KEY);
        apply(stored && THEMES.indexOf(stored) !== -1 ? stored : 'midnight');
    };
    window.tfCustomColors = function () {
        return { accent: read(KEYS.accent) || '#7c5cff',
                 accent2: read(KEYS.accent2) || read(KEYS.accent) || '#7c5cff',
                 bg: read(KEYS.bg) || '#0f0f16' };
    };

    var stored = read(KEY);
    if (stored === 'paper') { stored = 'midnight'; }   // the removed light theme
    apply(stored || 'midnight');
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { applyScheme(); });
    }
})();
