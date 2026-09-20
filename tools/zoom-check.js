// Check the in-app zoom: Ctrl +/-, Ctrl+0 and Ctrl+wheel.
//
// Why this exists: the app window cannot use the browser's own zoom. pywebview's WebView2
// backend enables AreBrowserAcceleratorKeysEnabled only in debug mode, so Ctrl +/- do
// nothing there even though they work in a browser. The app therefore implements its own
// zoom, and this checks that it actually works, in a real engine, with real events.
//
// It also doubles as a smoke test for the page: if the zoom module were inserted badly,
// the whole script block would fail to parse and app globals such as newChat would be
// undefined. That is checked first.
//
// No screenshot is taken: the app holds private conversation, and a capture could contain
// it. The state checks below are the verification.
const { spawn } = require('child_process');
const http = require('http');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9431;
const profile = process.env.TEMP + '\\tf_zoom_' + Date.now();
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--ignore-certificate-errors',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile, '--no-first-run',
  '--window-size=1100,800', 'https://127.0.0.1:5003/'], { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));
const getJSON = p => new Promise((res, rej) => http.get({ host: '127.0.0.1', port: PORT, path: p },
  r => { let d = ''; r.on('data', c => d += c); r.on('end', () => res(JSON.parse(d))); }).on('error', rej));

(async () => {
  let targets = null;
  for (let i = 0; i < 60; i++) {
    try { targets = await getJSON('/json'); if (targets.some(t => t.type === 'page')) break; } catch (e) {}
    await sleep(500);
  }
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const errors = [];
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') {
      errors.push((m.params.exceptionDetails.exception || {}).description || 'exception');
    }
  };
  const send = (method, params) => new Promise(res => {
    const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params }));
  });
  await new Promise(r => ws.onopen = r);
  await send('Page.enable', {});
  await send('Runtime.enable', {});
  await sleep(7000);

  const ev = async expr => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    return r.result && r.result.result ? r.result.result.value : JSON.stringify(r.result);
  };
  const zoom = () => ev('document.documentElement.style.zoom || "(unset)"');
  const press = key => ev(`(() => { document.dispatchEvent(
      new KeyboardEvent('keydown', {key:${JSON.stringify(key)}, ctrlKey:true, bubbles:true, cancelable:true})); })()`);
  const wheel = (dy) => ev(`(() => { document.dispatchEvent(
      new WheelEvent('wheel', {deltaY:${dy}, ctrlKey:true, bubbles:true, cancelable:true})); })()`);

  let pass = 0, fail = 0;
  const check = (label, got, want) => {
    const ok = String(got) === String(want);
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}: got ${got}, expected ${want}`);
    ok ? pass++ : fail++;
  };

  console.log('1. the page parsed and the app is alive (my edit did not break the script)');
  check('newChat is a function', await ev('typeof newChat'), 'function');
  check('doSend is a function', await ev('typeof doSend'), 'function');
  check('tfApplyZoom is a function', await ev('typeof tfApplyZoom'), 'function');

  console.log('2. zoom in / out / reset with Ctrl +, Ctrl -, Ctrl 0');
  await press('='); await sleep(200);
  check('after Ctrl+=', await zoom(), '1.1');
  await press('='); await sleep(200);
  check('after Ctrl+= twice', await zoom(), '1.2');
  await press('-'); await sleep(200);
  check('after Ctrl+-', await zoom(), '1.1');
  await press('0'); await sleep(200);
  check('after Ctrl+0', await zoom(), '1');

  console.log('3. Ctrl+wheel');
  await wheel(-120); await sleep(200);
  check('after Ctrl+wheel up', await zoom(), '1.1');
  await wheel(120); await sleep(200);
  check('after Ctrl+wheel down', await zoom(), '1');

  console.log('4. limits and persistence');
  for (let i = 0; i < 20; i++) await press('-');
  await sleep(300);
  check('zoomed all the way out clamps at', await zoom(), '0.6');
  for (let i = 0; i < 30; i++) await press('=');
  await sleep(300);
  check('zoomed all the way in clamps at', await zoom(), '2');
  check('saved in localStorage', await ev("localStorage.getItem('tf_zoom')"), '2');
  await press('0'); await sleep(200);
  check('reset saves too', await ev("localStorage.getItem('tf_zoom')"), '1');

  console.log('5. page errors');
  console.log('  ' + (errors.length ? errors.slice(0, 3).join(' | ') : 'none'));

  console.log(`\n  ${pass} passed, ${fail} failed`);
  ws.close(); chrome.kill(); process.exit(fail ? 1 : 0);
})();
