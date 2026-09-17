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
    var THEMES = ['midnight', 'galaxy', 'odyssey', 'ember', 'forest', 'sakura', 'paper', 'custom'];
    var KEY = 'trio_theme';
    var KEYS = { accent: 'trio_theme_accent', bg: 'trio_theme_bg', accent2: 'trio_theme_accent2' };

    function read(key) {
        try { return localStorage.getItem(key) || ''; } catch (e) { return ''; }
    }

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

    function apply(name) {
        if (THEMES.indexOf(name) === -1) { name = 'midnight'; }
        if (name === 'midnight') {
            // Midnight IS the app's own design, so the theme layer steps aside for it:
            // no data-theme attribute means none of the override rules match and the
            // original stylesheets apply untouched. Choosing the default can therefore
            // never change how the app already looked.
            delete document.documentElement.dataset.theme;
            return;
        }
        document.documentElement.dataset.theme = name;
        if (name === 'custom') { applyCustom(); }
    }

    // Exposed so the picker in the chat page (and any page) can switch instantly.
    window.tfSetTheme = function (name, custom) {
        if (custom) {
            try {
                if (custom.accent) { localStorage.setItem(KEYS.accent, custom.accent); }
                if (custom.accent2) { localStorage.setItem(KEYS.accent2, custom.accent2); }
                if (custom.bg) { localStorage.setItem(KEYS.bg, custom.bg); }
            } catch (e) {}
        }
        try { localStorage.setItem(KEY, name); } catch (e) {}
        apply(name);
    };
    window.tfCurrentTheme = function () { return document.documentElement.dataset.theme || 'midnight'; };
    window.tfCustomColors = function () {
        return { accent: read(KEYS.accent) || '#7c5cff',
                 accent2: read(KEYS.accent2) || read(KEYS.accent) || '#7c5cff',
                 bg: read(KEYS.bg) || '#0f0f16' };
    };

    apply(read(KEY) || 'midnight');
})();
