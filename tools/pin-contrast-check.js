// Measure the real, computed contrast of text on a corkboard pin.
//
// Why: the pin "paper" is always light (its own gradients) while the app theme paints
// ordinary text near-white. static/themes.css already forces the pin text dark, but a
// colour is only half the story - an element with opacity, or one that inherits a
// translucent colour, composites to something else entirely. This reads what the engine
// actually renders and computes the WCAG contrast ratio against the pin's own gradient,
// so "hard to read" becomes a number instead of an opinion.
//
// No screenshot: pins contain the user's own notes, and a capture could include them.
const { spawn } = require('child_process');
const http = require('http');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9451;
const URL = process.env.TF_URL || 'https://127.0.0.1:5003/corkboard';
const profile = process.env.TEMP + '\\tf_contrast_' + Date.now();
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--ignore-certificate-errors',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile, '--no-first-run',
  '--window-size=1400,900', URL], { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));
const getJSON = p => new Promise((res, rej) => http.get({ host: '127.0.0.1', port: PORT, path: p },
  r => { let d = ''; r.on('data', c => d += c); r.on('end', () => res(JSON.parse(d))); }).on('error', rej));

// WCAG relative luminance + contrast, computed in the page so it uses the real colours
const MEASURE = `(() => {
  function parse(c) {
    const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function lum({r, g, b}) {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  }
  function over(fg, bg) {  // composite fg (with alpha) onto bg
    return { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a),
             b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 };
  }
  function ratio(a, b) {
    const la = lum(a), lb = lum(b);
    const hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }
  // effective opacity of an element = product of its own and its ancestors' opacity
  function effOpacity(el) {
    let o = 1, n = el;
    while (n && n.nodeType === 1) { o *= parseFloat(getComputedStyle(n).opacity || '1'); n = n.parentElement; }
    return o;
  }
  const pin = document.querySelector('.pin');
  if (!pin) return JSON.stringify({ error: 'no .pin on the board' });
  const pinCS = getComputedStyle(pin);
  // the pin's paper colour: take the first colour of its background gradient, else background-color
  let paper = parse(pinCS.backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
  const m = pinCS.backgroundImage.match(/rgba?\\([^)]+\\)/g);
  if (m && m.length) paper = parse(m[0]);
  const out = { paper: 'rgb(' + paper.r + ',' + paper.g + ',' + paper.b + ')', items: [] };
  for (const [label, sel] of [['title', '.pin-title'], ['content', '.pin-content p'],
                              ['content (plain)', '.pin-content'], ['timestamp', '.pin-timestamp'],
                              ['tag', '.pin-tags .tag-label']]) {
    const el = pin.querySelector(sel) || (sel === '.pin-content p' ? pin.querySelector('.pin-content') : null);
    if (!el) { out.items.push({ label, missing: true }); continue; }
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    const alpha = fg.a * effOpacity(el);
    const composited = over({ ...fg, a: alpha }, paper);
    out.items.push({
      label,
      declared: cs.color,
      opacity: Number(effOpacity(el).toFixed(2)),
      fontSize: cs.fontSize,
      weight: cs.fontWeight,
      contrast: Number(ratio(composited, paper).toFixed(2)),
    });
  }
  return JSON.stringify(out);
})()`;

(async () => {
  let targets = null;
  for (let i = 0; i < 60; i++) {
    try { targets = await getJSON('/json'); if (targets.some(t => t.type === 'page')) break; } catch (e) {}
    await sleep(500);
  }
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
  const send = (method, params) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  await new Promise(r => ws.onopen = r);
  await send('Page.enable', {});
  await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(9000);

  // The toast/pin text is hardcoded light (it was designed for a dark card), so the
  // THEME matters: on a warm accent the same text can land on a light background. My
  // first run used a fresh profile, i.e. the default theme, which is why it measured
  // clean. TF_THEME lets the check run against the theme the user actually sees.
  if (process.env.TF_THEME) {
    await send('Runtime.evaluate', {
      expression: `try { localStorage.setItem('trio_theme', ${JSON.stringify(process.env.TF_THEME)});
        localStorage.setItem('theme', ${JSON.stringify(process.env.TF_THEME)}); } catch (e) {}`,
      returnByValue: true,
    });
    await send('Page.reload', { ignoreCache: true });
    await sleep(9000);
    console.log('  (measured with theme forced to: ' + process.env.TF_THEME + ')');
  }

  const r = await send('Runtime.evaluate', { expression: MEASURE, returnByValue: true, awaitPromise: true });
  const raw = r.result && r.result.result ? r.result.result.value : null;
  const data = raw ? JSON.parse(raw) : null;

  // With TF_PROBE_FIX=1, neutralise the timestamp's opacity in the live page and measure
  // again. That shows what the stylesheet change does to the real engine, without needing
  // the server restarted to serve the new CSS.
  if (process.env.TF_PROBE_FIX && data && !data.error) {
    await send('Runtime.evaluate', {
      expression: `document.querySelectorAll('.pin-timestamp').forEach(e => e.style.opacity = '1');`,
      returnByValue: true,
    });
    const r2 = await send('Runtime.evaluate', { expression: MEASURE, returnByValue: true, awaitPromise: true });
    const raw2 = r2.result && r2.result.result ? r2.result.result.value : null;
    if (raw2) {
      const d2 = JSON.parse(raw2);
      const t = d2.items.find(i => i.label === 'timestamp');
      if (t) console.log('  with the timestamp opacity neutralised -> ' + t.contrast + ':1  '
        + (t.contrast >= 4.5 ? 'OK' : 'still low'));
    }
  }
  if (!data || data.error) {
    console.log('  could not measure:', data ? data.error : JSON.stringify(r.result).slice(0, 200));
  } else {
    console.log('  pin paper colour:', data.paper);
    console.log('  ' + 'element'.padEnd(18) + 'declared colour'.padEnd(22) + 'opacity'.padEnd(9) + 'size'.padEnd(7) + 'contrast');
    let worst = 99;
    for (const it of data.items) {
      if (it.missing) { console.log('  ' + it.label.padEnd(18) + '(not present on this pin)'); continue; }
      const flag = it.contrast >= 4.5 ? 'OK' : (it.contrast >= 3 ? 'low' : 'BAD');
      worst = Math.min(worst, it.contrast);
      console.log('  ' + it.label.padEnd(18) + it.declared.padEnd(22) + String(it.opacity).padEnd(9) + it.fontSize.padEnd(7) + it.contrast + ':1  ' + flag);
    }
    console.log('\n  worst contrast on this pin: ' + worst + ':1   (WCAG AA needs 4.5:1)');
  }
  ws.close(); chrome.kill(); process.exit(0);
})();
