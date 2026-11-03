/* ── TrioForge motion backdrop ────────────────────────────────────────────────
   Adapted from the GG Coder framework's home + goal scenes. One dependency-free
   file: plain <canvas> + requestAnimationFrame, no libraries, no build step.

   Three backdrops live here, switched with the 🎨 button in the composer and
   persisted to localStorage ("trio_backdrop"):

     floor     - the perspective line-grid floor (drags to turn the camera).
     campfire  - the home screen's pixel clearing: fire, embers, smoke,
                 fireflies, stars and clouds, lit by the local clock.
     farm      - a top-down pixel yard: critters (and Ken) walk the paths, and
                 clicking pokes whatever is under the pointer (water ripples,
                 animals hop, lights flare).

   Two buttons drive it: 🎨 cycles the backdrop, ⛶ opens the SAME scene
   full-screen where it is interactive (drag to look around / click to poke).
   The ✨ button still fires the one-shot confetti burst.

   Behaviour rules (same as the original framework):
     * The backdrop behind the chat is decoration: pointer-events are off, so it
       can never eat a click or a keystroke.
     * prefers-reduced-motion freezes the AMBIENT motion (fire, walking, stars,
       drift) but keeps user-driven reactions, so pokes still answer you.
     * Nothing here reads or writes app state. It is pure decoration.
   ──────────────────────────────────────────────────────────────────────────── */
(function () {
    'use strict';

    /* ── small helpers ──────────────────────────────────────────────────── */
    var reduceMotion = false;
    try {
        reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) { /* old engine: treat motion as allowed */ }

    function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
    function lerp(a, b, t) { return a + (b - a) * t; }
    function rgb(r, g, b) { return 'rgb(' + (r | 0) + ',' + (g | 0) + ',' + (b | 0) + ')'; }
    function rgba(r, g, b, a) {
        return 'rgba(' + (r | 0) + ',' + (g | 0) + ',' + (b | 0) + ',' + a + ')';
    }
    /* Seeded PRNG so the stars, clouds and meadow never "boil" between frames. */
    function makePRNG(seed) {
        var s = seed >>> 0 || 1;
        return function () {
            s = (s * 1664525 + 1013904223) >>> 0;
            return s / 4294967296;
        };
    }
    /* Canvas can't read CSS custom properties, so resolve them via computed style. */
    function themeColor(name, fallback) {
        try {
            var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
            return v || fallback;
        } catch (e) { return fallback; }
    }
    function hourNow() {
        var d = new Date();
        return d.getHours() + d.getMinutes() / 60;
    }

    /* ── canvases ───────────────────────────────────────────────────────── */
    var bg = document.getElementById('tfDotGrid');
    var bgCtx = bg && bg.getContext('2d');
    var overlay = document.getElementById('tfSceneOverlay');
    var scene = document.getElementById('tfScene');
    var sceneCtx = scene && scene.getContext('2d');
    var confetti = document.getElementById('tfConfetti');

    /* ── persistent, shared simulation state ─────────────────────────────── */
    var sim = {
        scene: null,          // 'floor' | 'campfire' | 'farm'
        phase: 0,             // floor: camera travel (lattice cells)
        vpX: 0, vpY: 0,       // floor: vanishing-point offset from dragging
        yaw: 0,               // floor: star rotation from dragging
        farm: null,           // lazy-built farm world (path, critters, layout)
        reactions: [],        // ripples / hops / flares / pulses (all scenes)
        stars: null,          // campfire: seeded stars
        clouds: null,         // campfire: seeded clouds
        accent: themeColor('--tf-accent', '#58a6ff'),
        accentTick: 0
    };

    function loadScene() {
        try {
            var v = localStorage.getItem('trio_backdrop');
            return (v === 'campfire' || v === 'farm') ? v : 'floor';
        } catch (e) { return 'floor'; }
    }
    sim.scene = loadScene();

    /* ── canvas sizing (returns CSS-pixel size, sets the backing store) ──── */
    function fit(cv, ctx) {
        var dpr = Math.min(window.devicePixelRatio || 1, 2);
        var r = cv.getBoundingClientRect();
        var w = Math.max(1, Math.round(r.width));
        var h = Math.max(1, Math.round(r.height));
        cv.width = Math.round(w * dpr);
        cv.height = Math.round(h * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        return { w: w, h: h };
    }

    /* ══════════════════════════════════════════════════════════════════════
       1. FLOOR — the perspective line grid, with a turnable camera.
       ══════════════════════════════════════════════════════════════════════ */
    var FOCAL = 380, CAM_H = 38, K = FOCAL * CAM_H;
    var CELL_X = 9, RATIO = 1.12, ROWS = 26, SEG = 9, HORIZON_FRAC = 0.45;
    var floorStars = null;

    function floorStarField() {
        if (floorStars) return floorStars;
        var rnd = makePRNG(20240917), out = [];
        for (var i = 0; i < 170; i++) {
            out.push({ a: rnd() * Math.PI * 2, r: 0.3 + rnd() * 0.75, o: rnd(), s: 0.4 + rnd() * 1.2, tw: rnd() * 6.28 });
        }
        floorStars = out;
        return out;
    }

    function drawFloor(ctx, w, h, t, frozen) {
        var horizon = h * HORIZON_FRAC + sim.vpY;
        var cx = w * 0.5 + sim.vpX;
        var depth = Math.max(1, h - horizon);
        var zNear = K / depth;
        var phase = sim.phase % 1;

        ctx.clearRect(0, 0, w, h);
        ctx.lineCap = 'round';
        ctx.strokeStyle = sim.accent;
        ctx.fillStyle = sim.accent;

        /* stars (above the floor) rotate with the camera yaw */
        ctx.fillStyle = '#ffffff';
        var stars = floorStarField(), cxw = w / 2, cyh = horizon * 0.5;
        for (var si = 0; si < stars.length; si++) {
            var st = stars[si];
            var ang = st.a + sim.yaw;
            var rad = st.r * Math.min(w, horizon) * 0.5;
            var x = cxw + Math.cos(ang) * rad;
            var y = cyh + Math.sin(ang) * rad * 0.8;
            ctx.globalAlpha = st.s * (0.65 + 0.35 * Math.sin(t * 0.0011 + st.tw));
            ctx.fillRect(x, y, 1.2, 1.2);
        }
        ctx.globalAlpha = 1;

        /* converging lines (fixed world x) */
        var jMax = Math.ceil((w * 0.5 * zNear) / (FOCAL * CELL_X)) + 1;
        for (var j = -jMax; j <= jMax; j++) {
            var xb = cx + (j * CELL_X) * FOCAL * depth / K;
            for (var s = 0; s < SEG; s++) {
                var f0 = s / SEG, f1 = (s + 1) / SEG;
                ctx.globalAlpha = 0.06 + 0.30 * ((f0 + f1) * 0.5);
                ctx.beginPath();
                ctx.moveTo(cx + (xb - cx) * f0, horizon + depth * f0);
                ctx.lineTo(cx + (xb - cx) * f1, horizon + depth * f1);
                ctx.stroke();
            }
        }

        /* rows + glowing nodes */
        for (var k = 0; k < ROWS; k++) {
            var z = zNear * Math.pow(RATIO, k - phase);
            var y = horizon + K / z;
            if (y > h + 6) continue;
            var d = clamp((y - horizon) / depth, 0, 1);
            var alpha = 0.05 + 0.26 * d;
            ctx.globalAlpha = alpha;
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(w, y);
            ctx.stroke();
            if (d < 0.14) continue;
            var r = 0.8 + 1.5 * d;
            for (var jj = -jMax; jj <= jMax; jj++) {
                var nx = cx + (jj * CELL_X) * FOCAL * (y - horizon) / K;
                if (nx < -6 || nx > w + 6) continue;
                ctx.globalAlpha = 0.10 + 0.30 * d;
                ctx.beginPath(); ctx.arc(nx, y, r * 2.6, 0, 6.2832); ctx.fill();
                ctx.globalAlpha = 0.35 + 0.55 * d;
                ctx.beginPath(); ctx.arc(nx, y, r, 0, 6.2832); ctx.fill();
            }
        }
        ctx.globalAlpha = 0.16;
        ctx.fillRect(0, horizon - 1, w, 2);
        ctx.globalAlpha = 1;
    }

    /* ══════════════════════════════════════════════════════════════════════
       2. CAMPFIRE — the home screen's pixel clearing, lit by the clock.
       ══════════════════════════════════════════════════════════════════════ */
    function skyPalette(hour) {
        // hour 0..24 -> a simple day/dawn/dusk/night ramp.
        var day = clamp(1 - Math.abs(hour - 13) / 6, 0, 1);          // 1 at midday
        var warm = clamp(1 - Math.abs(hour - 8) / 4, 0, 1) * clamp(1 - Math.abs(hour - 19) / 4, 0, 1);
        var night = clamp(1 - day * 1.4, 0, 1);
        var top = { r: lerp(6, 32, day), g: lerp(8, 88, day), b: lerp(24, 160, day) };
        var bot = { r: lerp(20, 180, day) + 60 * warm, g: lerp(26, 200, day) + 40 * warm, b: lerp(44, 220, day) };
        return { top: top, bot: bot, night: night, warm: warm };
    }

    function campfireStars() {
        if (sim.stars) return sim.stars;
        var rnd = makePRNG(777), out = [];
        for (var i = 0; i < 90; i++) out.push({ x: rnd(), y: rnd() * 0.55, s: 0.5 + rnd(), tw: rnd() * 6.28 });
        sim.stars = out;
        return out;
    }
    function campfireClouds() {
        if (sim.clouds) return sim.clouds;
        var rnd = makePRNG(12345), out = [];
        for (var i = 0; i < 5; i++) out.push({ x: rnd(), y: 0.1 + rnd() * 0.35, s: 30 + rnd() * 60, v: 2 + rnd() * 5 });
        sim.clouds = out;
        return out;
    }

    function drawCampfire(ctx, w, h, t, frozen) {
        var hour = hourNow();
        var pal = skyPalette(hour);
        var horizon = h * 0.62;

        /* sky */
        var grad = ctx.createLinearGradient(0, 0, 0, horizon);
        grad.addColorStop(0, rgb(pal.top.r, pal.top.g, pal.top.b));
        grad.addColorStop(1, rgb(pal.bot.r, pal.bot.g, pal.bot.b));
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, w, horizon + 1);

        /* sun / moon */
        var isNight = pal.night > 0.5;
        if (!isNight) {
            var sunX = w * (0.2 + 0.6 * (hour - 6) / 12);
            ctx.fillStyle = 'rgba(255,235,160,0.9)';
            ctx.beginPath(); ctx.arc(sunX, horizon - h * 0.22, h * 0.045, 0, 6.2832); ctx.fill();
        } else {
            ctx.fillStyle = 'rgba(230,235,255,0.9)';
            ctx.beginPath(); ctx.arc(w * 0.72, horizon - h * 0.2, h * 0.038, 0, 6.2832); ctx.fill();
            var stars = campfireStars();
            ctx.fillStyle = '#fff';
            for (var i = 0; i < stars.length; i++) {
                var s = stars[i];
                ctx.globalAlpha = 0.4 + 0.6 * Math.max(0, Math.sin(t * 0.0015 + s.tw));
                ctx.fillRect(s.x * w, s.y * horizon, s.s, s.s);
            }
            ctx.globalAlpha = 1;
        }

        /* clouds (drift slowly) */
        var clouds = campfireClouds();
        for (var ci = 0; ci < clouds.length; ci++) {
            var c = clouds[ci];
            var cx2 = ((c.x * w + c.v * t) % (w + 160)) - 80;
            ctx.fillStyle = rgba(255, 255, 255, 0.10 + 0.10 * pal.night);
            ctx.beginPath();
            ctx.ellipse(cx2, c.y * horizon, c.s, c.s * 0.32, 0, 0, 6.2832);
            ctx.fill();
        }

        /* pine ridge */
        var pine = isNight ? rgb(12, 18, 34) : rgb(24, 44, 40);
        var pineFar = isNight ? rgb(16, 22, 40) : rgb(34, 60, 48);
        ctx.fillStyle = pineFar;
        ctx.fillRect(0, horizon - h * 0.03, w, h * 0.05);
        ctx.fillStyle = pine;
        var tree = 0;
        for (var x = -20; x < w + 20; x += 26) {
            var th = h * (0.06 + 0.05 * Math.abs(Math.sin(tree++ * 1.7)));
            ctx.beginPath();
            ctx.moveTo(x, horizon);
            ctx.lineTo(x + 13, horizon - th);
            ctx.lineTo(x + 26, horizon);
            ctx.closePath();
            ctx.fill();
        }

        /* meadow */
        ctx.fillStyle = isNight ? rgb(22, 34, 26) : rgb(46, 82, 44);
        ctx.fillRect(0, horizon, w, h - horizon);

        /* campfire (bottom centre) */
        var fx = w / 2, fy = h * 0.86;
        ctx.fillStyle = '#4a3420';
        ctx.fillRect(fx - 16, fy - 3, 32, 8);
        ctx.fillRect(fx - 12, fy - 6, 24, 5);
        var flame = frozen ? 1 : Math.sin(t * 0.006) * 0.5 + 0.5;
        ctx.fillStyle = 'rgba(240,120,40,0.9)';
        ctx.beginPath();
        ctx.moveTo(fx - 10, fy - 3);
        ctx.lineTo(fx - 6 + flame * 3, fy - 22 - flame * 14);
        ctx.lineTo(fx + 2, fy - 3);
        ctx.closePath(); ctx.fill();
        ctx.fillStyle = 'rgba(255,220,90,0.95)';
        ctx.beginPath();
        ctx.moveTo(fx - 5, fy - 3);
        ctx.lineTo(fx + flame * 2, fy - 14 - flame * 9);
        ctx.lineTo(fx + 5, fy - 3);
        ctx.closePath(); ctx.fill();

        /* embers */
        var e = Math.floor(t * 4) % 8;
        for (var e2 = 0; e2 < 8; e2++) {
            var ea = ((e + e2 * 3) % 14) / 14;
            ctx.fillStyle = rgba(255, 170, 70, 1 - ea);
            ctx.fillRect(fx - 12 + ((e2 * 37) % 24), fy - 8 - ea * 40, 2, 2);
        }

        /* fireflies at night */
        if (isNight) {
            for (var f2 = 0; f2 < 14; f2++) {
                var fx2 = (f2 * 0.618) % 1;
                var fy2 = 0.7 + 0.3 * Math.sin(t * 0.0007 + f2);
                ctx.fillStyle = 'rgba(210,255,150,0.8)';
                ctx.fillRect(w * fx2, h * fy2 + Math.sin(t * 0.001 + f2) * 8, 2, 2);
            }
        }
    }

    /* ══════════════════════════════════════════════════════════════════════
       3. FARM — a top-down pixel yard with critters on the paths.
       ══════════════════════════════════════════════════════════════════════ */
    var MAP_COLS = 26, MAP_ROWS = 17;

    /* Stations (tile coords) and the closed path critters follow. */
    var STATIONS = [
        { id: 'bookcase', name: 'bookcase', x: 8, y: 3.2, w: 1.6, h: 1.2, c: '#a1653a' },
        { id: 'laptop', name: 'laptop', x: 19.5, y: 4, w: 1.6, h: 1.2, c: '#3b82f6' },
        { id: 'notes', name: 'notes board', x: 22.5, y: 9, w: 1.2, h: 1.6, c: '#e5e7eb' },
        { id: 'bench', name: 'carpenter bench', x: 18.5, y: 12.6, w: 1.6, h: 1.2, c: '#c99a5b' },
        { id: 'stove', name: 'stove', x: 12, y: 13.4, w: 1.4, h: 1.2, c: '#c2483a' },
        { id: 'post', name: 'post box', x: 5.6, y: 10.4, w: 1.1, h: 1.1, c: '#b0564a' },
        { id: 'tea', name: 'tea bench', x: 9.6, y: 8, w: 1.4, h: 1.1, c: '#c99a5b' }
    ];
    var PATH = [
        { x: 6, y: 2.6 }, { x: 13, y: 2.6 }, { x: 19.5, y: 2.6 }, { x: 22.5, y: 5 },
        { x: 22.5, y: 9 }, { x: 20, y: 12.6 }, { x: 13, y: 13.4 }, { x: 8, y: 13.4 },
        { x: 5.6, y: 10.4 }, { x: 5.6, y: 5.5 }
    ];
    var CRITTERS = [
        { kind: 'cat', color: '#8a6f4d', speed: 0.045, pos: 0.0 },
        { kind: 'shiba', color: '#d9a05b', speed: 0.032, pos: 0.25 },
        { kind: 'hen', color: '#e8e0d0', speed: 0.05, pos: 0.5 },
        { kind: 'critter', color: '#6f9e6a', speed: 0.038, pos: 0.75 },
        { kind: 'ken', color: '#4a6fb0', speed: 0.03, pos: 0.12 }
    ];
    var POND = { x: 4, y: 7.2, rx: 2.0, ry: 1.5 };
    var FIRE = { x: 14, y: 8.6 };

    function buildFarm() {
        var rnd = makePRNG(999);
        /* Path geometry: cumulative lengths so pos (0..1) walks the loop. */
        var segs = [], total = 0;
        for (var i = 0; i < PATH.length; i++) {
            var a = PATH[i], b = PATH[(i + 1) % PATH.length];
            var len = Math.hypot(b.x - a.x, b.y - a.y);
            segs.push({ a: a, b: b, len: len, start: total });
            total += len;
        }
        function pathPoint(f) {
            var d = f * total;
            for (var j = 0; j < segs.length; j++) {
                var s = segs[j];
                if (d <= s.start + s.len || j === segs.length - 1) {
                    var t = s.len ? clamp((d - s.start) / s.len, 0, 1) : 0;
                    return { x: lerp(s.a.x, s.b.x, t), y: lerp(s.a.y, s.b.y, t) };
                }
            }
            return { x: 0, y: 0 };
        }
        var critters = CRITTERS.map(function (c) {
            return {
                kind: c.kind, color: c.color, speed: c.speed,
                pos: c.pos, hop: 0, pause: 0, puff: 0
            };
        });
        return {
            segs: segs, total: total, pathPoint: pathPoint,
            critters: critters,
            shade: (function () { var g = []; for (var i = 0; i < MAP_COLS * MAP_ROWS; i++) g.push(rnd() * 0.08); return g; })(),
            grass: rnd()
        };
    }
    if (!sim.farm) sim.farm = buildFarm();

    function farmScale(w, h) {
        return Math.min(w / MAP_COLS, h / MAP_ROWS);
    }

    function drawFarm(ctx, w, h, t, dt, frozen, pokeX, pokeY) {
        var s = farmScale(w, h);
        var ox = (w - MAP_COLS * s) / 2;
        var oy = (h - MAP_ROWS * s) / 2;
        var hour = hourNow();
        var night = clamp(1 - Math.abs(hour - 13) / 6, 0, 1) < 0.25;

        ctx.clearRect(0, 0, w, h);
        ctx.imageSmoothingEnabled = false;

        /* grass base */
        ctx.fillStyle = night ? '#1d2a1d' : '#4c8a3f';
        ctx.fillRect(0, 0, w, h);
        ctx.fillStyle = night ? '#263826' : '#57984a';
        for (var gy = 0; gy < MAP_ROWS; gy++) {
            for (var gx = 0; gx < MAP_COLS; gx++) {
                var sh = sim.farm.shade[gy * MAP_COLS + gx];
                if (sh > 0.05) {
                    ctx.fillStyle = night ? rgba(46, 64, 46, sh) : rgba(255, 255, 255, sh);
                    ctx.fillRect(ox + gx * s, oy + gy * s, s + 1, s + 1);
                }
            }
        }

        /* dirt path */
        ctx.strokeStyle = night ? '#4a3b2c' : '#b08a5a';
        ctx.lineWidth = s * 1.05;
        ctx.lineJoin = 'round';
        ctx.beginPath();
        var pp0 = sim.farm.pathPoint(0);
        ctx.moveTo(ox + pp0.x * s, oy + pp0.y * s);
        for (var i = 1; i <= 40; i++) {
            var pp = sim.farm.pathPoint(i / 40);
            ctx.lineTo(ox + pp.x * s, oy + pp.y * s);
        }
        ctx.stroke();

        /* pond */
        ctx.fillStyle = night ? '#16324a' : '#3f8fd0';
        ctx.beginPath();
        ctx.ellipse(ox + POND.x * s, oy + POND.y * s, POND.rx * s, POND.ry * s, 0, 0, 6.2832);
        ctx.fill();
        ctx.fillStyle = 'rgba(255,255,255,0.18)';
        ctx.beginPath();
        ctx.ellipse(ox + (POND.x - 0.5) * s, oy + (POND.y - 0.4) * s, POND.rx * 0.5 * s, POND.ry * 0.4 * s, 0, 0, 6.2832);
        ctx.fill();

        /* campfire tile */
        drawFarmFire(ctx, ox + FIRE.x * s, oy + FIRE.y * s, s, t, frozen);

        /* stations */
        for (var st = 0; st < STATIONS.length; st++) {
            var q = STATIONS[st];
            var sx = ox + q.x * s, sy = oy + q.y * s;
            ctx.fillStyle = q.c;
            ctx.fillRect(sx - q.w * s / 2, sy - q.h * s / 2, q.w * s, q.h * s);
            ctx.fillStyle = 'rgba(0,0,0,0.15)';
            ctx.fillRect(sx - q.w * s / 2, sy - q.h * s / 2, q.w * s, 0.2 * s);
        }

        /* reactions drawn under critters (ripples live on the ground) */
        drawReactions(ctx, t, s, ox, oy, true);

        /* critters */
        if (!frozen) {
            for (var c = 0; c < sim.farm.critters.length; c++) {
                var cr = sim.farm.critters[c];
                if (cr.pause > t) continue;
                cr.pos = (cr.pos + cr.speed * dt) % 1;
                if (Math.random() < 0.002) cr.pause = t + 0.5 + Math.random() * 1.5;
            }
        }
        for (var cc = 0; cc < sim.farm.critters.length; cc++) {
            var k = sim.farm.critters[cc];
            var p = sim.farm.pathPoint(k.pos);
            var kx = ox + p.x * s, ky = oy + p.y * s;
            if (k.hop > 0) k.hop = Math.max(0, k.hop - dt * 2.6);
            drawCritter(ctx, k, kx, ky - k.hop * s * 0.6, s);
        }

        /* reactions drawn over critters (flares, pulses) */
        drawReactions(ctx, t, s, ox, oy, false);

        /* night tint + fireflies */
        if (night) {
            ctx.fillStyle = 'rgba(10,14,40,0.28)';
            ctx.fillRect(0, 0, w, h);
            for (var fl = 0; fl < 12; fl++) {
                var fx2 = (fl * 0.618) % 1, fy2 = (fl * 0.43) % 1;
                ctx.fillStyle = 'rgba(210,255,150,0.8)';
                ctx.fillRect(w * fx2, h * fy2 + Math.sin(t * 0.001 + fl) * 6, 2, 2);
            }
        }
    }

    function drawFarmFire(ctx, x, y, s, t, frozen) {
        var flame = frozen ? 1 : Math.sin(t * 0.005) * 0.5 + 0.5;
        ctx.fillStyle = '#5a3a24';
        ctx.fillRect(x - 0.35 * s, y - 0.3 * s, 0.7 * s, 0.6 * s);
        ctx.fillStyle = 'rgba(240,120,40,0.9)';
        ctx.beginPath(); ctx.arc(x, y - 0.1 * s, (0.16 + flame * 0.1) * s, 0, 6.2832); ctx.fill();
        ctx.fillStyle = 'rgba(255,220,90,0.95)';
        ctx.beginPath(); ctx.arc(x, y - 0.1 * s, (0.08 + flame * 0.05) * s, 0, 6.2832); ctx.fill();
    }

    function drawCritter(ctx, k, x, y, s) {
        var r = 0.34 * s;
        ctx.fillStyle = k.color;
        ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();       // body
        ctx.fillStyle = 'rgba(0,0,0,0.2)';
        ctx.beginPath(); ctx.arc(x, y + r * 0.5, r * 0.5, 0, 6.2832); ctx.fill(); // shadow
        if (k.kind === 'ken') {
            ctx.fillStyle = '#223';                                       // Ken's hat
            ctx.fillRect(x - r * 0.6, y - r * 1.5, r * 1.2, r * 0.6);
        } else if (k.kind === 'hen') {
            ctx.fillStyle = '#c0392b';
            ctx.beginPath(); ctx.moveTo(x - r * 0.7, y - r * 0.4);
            ctx.lineTo(x, y - r * 1.2); ctx.lineTo(x + r * 0.5, y - r * 0.3); ctx.closePath(); ctx.fill();
        }
        if (k.pause > 0) { /* idle "…" */ }
    }

    function drawReactions(ctx, t, s, ox, oy, under) {
        for (var i = sim.reactions.length - 1; i >= 0; i--) {
            var rr = sim.reactions[i];
            var age = t - rr.start;
            if (age > rr.life) { sim.reactions.splice(i, 1); continue; }
            var below = rr.kind === 'ripple' || rr.kind === 'dust';
            if (below !== under) continue;
            var x = ox + rr.x * s, y = oy + rr.y * s;
            var p = age / rr.life;
            if (rr.kind === 'ripple' || rr.kind === 'pulse') {
                ctx.strokeStyle = rgba(200, 235, 255, 1 - p);
                ctx.lineWidth = 2;
                ctx.beginPath(); ctx.arc(x, y, p * rr.max * s, 0, 6.2832); ctx.stroke();
            } else if (rr.kind === 'flare') {
                ctx.fillStyle = rgba(255, 200, 80, (1 - p) * 0.85);
                ctx.beginPath(); ctx.arc(x, y, (0.4 + p * 1.2) * s, 0, 6.2832); ctx.fill();
            } else if (rr.kind === 'dust') {
                ctx.fillStyle = rgba(200, 180, 150, (1 - p) * 0.5);
                ctx.beginPath(); ctx.arc(x, y, (0.2 + p * 0.5) * s, 0, 6.2832); ctx.fill();
            }
        }
    }

    function farmPoke(px, py) {
        var s = farmScale(scene.width, scene.height) || 0;
        /* px,py are map coords relative to the farm origin; recompute from CSS size. */
        var w = scene.getBoundingClientRect().width, h = scene.getBoundingClientRect().height;
        s = farmScale(w, h);
        var ox = (w - MAP_COLS * s) / 2, oy = (h - MAP_ROWS * s) / 2;
        var mx = (px - ox) / s, my = (py - oy) / s;
        var now = performance.now() / 1000;

        /* pond */
        var dx = (mx - POND.x) / POND.rx, dy = (my - POND.y) / POND.ry;
        if (dx * dx + dy * dy <= 1) {
            sim.reactions.push({ x: POND.x, y: POND.y, start: now, life: 1.1, max: 2.4, kind: 'ripple' });
            return 'the pond ripples';
        }
        /* fire */
        if (Math.hypot(mx - FIRE.x, my - FIRE.y) < 0.9) {
            sim.reactions.push({ x: FIRE.x, y: FIRE.y, start: now, life: 0.5, max: 1.6, kind: 'flare' });
            return 'the fire flares';
        }
        /* stations */
        for (var i = 0; i < STATIONS.length; i++) {
            var q = STATIONS[i];
            if (Math.abs(mx - q.x) < q.w / 2 + 0.4 && Math.abs(my - q.y) < q.h / 2 + 0.4) {
                sim.reactions.push({ x: q.x, y: q.y, start: now, life: 0.6, max: 1.6, kind: 'pulse' });
                return 'the ' + q.name + ' glints';
            }
        }
        /* critters */
        for (var c = 0; c < sim.farm.critters.length; c++) {
            var k = sim.farm.critters[c];
            var p = sim.farm.pathPoint(k.pos);
            if (Math.hypot(mx - p.x, my - p.y) < 0.7) {
                k.hop = 1;
                return (k.kind === 'ken' ? 'Ken' : 'a critter') + ' hops';
            }
        }
        /* ground */
        sim.reactions.push({ x: mx, y: my, start: now, life: 0.4, max: 0.6, kind: 'dust' });
        return 'dust';
    }

    /* ══════════════════════════════════════════════════════════════════════
       Frame loop + the two public buttons.
       ══════════════════════════════════════════════════════════════════════ */
    var lastNow = 0;
    var bgFit = { w: 0, h: 0 };
    var sceneFit = { w: 0, h: 0 };
    var overlayOpen = false;

    function stepAndDraw(now) {
        var t = now / 1000;
        var dt = lastNow ? Math.min(0.25, (now - lastNow) / 1000) : 0.016;
        lastNow = now;
        var frozen = reduceMotion;

        if (sim.accentTick++ % 90 === 0) sim.accent = themeColor('--tf-accent', '#58a6ff');
        if (!frozen) sim.phase += 0.012;

        if (bgCtx && bgFit.w > 0) {
            drawScene(bgCtx, bgFit.w, bgFit.h, t, dt, frozen, null, null);
        }
        if (overlayOpen && sceneCtx && sceneFit.w > 0) {
            drawScene(sceneCtx, sceneFit.w, sceneFit.h, t, dt, frozen, null, null);
        }
        raf = requestAnimationFrame(stepAndDraw);
    }

    function drawScene(ctx, w, h, t, dt, frozen, pokeX, pokeY) {
        if (sim.scene === 'campfire') drawCampfire(ctx, w, h, t, frozen);
        else if (sim.scene === 'farm') drawFarm(ctx, w, h, t, dt, frozen, pokeX, pokeY);
        else drawFloor(ctx, w, h, t, frozen);
    }

    var raf = 0;
    function start() {
        if (raf) return;
        lastNow = 0;
        raf = requestAnimationFrame(stepAndDraw);
    }

    function fitBackdrop() {
        if (bg && bgCtx) bgFit = fit(bg, bgCtx);
        if (scene && sceneCtx) sceneFit = fit(scene, sceneCtx);
    }
    fitBackdrop();
    if (window.ResizeObserver) {
        try {
            var ro = new ResizeObserver(function () { fitBackdrop(); });
            ro.observe(bg); ro.observe(scene);
        } catch (e) { window.addEventListener('resize', fitBackdrop); }
    } else {
        window.addEventListener('resize', fitBackdrop);
    }
    start();

    /* ── public API ─────────────────────────────────────────────────────── */
    window.tfCycleBackdrop = function () {
        sim.scene = sim.scene === 'floor' ? 'campfire' : (sim.scene === 'campfire' ? 'farm' : 'floor');
        try { localStorage.setItem('trio_backdrop', sim.scene); } catch (e) {}
        var btn = document.getElementById('backdropBtn');
        if (btn) btn.title = 'Backdrop: ' + sim.scene + ' — click to switch';
        fitBackdrop();
        return sim.scene;
    };

    window.tfOpenScene = function () {
        if (!overlay || !scene) return;
        overlayOpen = true;
        overlay.style.display = 'block';
        fitBackdrop();
        var hint = document.getElementById('tfSceneHint');
        if (hint) hint.textContent = sim.scene === 'farm'
            ? 'click things to poke them · drag to turn · Esc to close'
            : 'drag to turn the view · Esc to close';
    };
    window.tfCloseScene = function () {
        if (!overlay) return;
        overlayOpen = false;
        overlay.style.display = 'none';
    };

    /* Scene overlay interaction: drag turns (floor) / pans stars; click pokes (farm). */
    var dragging = false, lastX = 0, lastY = 0;
    if (scene) {
        scene.addEventListener('pointerdown', function (e) {
            dragging = true; lastX = e.clientX; lastY = e.clientY;
            scene.setPointerCapture && scene.setPointerCapture(e.pointerId);
        });
        scene.addEventListener('pointermove', function (e) {
            if (!dragging) return;
            var dx = e.clientX - lastX, dy = e.clientY - lastY;
            lastX = e.clientX; lastY = e.clientY;
            if (sim.scene === 'floor') {
                sim.vpX = clamp(sim.vpX + dx, -200, 200);
                sim.vpY = clamp(sim.vpY + dy, -120, 120);
                sim.yaw += dx * 0.002;
            }
        });
        scene.addEventListener('pointerup', function (e) {
            if (!dragging) return;
            dragging = false;
            var dx = Math.abs(e.clientX - lastX), dy = Math.abs(e.clientY - lastY);
            if (dx < 5 && dy < 5 && sim.scene === 'farm') {
                var r = scene.getBoundingClientRect();
                farmPoke(e.clientX - r.left, e.clientY - r.top);
            }
        });
    }
    if (overlay) {
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) window.tfCloseScene();
        });
    }
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') window.tfCloseScene();
    });

    /* ── confetti (unchanged physics from Confetti.tsx) ──────────────────── */
    var confRaf = 0;
    window.tfCelebrate = function (originEl) {
        if (reduceMotion) return;
        var cv = confetti;
        if (!cv) return;
        var ctx = cv.getContext('2d');
        if (!ctx) return;
        if (confRaf) { cancelAnimationFrame(confRaf); confRaf = 0; }

        var dpr = Math.min(window.devicePixelRatio || 1, 2);
        var w = Math.max(1, window.innerWidth), h = Math.max(1, window.innerHeight);
        cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
        cv.style.width = w + 'px'; cv.style.height = h + 'px';
        cv.style.display = 'block';
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        var ox = w * 0.5, oy = h * 0.5;
        if (originEl && typeof originEl.getBoundingClientRect === 'function') {
            var r = originEl.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) { ox = r.left + r.width / 2; oy = r.top + r.height / 2; }
        }
        var palette = [sim.accent, themeColor('--tf-accent-2', '#a371f7'),
            '#3fb950', '#d29922', '#ff7b72', '#ffffff'];
        var parts = [];
        for (var i = 0; i < 140; i++) {
            var ang = Math.random() * Math.PI * 2, speed = 5 + Math.random() * 9;
            parts.push({
                x: ox, y: oy, vx: Math.cos(ang) * speed, vy: Math.sin(ang) * speed - 3,
                rot: Math.random() * Math.PI, vrot: (Math.random() - 0.5) * 0.4,
                size: 5 + Math.random() * 6, color: palette[(Math.random() * palette.length) | 0]
            });
        }
        var start = performance.now();
        function tick(now) {
            var elapsed = now - start;
            ctx.clearRect(0, 0, w, h);
            var fade = elapsed < 900 ? 1 : Math.max(0, 1 - (elapsed - 900) / 800);
            for (var p = 0; p < parts.length; p++) {
                var q = parts[p];
                q.vx *= 0.985; q.vy = q.vy * 0.985 + 0.18;
                q.x += q.vx; q.y += q.vy; q.rot += q.vrot;
                ctx.save(); ctx.globalAlpha = fade;
                ctx.translate(q.x, q.y); ctx.rotate(q.rot);
                ctx.fillStyle = q.color;
                ctx.fillRect(-q.size / 2, -q.size / 2, q.size, q.size * 0.6);
                ctx.restore();
            }
            if (elapsed < 1700) confRaf = requestAnimationFrame(tick);
            else { ctx.clearRect(0, 0, w, h); cv.style.display = 'none'; confRaf = 0; }
        }
        confRaf = requestAnimationFrame(tick);
    };
})();
