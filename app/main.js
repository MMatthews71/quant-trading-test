'use strict';

const { app, BrowserWindow, ipcMain, Menu, dialog } = require('electron');
const path = require('path');
const { pathToFileURL } = require('url');
const fs = require('fs');
const os = require('os');
const { spawn, spawnSync, execFile } = require('child_process');
const initSqlJs = require('sql.js');
const { lttb } = require('./lttb');

// Headless/CI diagnostics only (set QUANT_DIAG=1): avoids GPU crashes in
// environments without a working display. Not used in normal desktop runs.
if (process.env.QUANT_DIAG) app.disableHardwareAcceleration();

// --------------------------------------------------------------------------- //
// Paths
// --------------------------------------------------------------------------- //
const PROJECT_ROOT = path.join(__dirname, '..');
// The app in app/, the Python engine in engine/. Everything that belongs to a
// strategy (its manifest, params, bar database and generated outputs) lives in
// that strategy's folder, engine/strategies/<name>/.
//   QUANT_TRADING_ROOT -> repo root holding app/ and engine/ (default: here)
//   QUANT_DB           -> override the strategy's SQLite file
const QUANT_ROOT = process.env.QUANT_TRADING_ROOT || PROJECT_ROOT;
// Python lives in engine/; scripts are launched with cwd=QUANT_ROOT.
const ENGINE = 'engine';
const STRATEGIES_DIR = path.join(QUANT_ROOT, ENGINE, 'strategies');

// --------------------------------------------------------------------------- //
// The active strategy. The app itself is strategy-agnostic: everything
// instrument-specific comes from the strategy's folder: its manifest.json (bar
// table + columns, tunable params, data tables) and its own screens in ui/.
// Selected by $QUANT_STRATEGY, else "strategy" in engine/config.json if that
// file exists, else the only strategy folder (same rule as core/strategy.py).
// Exported so every Python child process uses the same one.
// --------------------------------------------------------------------------- //
function activeStrategyName() {
  if (process.env.QUANT_STRATEGY) return process.env.QUANT_STRATEGY;
  try {
    const name = JSON.parse(fs.readFileSync(path.join(QUANT_ROOT, ENGINE, 'config.json'), 'utf8')).strategy;
    if (name) return name;
  } catch {}
  let found = [];
  try {
    found = fs.readdirSync(STRATEGIES_DIR)
      .filter((d) => fs.existsSync(path.join(STRATEGIES_DIR, d, 'manifest.json')));
  } catch {}
  if (found.length === 1) return found[0];
  throw new Error(`No strategy selected: ${found.length} strategy folders in ${STRATEGIES_DIR}. ` +
                  'Set QUANT_STRATEGY or add {"strategy": "<name>"} to engine/config.json.');
}
const STRATEGY = activeStrategyName();
process.env.QUANT_STRATEGY = STRATEGY;
const STRATEGY_DIR = path.join(STRATEGIES_DIR, STRATEGY);
const MANIFEST = JSON.parse(fs.readFileSync(path.join(STRATEGY_DIR, 'manifest.json'), 'utf8'));
// The strategy's data folder: its bar database + everything the engine writes.
const DATA_DIR = path.join(STRATEGY_DIR, 'data');
const DB_PATH = process.env.QUANT_DB || path.join(DATA_DIR, 'quant.db');

const FINAL_TABLE = MANIFEST.market_table;
const FINAL_TS = MANIFEST.columns.ts;
// The strategy's two parameter sets (Backtest tab / Live tab). The Python engine
// reads the same files (core/params.py) with the same rules.
const STRATEGY_PARAMS_PATH = path.join(STRATEGY_DIR, 'backtest_params.json');
const LIVE_PARAMS_PATH = path.join(STRATEGY_DIR, 'live_params.json');
// Outputs the engine writes and the app only READS.
const BACKTEST_JSON = path.join(DATA_DIR, 'backtest.json');
const LIVE_SIGNAL_JSON = path.join(DATA_DIR, 'live_signal.json');

// Every Python task goes through the engine's one entry point:
//   quant.py backtest | live [--watch] | export backtest|live OUT | update --loop
//            | table INPUT CMD | notify KIND
const ENGINE_CLI = `${ENGINE}/quant.py`;
const engineArgs = (...args) => ['-u', ENGINE_CLI, ...args];

// Params, from the manifest: [{key, default, step?, min?, max?, choices?, label?, title?}].
// `step` (when a finite number) both drives the number input's stepping AND
// snaps the stored value to the nearest multiple; `min`/`max` clamp it. These
// hold even if the JSON is hand-edited.
const STRATEGY_PARAM_KEYS = MANIFEST.params.map((p) => p.key);
const STRATEGY_PARAM_DEFAULTS = Object.fromEntries(MANIFEST.params.map((p) => [p.key, p.default]));
const STRATEGY_PARAM_META = Object.fromEntries(MANIFEST.params.map((p) => {
  const { key, default: _d, ...meta } = p;
  return [key, meta];
}));

// Coerce one value to its constraints (snap to `step`, clamp to `min`/`max`).
function snapParam(k, v) {
  const m = STRATEGY_PARAM_META[k];
  if (!m) return v;
  if (Number.isFinite(m.step) && m.step > 0) v = Math.round(v / m.step) * m.step;
  if (Number.isFinite(m.min)) v = Math.max(v, m.min);
  if (Number.isFinite(m.max)) v = Math.min(v, m.max);
  return v;
}

// Validate/normalise a raw value for one key. Choice params return a valid choice
// (default if invalid); numeric params return a snapped number, or undefined if
// the value isn't a finite number (so the caller keeps the existing value).
function coerceParam(k, val) {
  const m = STRATEGY_PARAM_META[k];
  if (m && Array.isArray(m.choices)) {
    return m.choices.includes(val) ? val : STRATEGY_PARAM_DEFAULTS[k];
  }
  const v = Number(val);
  return Number.isFinite(v) ? snapParam(k, v) : undefined;
}

const MAX_POINTS = 2000; // downsample target per chart
const RANGE_DAYS = { '1D': 1, '1W': 7, '1M': 30, '3M': 90, '1Y': 365, 'ALL': null };

// The strategy's own views (optional): engine/strategies/<name>/ui/ui.js (+ ui.css).
// The renderer loads them; they plug into the Live and Input sub-tabs via window.QT.
const UI_DIR = path.join(STRATEGY_DIR, 'ui');

// --------------------------------------------------------------------------- //
// SQLite (sql.js / WASM) — read-only snapshot of quant.db, auto-reloaded
// when the file changes on disk (the Python updater checkpoints each cycle).
// --------------------------------------------------------------------------- //
let SQL = null;
let db = null;
let dbMtimeMs = 0;

async function ensureDb() {
  if (SQL === null) {
    SQL = await initSqlJs({
      locateFile: (f) => path.join(__dirname, 'node_modules', 'sql.js', 'dist', f),
    });
  }
  if (!fs.existsSync(DB_PATH)) {
    throw new Error(`Database not found at ${DB_PATH}. Start the updater (Log tab) to build it.`);
  }
  const mtime = fs.statSync(DB_PATH).mtimeMs;
  if (db === null || mtime !== dbMtimeMs) {
    const buf = fs.readFileSync(DB_PATH);
    if (db) db.close();
    db = new SQL.Database(buf);
    dbMtimeMs = mtime;
  }
  return db;
}

// Quote an SQL identifier (table/column) so it is always a legal name.
function _col(name) {
  return '"' + String(name).replace(/"/g, '""') + '"';
}

function queryAll(sql, binds) {
  const stmt = db.prepare(sql);
  if (binds) stmt.bind(binds);
  const out = [];
  while (stmt.step()) out.push(stmt.get());
  stmt.free();
  return out;
}

// Parse the wall-clock portion of "YYYY-MM-DD HH:MM:SS+HH:MM" as if it were
// UTC, so lightweight-charts (which renders numeric time in UTC) shows the
// strategy's local wall time on the axis.
function wallSeconds(ts) {
  return Math.floor(Date.parse(ts.slice(0, 19) + 'Z') / 1000);
}
// Absolute instant (real UTC) for range-window math.
function absMs(ts) {
  return Date.parse(ts.slice(0, 19).replace(' ', 'T') + ts.slice(19));
}

// Everything the renderer needs to lay itself out for the active strategy.
function getAppConfig() {
  return {
    strategy: STRATEGY,
    title: MANIFEST.title || STRATEGY,
    symbol: MANIFEST.symbol || STRATEGY,
    tzLabel: MANIFEST.timezone_label || MANIFEST.timezone || '',
    paramGroups: MANIFEST.param_groups || [],
    ui: {
      script: fs.existsSync(path.join(UI_DIR, 'ui.js')) ? pathToFileURL(path.join(UI_DIR, 'ui.js')).href : null,
      style: fs.existsSync(path.join(UI_DIR, 'ui.css')) ? pathToFileURL(path.join(UI_DIR, 'ui.css')).href : null,
    },
  };
}

// Read one column of the bar table as a chart series: range-filter, drop NaNs, enforce
// unique time keys, LTTB-downsample to `cap`, and build the {points, meta} shape
// the renderer expects. `scale` converts units (e.g. raw USD -> USD bn).
function finalSeries(column, range, cap, scale) {
  const s = scale || 1;
  const maxRows = queryAll(`SELECT MAX(${_col(FINAL_TS)}) FROM ${_col(FINAL_TABLE)}`);
  const maxTs = maxRows.length ? maxRows[0][0] : null;
  if (!maxTs) return { points: [], meta: { count: 0 } };

  const days = RANGE_DAYS[range];
  let rows;
  if (days == null) {
    rows = queryAll(
      `SELECT ${_col(FINAL_TS)}, ${_col(column)} FROM ${_col(FINAL_TABLE)} ORDER BY ${_col(FINAL_TS)}`
    );
  } else {
    const cutoffMs = absMs(maxTs) - days * 86400000;
    const cutoffUtc = new Date(cutoffMs).toISOString().slice(0, 19).replace('T', ' ');
    rows = queryAll(
      `SELECT ${_col(FINAL_TS)}, ${_col(column)} FROM ${_col(FINAL_TABLE)}
        WHERE julianday(${_col(FINAL_TS)}) >= julianday($cut)
        ORDER BY ${_col(FINAL_TS)}`,
      { $cut: cutoffUtc }
    );
  }

  const rawCount = rows.length;
  let pts = rows
    .filter((r) => r[1] != null)
    .map((r) => ({ x: wallSeconds(r[0]), y: r[1] * s }));

  // Ensure strictly-increasing, unique time keys for the chart.
  const seen = new Set();
  pts = pts.filter((p) => (seen.has(p.x) ? false : (seen.add(p.x), true)));

  const downsampled = pts.length > cap ? lttb(pts, cap) : pts;
  const points = downsampled.map((p) => ({ time: p.x, value: p.y }));

  // Headline stats over the non-null rows in the window (first/last real values).
  const nn = rows.filter((r) => r[1] != null);
  const first = nn.length ? nn[0] : null;
  const last = nn.length ? nn[nn.length - 1] : null;
  const firstPrice = first ? first[1] * s : null;
  const lastPrice = last ? last[1] * s : null;
  return {
    points,
    meta: {
      count: rawCount,
      plotted: points.length,
      firstTs: first ? first[0] : null,
      lastTs: last ? last[0] : null,
      firstPrice,
      lastPrice,
      change: firstPrice != null && lastPrice != null ? lastPrice - firstPrice : null,
      changePct: firstPrice ? ((lastPrice - firstPrice) / firstPrice) * 100 : null,
    },
  };
}

// Any column of the strategy's bar table, as a chart series (the strategy's UI
// asks for what it wants to plot). Checked against the table's real columns.
async function getSeries({ column, range, maxPoints, scale }) {
  await ensureDb();
  const cap = maxPoints && maxPoints > 2 ? maxPoints : MAX_POINTS;
  const cols = queryAll(`PRAGMA table_info(${_col(FINAL_TABLE)})`).map((r) => r[1]);
  if (!cols.includes(column)) throw new Error(`No column '${column}' in ${FINAL_TABLE}`);
  return finalSeries(column, range, cap, Number(scale) || 1);
}

// --------------------------------------------------------------------------- //
// Live updater control.
//
// The updater runs while this app is running: it is started automatically when
// the app opens and stopped when the app closes. Output is shown by tailing the
// shared updater.log file.
//
// The app also detects an updater started outside itself (a manual run) by
// scanning for a python process whose command line contains "quant.py update",
// so it won't launch a duplicate. The updater appends only rows newer than
// the last stored bar, so even a brief double-run can't duplicate data.
// --------------------------------------------------------------------------- //
const LOG_PATH = path.join(DATA_DIR, 'updater.log');

let child = null;         // updater spawned directly by the app (if any)
let logPos = 0;           // byte offset already tailed from updater.log
let logRemainder = '';    // partial trailing line carried between polls

function resolvePython() {
  // Trust an explicit override (run.bat exports this after detecting Python).
  if (process.env.QUANT_PYTHON) return process.env.QUANT_PYTHON;
  // Else auto-detect a conda 'bloomberg' env under the current user's profile.
  const home = os.homedir();
  const candidates = [
    path.join(home, 'anaconda3', 'envs', 'bloomberg', 'python.exe'),
    path.join(home, 'miniconda3', 'envs', 'bloomberg', 'python.exe'),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return 'python'; // last resort: rely on PATH
}

function sendAll(channel, payload) {
  for (const w of BrowserWindow.getAllWindows()) {
    if (!w.isDestroyed()) w.webContents.send(channel, payload);
  }
}

// Find a running updater (login-launched or manual) by command line. Returns
// its PID or null. Windows-only; other platforms rely on the app-spawned child.
function detectUpdaterPid() {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') return resolve(null);
    const ps =
      "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\"" +
      " | Where-Object { $_.CommandLine -like '*quant.py update*' }" +
      " | Select-Object -First 1 -ExpandProperty ProcessId; if ($p) { $p }";
    execFile(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command', ps],
      { windowsHide: true },
      (err, stdout) => {
        if (err) return resolve(null);
        const pid = parseInt((stdout || '').trim(), 10);
        resolve(Number.isFinite(pid) ? pid : null);
      }
    );
  });
}

async function updaterStatus() {
  if (child && child.exitCode === null) return { running: true, source: 'app', pid: child.pid };
  const pid = await detectUpdaterPid();
  if (pid) return { running: true, source: 'external', pid };
  return { running: false, source: 'none', pid: null };
}

function spawnDirect() {
  if (child && child.exitCode === null) return;
  const py = resolvePython();
  sendAll('updater:log', { stream: 'sys', line: `Starting updater: ${py} ${engineArgs('update', '--loop').join(' ')}` });
  // --loop keeps the updater running (a fetch/transform/append cycle every few
  // minutes) for as long as the app is open. stdout/stderr also land in
  // updater.log via the Python file handler, which the log tail displays — so we
  // don't re-emit the child's streams. Runs in the pipeline repo (QUANT_ROOT).
  child = spawn(py, engineArgs('update', '--loop'), { cwd: QUANT_ROOT, windowsHide: true });
  child.stdout.on('data', () => {});
  child.stderr.on('data', () => {});
  child.on('error', (e) => {
    sendAll('updater:log', { stream: 'sys', line: `Failed to launch: ${e.message}` });
    child = null;
    updaterStatus().then((s) => sendAll('updater:status', s));
  });
  child.on('close', () => {
    child = null;
    updaterStatus().then((s) => sendAll('updater:status', s));
  });
}

async function startUpdater() {
  const st = await updaterStatus();
  if (st.running) { sendAll('updater:status', st); return st; }
  spawnDirect();
  const s = await updaterStatus();
  sendAll('updater:status', s);
  return s;
}

function killPid(pid) {
  if (process.platform === 'win32') spawn('taskkill', ['/pid', String(pid), '/T', '/F']);
  else try { process.kill(pid, 'SIGTERM'); } catch {}
}

async function stopUpdater() {
  sendAll('updater:log', { stream: 'sys', line: 'Stopping updater...' });
  if (child && child.exitCode === null) {
    killPid(child.pid);
    child = null;
  } else {
    const pid = await detectUpdaterPid();
    if (pid) killPid(pid);
  }
  const s = { running: false, source: 'none' };
  sendAll('updater:status', s);
  return s;
}

// Auto-start on app launch if nothing is already running.
async function ensureRunning() {
  const st = await updaterStatus();
  if (st.running) sendAll('updater:status', st);
  else await startUpdater();
}

// ---- updater.log tail (shows output whoever started the updater) ----------- //
function emitLogLine(line) {
  // The updater logs as "HH:MM:SS | LEVEL | message".
  const stream = /\|\s*(ERROR|CRITICAL)\s*\|/.test(line) ? 'err' : 'out';
  sendAll('updater:log', { stream, line });
}

function pollLog() {
  let st;
  try {
    st = fs.statSync(LOG_PATH);
  } catch {
    return; // log not created yet
  }
  if (st.size < logPos) { logPos = 0; logRemainder = ''; } // rotated/truncated
  if (st.size <= logPos) return;
  const len = st.size - logPos;
  const buf = Buffer.alloc(len);
  const fd = fs.openSync(LOG_PATH, 'r');
  try { fs.readSync(fd, buf, 0, len, logPos); } finally { fs.closeSync(fd); }
  logPos = st.size;
  logRemainder += buf.toString('utf8');
  const parts = logRemainder.split(/\r?\n/);
  logRemainder = parts.pop();
  for (const line of parts) if (line) emitLogLine(line);
}

function startLogTail() {
  // Seed with the tail of any existing log so the panel isn't empty on open.
  try {
    const size = fs.statSync(LOG_PATH).size;
    const start = Math.max(0, size - 60000);
    const buf = Buffer.alloc(size - start);
    const fd = fs.openSync(LOG_PATH, 'r');
    try { fs.readSync(fd, buf, 0, buf.length, start); } finally { fs.closeSync(fd); }
    logPos = size;
    const parts = buf.toString('utf8').split(/\r?\n/);
    logRemainder = parts.pop();
    for (const line of parts) if (line) emitLogLine(line);
  } catch {
    logPos = 0;
  }
  setInterval(pollLog, 1000);
}

// Watch quant.db and data/backtest.json, telling the renderer when either changes
// so the charts / backtest tab refresh automatically when new data is written.
let lastSeenMtime = 0;
let lastSeenBtMtime = 0;
let lastSeenLiveMtime = 0;
function watchDb() {
  try { lastSeenMtime = fs.statSync(DB_PATH).mtimeMs; } catch {}
  try { lastSeenBtMtime = fs.statSync(BACKTEST_JSON).mtimeMs; } catch {}
  try { lastSeenLiveMtime = fs.statSync(LIVE_SIGNAL_JSON).mtimeMs; } catch {}
  setInterval(() => {
    let m;
    try { m = fs.statSync(DB_PATH).mtimeMs; } catch { m = null; }
    if (m !== null && m !== lastSeenMtime) {
      lastSeenMtime = m;
      sendAll('db:changed', null);
      runLiveSignal();   // no-op while the live watcher runs (it saw the change too)
    }
    let b;
    try { b = fs.statSync(BACKTEST_JSON).mtimeMs; } catch { b = null; }
    if (b !== null && b !== lastSeenBtMtime) {
      lastSeenBtMtime = b;
      sendAll('backtest:changed', null);
    }
    let l;
    try { l = fs.statSync(LIVE_SIGNAL_JSON).mtimeMs; } catch { l = null; }
    if (l !== null && l !== lastSeenLiveMtime) {
      lastSeenLiveMtime = l;
      sendAll('live:changed', null);
    }
  }, 1000);   // poll fast so the Live signal refreshes ~immediately after new data
}

// --------------------------------------------------------------------------- //
// Data tables: small hand-maintained tables the strategy declares in its
// manifest ("data_tables"), edited through the strategy's own UI. Read here;
// written through `quant.py table` since sql.js is read-only. Rows travel as
// objects keyed by DB column name; `id` is the row's key value.
// --------------------------------------------------------------------------- //
function dataTableSpec(table) {
  const spec = (MANIFEST.data_tables || []).find((d) => d.table === table);
  if (!spec) throw new Error(`Unknown data table '${table}'`);
  return spec;
}

async function listDataTable(table) {
  await ensureDb();
  const spec = dataTableSpec(table);
  const cols = Object.keys(spec.columns);
  let rows;
  try {
    rows = queryAll(`SELECT ${cols.map(_col).join(', ')} FROM ${_col(spec.table)} ORDER BY ${_col(spec.key)}`);
  } catch {
    return { rows: [] }; // table not created yet
  }
  const k = cols.indexOf(spec.key);
  return { rows: rows.map((r) => ({ id: r[k], ...Object.fromEntries(cols.map((c, i) => [c, r[i]])) })) };
}

function runTableStore(table, cmd, { id, row } = {}) {
  return new Promise((resolve) => {
    const args = engineArgs('table', dataTableSpec(table).table, cmd);
    if (id != null) args.push('--id', String(id));
    if (row) args.push('--row', JSON.stringify(row));
    execFile(resolvePython(), args, { cwd: QUANT_ROOT, windowsHide: true }, (err, stdout, stderr) => {
      if (err) return resolve({ ok: false, error: (stderr || err.message || `${cmd} failed`).trim() });
      dbMtimeMs = 0; // force the read-only snapshot to reload so the change is visible
      resolve({ ok: true });
    });
  });
}

// --------------------------------------------------------------------------- //
// Strategy parameters (strategies/<name>/backtest_params.json). The tunable
// variables declared in the manifest. The app only reads/writes the JSON; the
// engine loads it through core/params.py.
// --------------------------------------------------------------------------- //
function loadStrategyParams() {
  const out = { ...STRATEGY_PARAM_DEFAULTS };
  let raw;
  try { raw = JSON.parse(fs.readFileSync(STRATEGY_PARAMS_PATH, 'utf8')); }
  catch { raw = {}; }
  for (const k of STRATEGY_PARAM_KEYS) {
    if (raw && k in raw) {
      const c = coerceParam(k, raw[k]);
      if (c !== undefined) out[k] = c;
    }
  }
  return out;
}

function saveStrategyParams(patch) {
  const cur = loadStrategyParams();
  for (const k of STRATEGY_PARAM_KEYS) {
    if (patch && k in patch) {
      const c = coerceParam(k, patch[k]);
      if (c !== undefined) cur[k] = c;
    }
  }
  try {
    fs.writeFileSync(STRATEGY_PARAMS_PATH, JSON.stringify(cur, null, 2) + '\n', 'utf8');
    return { ok: true, params: cur };
  } catch (e) {
    return { ok: false, error: e.message, params: cur };
  }
}

// Live tab parameters: a SEPARATE set from the backtest, same fields/constraints,
// stored in live_params.json. Seeded from the backtest params the first time so
// the live signal starts from something sensible rather than neutral defaults.
function loadLiveParams() {
  const out = { ...STRATEGY_PARAM_DEFAULTS };
  let raw;
  try { raw = JSON.parse(fs.readFileSync(LIVE_PARAMS_PATH, 'utf8')); }
  catch { raw = null; }
  if (!raw) {
    // No live file yet — start from the backtest params.
    try { raw = JSON.parse(fs.readFileSync(STRATEGY_PARAMS_PATH, 'utf8')); }
    catch { raw = {}; }
  }
  for (const k of STRATEGY_PARAM_KEYS) {
    if (raw && k in raw) {
      const c = coerceParam(k, raw[k]);
      if (c !== undefined) out[k] = c;
    }
  }
  return out;
}

function saveLiveParams(patch) {
  const cur = loadLiveParams();
  for (const k of STRATEGY_PARAM_KEYS) {
    if (patch && k in patch) {
      const c = coerceParam(k, patch[k]);
      if (c !== undefined) cur[k] = c;
    }
  }
  try {
    fs.writeFileSync(LIVE_PARAMS_PATH, JSON.stringify(cur, null, 2) + '\n', 'utf8');
    return { ok: true, params: cur };
  } catch (e) {
    return { ok: false, error: e.message, params: cur };
  }
}

// --------------------------------------------------------------------------- //
// Backtest results (data/backtest.json). Written by `quant.py backtest`; the app
// just reads and renders it.
// --------------------------------------------------------------------------- //
function loadBacktest() {
  let st;
  try { st = fs.statSync(BACKTEST_JSON); }
  catch { return { ok: false, error: 'no-file', path: BACKTEST_JSON }; }
  try {
    const payload = JSON.parse(fs.readFileSync(BACKTEST_JSON, 'utf8'));
    return { ok: true, ...payload, mtime: st.mtimeMs };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// Run `quant.py backtest` for the active strategy; it writes
// data/backtest.json. Resolves after the
// process exits; the poller (and the nudge below) refresh the UI.
let backtestRunning = false;
function runBacktest() {
  return new Promise((resolve) => {
    if (backtestRunning) return resolve({ ok: false, error: 'A backtest is already running.' });
    backtestRunning = true;
    execFile(
      resolvePython(), engineArgs('backtest'),
      { cwd: QUANT_ROOT, windowsHide: true, maxBuffer: 64 * 1024 * 1024 },
      (err, stdout, stderr) => {
        backtestRunning = false;
        if (err) {
          const msg = (stderr || err.message || 'backtest failed').trim();
          return resolve({ ok: false, error: msg.slice(-4000) });
        }
        sendAll('backtest:changed', null); // nudge the UI to reload the fresh results
        resolve({ ok: true, output: (stdout || '').trim().slice(-4000) });
      }
    );
  });
}

// Export the full per-bar backtest CSV (every bar, every column) to a location
// the user picks. Nothing is kept in the data folder: the user chooses the save
// path first, then `quant.py export` recomputes from the current params and
// writes the CSV straight there, so the file only ever exists where they asked.
let exportRunning = false;
async function exportFullCsv({ which, title, defaultName }) {
  if (exportRunning) return { ok: false, error: 'An export is already running.' };
  const win = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  const { canceled, filePath } = await dialog.showSaveDialog(win, {
    title,
    defaultPath: `${defaultName}-${stamp}.csv`,
    filters: [{ name: 'CSV', extensions: ['csv'] }],
  });
  if (canceled || !filePath) return { ok: false, error: 'canceled' };

  exportRunning = true;
  return new Promise((resolve) => {
    execFile(
      resolvePython(), engineArgs('export', which, filePath),
      { cwd: QUANT_ROOT, windowsHide: true, maxBuffer: 16 * 1024 * 1024 },
      (err, _stdout, stderr) => {
        exportRunning = false;
        if (err) {
          const msg = (stderr || err.message || 'export failed').trim();
          return resolve({ ok: false, error: msg.slice(-4000) });
        }
        resolve({ ok: true, path: filePath });
      }
    );
  });
}

const exportBacktestCsv = () => exportFullCsv({
  which: 'backtest', title: 'Export full backtest CSV', defaultName: 'backtest-full',
});
const exportLiveCsv = () => exportFullCsv({
  which: 'live', title: 'Export full live CSV', defaultName: 'live-full',
});

// --------------------------------------------------------------------------- //
// Live signal (data/live_signal.json). Written by `quant.py live`, which reuses
// the exact backtest math (core/backtest.py) on the latest data with the live params.
// --------------------------------------------------------------------------- //
function loadLiveSignal() {
  let st;
  try { st = fs.statSync(LIVE_SIGNAL_JSON); }
  catch { return { ok: false, error: 'no-file', path: LIVE_SIGNAL_JSON }; }
  try {
    const payload = JSON.parse(fs.readFileSync(LIVE_SIGNAL_JSON, 'utf8'));
    return { ok: true, ...payload, mtime: st.mtimeMs };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// The live signal normally comes from a persistent `quant.py live --watch`
// process: it keeps the stored history in memory and recomputes as soon as the
// forming bar (data/live_tail.json, rewritten by the updater every cycle), the DB
// or live_params.json changes. The watchDb poll forwards each new
// live_signal.json to the renderer. Restarted if it dies while the app is open.
let liveWatcher = null;
let appQuitting = false;
function startLiveWatcher() {
  if (appQuitting || (liveWatcher && liveWatcher.exitCode === null)) return;
  liveWatcher = spawn(resolvePython(), engineArgs('live', '--watch'), { cwd: QUANT_ROOT, windowsHide: true });
  liveWatcher.stdout.on('data', () => {});
  liveWatcher.stderr.on('data', (d) => console.error('live signal:', d.toString().trim().slice(-1000)));
  liveWatcher.on('error', (e) => { console.error('live signal failed to launch:', e.message); liveWatcher = null; });
  liveWatcher.on('close', () => {
    liveWatcher = null;
    if (!appQuitting) setTimeout(startLiveWatcher, 3000);
  });
}
function stopLiveWatcherSync() {
  appQuitting = true;
  if (!liveWatcher || liveWatcher.exitCode !== null) { liveWatcher = null; return; }
  const pid = liveWatcher.pid;
  liveWatcher = null;
  try {
    if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(pid), '/T', '/F']);
    else process.kill(pid, 'SIGTERM');
  } catch {}
}

// One-off recompute — only needed when the watcher isn't running (it already
// recomputes on every change). Coalesced: if a run is in flight, mark it dirty
// and run once more when it finishes, so rapid updates don't pile up processes.
let liveRunning = false;
let liveDirty = false;
function runLiveSignal() {
  return new Promise((resolve) => {
    if (liveWatcher && liveWatcher.exitCode === null) return resolve({ ok: true, output: 'live watcher running' });
    if (liveRunning) { liveDirty = true; return resolve({ ok: false, error: 'busy' }); }
    liveRunning = true;
    execFile(
      resolvePython(), engineArgs('live'),
      { cwd: QUANT_ROOT, windowsHide: true, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout, stderr) => {
        liveRunning = false;
        if (err) {
          const msg = (stderr || err.message || 'live signal failed').trim();
          console.error('live signal failed:', msg.slice(-1000));
          if (liveDirty) { liveDirty = false; runLiveSignal(); }
          return resolve({ ok: false, error: msg.slice(-4000) });
        }
        sendAll('live:changed', null); // nudge the Live tab to reload
        if (liveDirty) { liveDirty = false; runLiveSignal(); }
        resolve({ ok: true, output: (stdout || '').trim().slice(-4000) });
      }
    );
  });
}

// --------------------------------------------------------------------------- //
// IPC
// --------------------------------------------------------------------------- //
ipcMain.handle('app:config', () => getAppConfig());
ipcMain.handle('db:series', (_e, args) => getSeries(args));
ipcMain.handle('db:reload', async () => { dbMtimeMs = 0; await ensureDb(); return true; });
ipcMain.handle('tables:list', (_e, table) => listDataTable(table));
ipcMain.handle('tables:add', (_e, table, row) => runTableStore(table, 'add', { row }));
ipcMain.handle('tables:update', (_e, table, id, row) => runTableStore(table, 'update', { id, row }));
ipcMain.handle('tables:delete', (_e, table, id) => runTableStore(table, 'delete', { id }));
ipcMain.handle('params:load', () => ({ ok: true, keys: STRATEGY_PARAM_KEYS, params: loadStrategyParams(), meta: STRATEGY_PARAM_META }));
ipcMain.handle('backtest:load', () => loadBacktest());
ipcMain.handle('backtest:run', () => runBacktest());
ipcMain.handle('backtest:exportCsv', () => exportBacktestCsv());
ipcMain.handle('live:exportCsv', () => exportLiveCsv());
ipcMain.handle('params:save', (_e, patch) => saveStrategyParams(patch));
ipcMain.handle('updater:start', () => startUpdater());
ipcMain.handle('updater:stop', () => stopUpdater());
ipcMain.handle('updater:status', () => updaterStatus());
ipcMain.handle('live:loadParams', () => ({ ok: true, keys: STRATEGY_PARAM_KEYS, params: loadLiveParams(), meta: STRATEGY_PARAM_META }));
ipcMain.handle('live:saveParams', async (_e, patch) => {
  const res = saveLiveParams(patch);
  if (res.ok) runLiveSignal();   // params changed -> recompute the signal
  return res;
});
ipcMain.handle('live:load', () => loadLiveSignal());

// --------------------------------------------------------------------------- //
// Window
// --------------------------------------------------------------------------- //
function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0d1117',
    title: 'Quant Trading',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  win.webContents.on('render-process-gone', (_e, details) => {
    console.error('Renderer process gone:', details.reason);
    if (!win.isDestroyed()) win.reload(); // recover from a transient GPU crash
  });
  win.webContents.once('did-finish-load', () => {
    ensureRunning().catch((e) =>
      sendAll('updater:log', { stream: 'sys', line: `Auto-start check failed: ${e.message}` })
    );
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

// Telegram "program offline" ping on close — best-effort, via `quant.py notify` +
// telegram_config.json (a no-op if Telegram isn't configured). Synchronous so it
// actually goes out before the app dies.
let quitPinged = false;
function pingTelegramStopped() {
  if (quitPinged) return;
  quitPinged = true;
  try { spawnSync(resolvePython(), engineArgs('notify', 'stopped'), { cwd: QUANT_ROOT, windowsHide: true, timeout: 6000 }); } catch {}
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  startLogTail();
  watchDb();
  // Ensure the strategy's data-input tables exist in quant.db. Best-effort.
  try { execFile(resolvePython(), engineArgs('table', 'all', 'ensure'), { cwd: QUANT_ROOT, windowsHide: true }, () => {}); } catch {}
  startLiveWatcher();   // computes the current signal now, then on every change
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// The updater is tied to the app's lifetime: stop the one we started on close.
// A manually-started ("external") updater is left alone — we didn't start it.
function stopChildSync() {
  if (!child || child.exitCode !== null) { child = null; return; }
  const pid = child.pid;
  child = null;
  try {
    if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(pid), '/T', '/F']);
    else process.kill(pid, 'SIGTERM');
  } catch {}
}

app.on('window-all-closed', () => {
  stopChildSync();
  stopLiveWatcherSync();
  if (process.platform !== 'darwin') app.quit();
});
app.on('before-quit', () => {
  pingTelegramStopped();   // tell the chat the program is closing (synchronous)
  stopChildSync();
  stopLiveWatcherSync();
});
