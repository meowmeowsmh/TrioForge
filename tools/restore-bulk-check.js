// Verify the multi-select clean-up in the 🗄️ restore modal, in real headless Chrome.
//
// The list is often dozens of near-identical rows (every delete of the same chat
// archives another copy), so the point of this check is that a whole selection can
// be acted on at once - and that nothing happens without the confirm.
//
// It deliberately CANCELS every confirm, so running it never deletes real archives:
// the assertion is that the rows are still there afterwards.
//
//   node tools/restore-bulk-check.js [url]      (default https://127.0.0.1:5003/)

const { spawn } = require('child_process');
const http = require('http');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9357;
const URL_ = process.argv[2] || 'https://127.0.0.1:5003/';
const profile = process.env.TEMP + '\\tf_rb_' + Date.now();

const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--ignore-certificate-errors',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile,
    '--no-first-run', '--window-size=1400,900', URL_
], { stdio: 'ignore' });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const getJSON = (p) => new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, r => {
        let d = ''; r.on('data', c => d += c); r.on('end', () => res(JSON.parse(d)));
    }).on('error', rej);
});

let failures = 0;
const check = (label, ok, detail) => {
    if (!ok) failures++;
    console.log((ok ? '  OK   ' : '  FAIL ') + label + (detail === undefined ? '' : '  -> ' + detail));
};

(async () => {
    let targets = null;
    for (let i = 0; i < 40; i++) {
        try { targets = await getJSON('/json'); if (targets.some(t => t.type === 'page')) break; } catch (e) {}
        await sleep(500);
    }
    const page = (targets || []).find(t => t.type === 'page');
    if (!page) { console.log('  FAIL could not attach to Chrome'); process.exit(1); }
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    let id = 0; const pending = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data);
        if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
    const send = (method, params) => new Promise(res => {
        const myId = ++id; pending.set(myId, res);
        ws.send(JSON.stringify({ id: myId, method, params })); });
    await new Promise(res => ws.onopen = res);
    await sleep(8000);

    const ev = async (expr) => {
        const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true,
                                                  awaitPromise: true });
        if (r.result && r.result.exceptionDetails) return 'ERROR: ' + JSON.stringify(r.result.exceptionDetails.text);
        return r.result && r.result.result ? r.result.result.value : JSON.stringify(r.result);
    };

    // Never delete anything: every confirm is answered "no".
    await ev(`window.__confirmCalls = 0; window.confirm = function(){ window.__confirmCalls++; return false; };`);

    // Open the modal and wait for the lists to arrive.
    await ev(`openRestoreModal()`);
    await sleep(2500);
    await ev(`loadRestoreLists()`);
    await sleep(2500);

    const shape = await ev(`(() => {
        const el = document.getElementById('restoreConvs');
        if (!el) return 'no container';
        return JSON.stringify({
            rows: el.querySelectorAll('input[data-restore-row]').length,
            all: !!document.getElementById('restoreConvsAll'),
            count: (document.getElementById('restoreConvsCount') || {}).textContent || '',
            deleteDisabled: (document.getElementById('restoreConvsBulkDelete') || {}).disabled,
            restoreDisabled: (document.getElementById('restoreConvsBulkRestore') || {}).disabled,
            perRow: el.querySelectorAll('button').length
        });
    })()`);
    console.log('  list:', shape);
    let s;
    try { s = JSON.parse(shape); } catch (e) { console.log('  FAIL could not read the list'); process.exit(1); }

    check('each row has a checkbox', s.rows > 0, s.rows + ' rows');
    check('a select-all control exists', s.all === true);
    check('bulk buttons start disabled', s.deleteDisabled === true && s.restoreDisabled === true);
    check('per-row Restore/✕ kept', s.perRow >= s.rows * 2, s.perRow + ' buttons');

    // Select all -> the count updates and both buttons arm.
    await ev(`(() => { const cb = document.getElementById('restoreConvsAll');
                       cb.checked = true; cb.onchange(); })()`);
    await sleep(400);
    const all = await ev(`(() => JSON.stringify({
        count: (document.getElementById('restoreConvsCount') || {}).textContent || '',
        checked: document.querySelectorAll('#restoreConvs input[data-restore-row]:checked').length,
        deleteDisabled: (document.getElementById('restoreConvsBulkDelete') || {}).disabled,
        restoreDisabled: (document.getElementById('restoreConvsBulkRestore') || {}).disabled
    }))()`);
    console.log('  after select-all:', all);
    const a = JSON.parse(all);
    check('select-all ticks every row', a.checked === s.rows, a.checked + '/' + s.rows);
    check('the count says how many are picked', a.count.indexOf(String(s.rows)) === 0, a.count);
    check('bulk buttons arm when something is picked',
          a.deleteDisabled === false && a.restoreDisabled === false);

    // Selecting one row only.
    await ev(`(() => { const cb = document.getElementById('restoreConvsAll');
                       cb.checked = false; cb.onchange();
                       const row = document.querySelector('#restoreConvs input[data-restore-row]');
                       row.checked = true; row.onchange(); })()`);
    await sleep(400);
    const one = JSON.parse(await ev(`(() => JSON.stringify({
        count: (document.getElementById('restoreConvsCount') || {}).textContent || '',
        deleteDisabled: (document.getElementById('restoreConvsBulkDelete') || {}).disabled
    }))()`));
    check('one row reads as 1 selected', one.count.indexOf('1 selected') === 0, one.count);
    check('the delete button is live', one.deleteDisabled === false);

    // Ask to delete: the confirm is refused, so nothing may change.
    const rowsBefore = s.rows;
    await ev(`document.getElementById('restoreConvsBulkDelete').click()`);
    await sleep(1200);
    const after = JSON.parse(await ev(`(() => JSON.stringify({
        confirms: window.__confirmCalls,
        rows: document.querySelectorAll('#restoreConvs input[data-restore-row]').length
    }))()`));
    check('a destructive action asks first', after.confirms > 0, after.confirms + ' confirm(s)');
    check('refusing the confirm deletes nothing', after.rows === rowsBefore,
          after.rows + ' rows still there');

    console.log(failures ? `\n  ${failures} CHECK(S) FAILED` : '\n  all checks passed');
    chrome.kill();
    process.exit(failures ? 1 : 0);
})();
