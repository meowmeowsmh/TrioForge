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
        // Record what was resolved, so the state can be read back instead of guessed
        // (`data-tf-theme` in the served DOM tells you which theme actually applied).
        document.documentElement.dataset.tfTheme = name;
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

    // Changing the theme must write BOTH stores. localStorage is read instantly by every
    // page on this origin; the server value is what a fresh profile reads and, since it now
    // takes priority, what the NEXT load reads. Writing only localStorage meant a switch to
    // dark lasted until you reloaded, which then read the server's 'paper' and put you back
    // in light - "is this light mode suddenly?". Only called from a user action, so it
    // cannot loop.
    function persist(name) {
        try {
            fetch('/api/ui_settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    trio_theme: name,
                    theme: isLight(name) ? 'light' : 'dark'
                })
            }).catch(function () { });
        } catch (e) { }
    }

    // Applies a theme. `remote` decides whether the SERVER copy is written, and only a
    // real user action (the picker, or a click on the moon/sun toggle) passes true. The
    // pages call tfSetLight() on start-up to sync the toggle, and that must never write:
    // doing so let merely LOADING a page overwrite the saved theme with whatever that page
    // happened to resolve - which is how the setting kept reverting to midnight.
    function setTheme(name, remote) {
        if (THEMES.indexOf(name) === -1) { name = 'midnight'; }
        if (!isLight(name)) { write(LAST_DARK_KEY, name); }
        write(KEY, name);
        if (remote) { persist(name); }
        apply(name);
    }

    // Exposed so the picker in the chat page (and any page) can switch instantly.
    window.tfSetTheme = function (name, custom) {
        if (custom) {
            if (custom.accent) { write(KEYS.accent, custom.accent); }
            if (custom.accent2) { write(KEYS.accent2, custom.accent2); }
            if (custom.bg) { write(KEYS.bg, custom.bg); }
        }
        setTheme(name, true);
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
        // Already in the requested state: re-assert the theme that is ACTUALLY in force,
        // read back from the DOM. This used to fall back to localStorage, which is empty
        // on a fresh profile - so a page that had correctly resolved 'paper' was flipped
        // back to 'midnight' by its own load-time sync. That is why notes and corkboard
        // came up dark while the chat was light.
        if (light === window.tfIsLight()) {
            apply(document.documentElement.dataset.tfTheme || read(KEY) || 'midnight');
            return;
        }
        if (light) {
            var current = document.documentElement.dataset.theme;
            if (current && !isLight(current)) { write(LAST_DARK_KEY, current); }
            setTheme('paper', false);
        } else {
            setTheme(read(LAST_DARK_KEY) || 'midnight', false);
        }
    };
    window.tfCustomColors = function () {
        return { accent: read(KEYS.accent) || '#7c5cff',
                 accent2: read(KEYS.accent2) || read(KEYS.accent) || '#7c5cff',
                 bg: read(KEYS.bg) || '#0f0f16' };
    };

    // The SERVER's setting wins over localStorage. They are two stores of the same
    // choice and they can disagree - localStorage is written the instant the picker is
    // used, while the server value is the one that survives a cleared profile. Trusting
    // localStorage first produced a genuinely MIXED state: the app's own light-mode class
    // came on from the old 'light' key while the theme itself resolved to Midnight, so
    // the page went white but every component on it stayed dark. Rendering the page
    // headlessly is what exposed that, and this ordering is what fixes it.
    var injected = window.__ui_settings || {};
    var stored = injected.trio_theme || '';
    if (!stored) { stored = read(KEY); }
    if (!stored) {
        var legacy = injected.theme || read(LEGACY_KEY);
        stored = legacy === 'light' ? 'paper' : (legacy === 'dark' ? 'midnight' : '');
    }
    if (stored === 'light') { stored = 'paper'; }
    if (stored === 'dark') { stored = 'midnight'; }
    if (!stored) { stored = 'midnight'; }
    // Deliberately does NOT write back to localStorage: the page mirrors localStorage to
    // the server as a partial snapshot, so a write here fed a value straight back into the
    // settings file. (That endpoint also used to overwrite rather than merge, so the write
    // erased the very setting it came from. Fixed there; this stays read-only regardless.)
    apply(stored);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { applyScheme(stored); });
    }

    // ENFORCE the theme's polarity on both <html> and <body>, after the page's own scripts
    // have run. The app has its own light-mode class, set from an old localStorage key; when
    // that class and the theme attribute disagree you get the mix that has been showing up
    // as a white top bar above a black toolbar and a dark board - two halves of one page
    // styled by different modes. Whatever those scripts decide, this re-asserts the single
    // truth once the page has settled.
    (function enforce() {
        var theme = document.documentElement.dataset.tfTheme || stored;
        var light = isLight(theme);
        var setIt = function () {
            var els = [document.documentElement, document.body];
            for (var i = 0; i < els.length; i++) {
                if (els[i]) { els[i].classList.toggle('light-mode', light); }
            }
        };
        setIt();
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', setIt);
        }
        window.addEventListener('load', setIt);
        setTimeout(setIt, 400);
        setTimeout(setIt, 1500);
    })();

    // TAKE OVER the moon/sun toggle. The app has its own handler that flips only its
    // light-mode class, leaving the theme attribute alone - which is why toggling changed
    // the top bar and nothing else, and why the page kept ending up half light and half
    // dark. Catching the click first and routing it through setTheme() means the theme,
    // the light-mode class, the knob and both stores always move together.
    function knob(light) {
        var outer = document.getElementById('themeToggleOuter');
        if (outer) { outer.classList.toggle('day', light); }
    }
    document.addEventListener('click', function (ev) {
        var t = ev.target;
        while (t && t !== document.body) {
            var hit = (t.id === 'themeToggleOuter') ||
                      (t.id === 'themeKnob') ||
                      (t.classList && (t.classList.contains('toggle-outer') ||
                                       t.classList.contains('theme-toggle-wrapper')));
            if (hit) {
                ev.preventDefault();
                ev.stopImmediatePropagation();       // the app's own handler must not run
                var goingLight = !window.tfIsLight();
                setTheme(goingLight ? 'paper' : (read(LAST_DARK_KEY) || 'midnight'), true);
                knob(goingLight);
                return;
            }
            t = t.parentNode;
        }
    }, true);
    knob(window.tfIsLight());

    // Add ?tfdebug=1 to any page and it records what the browser ACTUALLY computed for
    // the surfaces that matter, in <html data-tf-probe="...">. Reading the source only
    // tells you what should happen; this says what did. Costs nothing when the flag is
    // absent, and makes "it is still dark for me" answerable with evidence.
    if (String(location.search || '').indexOf('tfdebug') !== -1) {
        setTimeout(function () {
            var wanted = [
                ['body', 'body'], ['topbar', '.top-bar'], ['chat', '.chat-area'],
                ['sidebar', '.sidebar'], ['inputbar', '.input-bar'], ['msginput', '#msgInput'],
                ['status', '#statusBar'], ['bot', '.msg.bot'], ['audio', '.tf-audio'],
                ['weather', '.weather-row'], ['weathercard', '.weather-card'],
                ['toolbar', '.toolbar'], ['board', '.board'], ['main', '.main'],
                ['notespanel', '.notes-panel'], ['editor', '.note-editor'],
                ['boardwrap', '.board-wrap']
            ];
            var out = ['theme=' + (document.documentElement.dataset.tfTheme || 'none')];
            wanted.forEach(function (pair) {
                var el = document.querySelector(pair[1]);
                if (!el) { out.push(pair[0] + '=absent'); return; }
                var cs = getComputedStyle(el);
                out.push(pair[0] + '=' + cs.backgroundColor + ' text ' + cs.color);
            });
            // Every bot bubble on its own: one of them looking dark while the first one
            // measures light is the difference between "the theme is broken" and "this
            // one element has a class of its own".
            var bots = document.querySelectorAll('.msg.bot');
            for (var i = 0; i < bots.length && i < 6; i++) {
                var cs2 = getComputedStyle(bots[i]);
                out.push('bot[' + i + ']=' + cs2.backgroundColor +
                         ' cls=' + (bots[i].className || '') +
                         ' kids=' + bots[i].children.length);
            }
            // INVENTORY: walk the whole DOM and list every element whose background has the
            // WRONG polarity for the active theme - dark islands on a light theme, light
            // islands on a dark theme. Naming selectors one at a time is how these kept
            // surviving; this finds all of them at once, measured, with the selector path
            // and box size so real surfaces stand out from specks.
            try {
                var islands = [];
                var intentional = 0;
                // Which way should surfaces lean? Light themes want light backgrounds.
                var wantLight = isLight(document.documentElement.dataset.tfTheme || stored);
                var all = document.querySelectorAll('body *');
                for (var k = 0; k < all.length; k++) {
                    var el2 = all[k];
                    var bg2 = getComputedStyle(el2).backgroundColor || '';
                    var mm = /rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/.exec(bg2);
                    if (!mm) { continue; }
                    if ((mm[4] === undefined ? 1 : parseFloat(mm[4])) < 0.5) { continue; }
                    var lum = (0.299 * +mm[1] + 0.587 * +mm[2] + 0.114 * +mm[3]) / 255;
                    // Wrong way: light surface in a dark theme, or dark surface in a light one.
                    // Mid-tones (the accent, a coloured pin) are neither.
                    var wrong = wantLight ? (lum < 0.45) : (lum > 0.75);
                    if (!wrong) { continue; }
                    var rc = el2.getBoundingClientRect();
                    var area = rc.width * rc.height;
                    if (area < 12000) { continue; }
                    // Deliberate exceptions: media, a modal's dim backdrop, sticky notes
                    // and pins are supposed to be their own colour.
                    var tag2 = el2.tagName.toLowerCase();
                    if (tag2 === 'img' || tag2 === 'video' || tag2 === 'canvas' ||
                        el2.id === 'setupModal' || el2.id === 'themeModal' ||
                        el2.id === 'integrityModal' || el2.id === 'logsModal' ||
                        (el2.className && typeof el2.className === 'string' &&
                         /(^|\s)(pin|note-item|sticky|board)\b/.test(el2.className))) {
                        intentional++;
                        continue;
                    }
                    var path = tag2;
                    if (el2.id) { path += '#' + el2.id; }
                    if (el2.className && typeof el2.className === 'string') {
                        path += '.' + el2.className.trim().split(/\s+/).slice(0, 3).join('.');
                    }
                    islands.push({ s: path, bg: bg2, a: Math.round(area) });
                }
                islands.sort(function (x, y) { return y.a - x.a; });
                out.push('wrong_colour_islands=' + islands.length +
                         ' (theme=' + (wantLight ? 'light' : 'dark') +
                         ', ' + intentional + ' deliberate skipped)');
                islands.slice(0, 14).forEach(function (it) {
                    out.push('WRONG ' + it.s + ' bg=' + it.bg + ' area=' + it.a);
                });
            } catch (e) { out.push('wrong_colour_islands=error ' + e.message); }
            document.documentElement.dataset.tfProbe = out.join(' | ');
        }, 3500);
    }
})();
