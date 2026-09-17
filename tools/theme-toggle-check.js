// Drive the REAL app in headless Chrome over the DevTools protocol, so the toggle can be
// clicked and the result measured - the one thing that could not be tested from outside.
// Node 21+ has a global WebSocket, so this needs no dependencies.
const { spawn } = require('child_process');
const http = require('http');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9333;
const URL_ = process.argv[2] || 'https://127.0.0.1:5003/corkboard';
const profile = process.env.TEMP + '\\tf_cdp_' + Date.now();

const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--ignore-certificate-errors',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile,
    '--no-first-run', '--window-size=1400,880', URL_
], { stdio: 'ignore' });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const getJSON = (path) => new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path }, r => {
        let d = ''; r.on('data', c => d += c); r.on('end', () => res(JSON.parse(d)));
    }).on('error', rej);
});

(async () => {
    let targets = null;
    for (let i = 0; i < 40; i++) {
        try { targets = await getJSON('/json'); if (targets.some(t => t.type === 'page')) break; } catch (e) {}
        await sleep(500);
    }
    const page = targets.find(t => t.type === 'page');
    if (!page) { console.log('no page target'); chrome.kill(); process.exit(1); }

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    let id = 0;
    const pending = new Map();
    ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    };
    const send = (method, params) => new Promise(res => {
        const myId = ++id;
        pending.set(myId, res);
        ws.send(JSON.stringify({ id: myId, method, params }));
    });
    await new Promise(res => ws.onopen = res);

    const evaluate = async (expr) => {
        const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
        return r.result && r.result.result ? r.result.result.value : r.result;
    };

    await sleep(6000);   // let the app settle

    const snapshot = `(() => {
        const g = (s) => { const el = document.querySelector(s); if (!el) return 'absent';
            const cs = getComputedStyle(el); return cs.backgroundColor; };
        return JSON.stringify({
            hasTfSetLight: typeof window.tfSetLight,
            hasTfTheme: typeof window.tfSetTheme,
            themeAttr: document.documentElement.dataset.theme || '(none)',
            resolved: document.documentElement.dataset.tfTheme || '(none)',
            lightMode: document.documentElement.classList.contains('light-mode'),
            body: g('body'), topbar: g('.top-bar'), toolbar: g('.toolbar'),
            board: g('.board'), main: g('.main')
        });
    })()`;

    console.log('=== BEFORE the click ===');
    console.log('  ' + await evaluate(snapshot));

    // click the real toggle, the way a user does
    const clicked = await evaluate(`(() => {
        const t = document.getElementById('themeToggleOuter');
        if (!t) return 'no toggle on this page';
        t.click();
        return 'clicked';
    })()`);
    console.log('=== click: ' + clicked + ' ===');
    await sleep(1500);
    console.log('=== AFTER the click ===');
    console.log('  ' + await evaluate(snapshot));

    ws.close();
    chrome.kill();
    process.exit(0);
})();
