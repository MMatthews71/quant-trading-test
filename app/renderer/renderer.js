'use strict';

const UP = '#2ec27e';
const DOWN = '#f0616d';
const FLAT = '#4c9aff';

// The active strategy's basics (title, tz label, param groups, its UI files),
// read by main.js at boot. This renderer is strategy-agnostic: everything
// specific to a strategy is drawn by that strategy's own ui/ui.js, which plugs
// in through window.QT (see "Strategy UI plug-in" below).
let CONFIG = { title: '', tzLabel: '', paramGroups: [], ui: {} };

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //
function formatPrice(p) {
  if (p == null) return '—';
  const abs = Math.abs(p);
  const dp = abs >= 1000 ? 1 : abs >= 10 ? 2 : abs >= 1 ? 3 : 4;
  return p.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

// HTML-escape a value for insertion into markup.
const esc = (v) => String(v == null ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function colorFor(meta) {
  if (!meta || meta.change == null) return FLAT;
  return meta.change > 0 ? UP : meta.change < 0 ? DOWN : FLAT;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function formatTime(sec, withTime) {
  const d = new Date(sec * 1000); // wall-as-UTC (see main.js)
  let s = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  if (withTime) {
    const hh = String(d.getUTCHours()).padStart(2, '0');
    const mm = String(d.getUTCMinutes()).padStart(2, '0');
    s += ` ${hh}:${mm}`;
  }
  return s;
}

function setActiveRange(container, range) {
  container.querySelectorAll('button').forEach((b) =>
    b.classList.toggle('active', b.dataset.range === range)
  );
}

// Build a lightweight-charts chart (full interactive). Logo always hidden.
function makeChart(el) {
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: {
      background: { color: 'transparent' },
      textColor: '#8b949e',
      attributionLogo: false,
      fontSize: 12,
    },
    grid: { vertLines: { color: 'rgba(42,51,64,0.5)' }, horzLines: { color: 'rgba(42,51,64,0.5)' } },
    rightPriceScale: { visible: true, borderColor: '#2a3340' },
    // minBarSpacing default is 0.5px/point; with ~2000 downsampled points that
    // needs ~1000px to show everything, so on a narrower window fitContent()
    // clips the oldest data off the left. Lower it so the full history (back to
    // 2022) always fits regardless of window width.
    timeScale: {
      visible: true, borderColor: '#2a3340', timeVisible: true, secondsVisible: false,
      minBarSpacing: 0.05,
    },
    handleScroll: true,
    handleScale: true,
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  const series = chart.addLineSeries({
    color: FLAT,
    lineWidth: 2,
    priceLineVisible: true,
    lastValueVisible: true,
    // The DB stores a row only when the price CHANGES, so hold each price flat
    // until the next recorded change and step there — never slope between them.
    lineType: LightweightCharts.LineType.WithSteps,
  });
  return { chart, series };
}

// --------------------------------------------------------------------------- //
// quant.db changed on disk (the updater wrote new data): reload the snapshot,
// then let the strategy's UI refresh whatever it draws from the DB.
// --------------------------------------------------------------------------- //
const dbListeners = [];
let refreshing = false;
window.api.onDbChanged(async () => {
  if (refreshing) return;
  refreshing = true;
  try {
    await window.api.reloadDb();
    for (const cb of dbListeners) await cb();
  } finally {
    refreshing = false;
  }
});

// --------------------------------------------------------------------------- //
// Tabs
// --------------------------------------------------------------------------- //
// The strategy's sub-tabs: Live / Backtest / Input / Log.
document.getElementById('subtabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.tab');
  if (!btn) return;
  document.querySelectorAll('#subtabs .tab').forEach((b) => b.classList.remove('active'));
  document.querySelectorAll('.tabpane').forEach((p) => p.classList.remove('active'));
  btn.classList.add('active');
  const id = btn.dataset.tab;
  document.getElementById('tab-' + id).classList.add('active');
  if (id === 'backtest') {
    initStrategyParams();
    loadBacktest();
  } else if (id === 'live') {
    initLiveParams();
    loadLive();
    // Re-fit the charts after the pane is shown so they pick up the correct
    // width/height coming back from another tab.
    if (liveEqChart) liveEqChart.timeScale().fitContent();
    if (plugin.live && plugin.live.onShow) plugin.live.onShow();
  } else if (id === 'input') {
    if (plugin.input && plugin.input.onShow) plugin.input.onShow();
  }
});

// --------------------------------------------------------------------------- //
// Strategy inputs (declared in the strategy's manifest) — persisted to its
// backtest_params.json, which the engine reads on its next run.
// Per-input `step`/`min` (from main.js meta) constrain the field — e.g. exec_lag
// only accepts whole 5-minute intervals.
// --------------------------------------------------------------------------- //
let btParamsBuilt = false;
let btParamsTimer = null;

function btParamInputs() {
  return Array.from(document.querySelectorAll('#bt-params [data-param]'));
}

function collectParams() {
  const patch = {};
  for (const el of btParamInputs()) {
    if (el.dataset.kind === 'choice') {
      patch[el.dataset.param] = el.value;                 // string (a <select>)
    } else {
      const v = parseFloat(el.value);
      if (Number.isFinite(v)) patch[el.dataset.param] = v;
    }
  }
  return patch;
}

async function saveStrategyParams() {
  const msg = document.getElementById('bt-params-msg');
  const res = await window.api.saveParams(collectParams());
  if (res && res.ok) {
    const t = new Date().toLocaleTimeString();
    msg.textContent = `saved ${t}`;
    msg.classList.remove('err');
  } else {
    msg.textContent = 'save failed: ' + ((res && res.error) || 'unknown');
    msg.classList.add('err');
  }
}

// The flat key list, split into the labelled sections the strategy's manifest
// declares ("param_groups"). `labels` overrides a field's caption (the section
// title already gives context). Any key not claimed by a section falls through
// into a trailing "Other" section so nothing is ever dropped. Each section lays
// its fields out in `cols` tight label→value pairs per row.
function paramSections(keys) {
  const defined = CONFIG.paramGroups || [];
  const claimed = new Set(defined.flatMap((g) => g.keys));
  const groups = defined
    .map((g) => ({ ...g, keys: g.keys.filter((k) => keys.includes(k)) }))
    .filter((g) => g.keys.length);
  const leftover = keys.filter((k) => !claimed.has(k));
  if (leftover.length) groups.push({ title: 'Other', keys: leftover });
  return groups;
}

// Build one field (label + input) for key `k`, using `meta[k]` for the input's
// constraints. Same markup everywhere; the section grid handles the layout.
function btParamField(k, meta, labelOverride) {
  const m = meta[k] || {};
  const label = labelOverride || m.label || k;
  const title = m.title ? ` title="${m.title}"` : '';
  const cls = 'sp-field';
  let control;
  if (Array.isArray(m.choices)) {
    const opts = m.choices.map((c) => `<option value="${c}">${c}</option>`).join('');
    control = `<select class="sp-in" data-param="${k}" data-kind="choice"${title}>${opts}</select>`;
  } else {
    const step = Number.isFinite(m.step) ? m.step : 'any';
    const min = Number.isFinite(m.min) ? ` min="${m.min}"` : '';
    const max = Number.isFinite(m.max) ? ` max="${m.max}"` : '';
    control = `<input class="sp-in" type="number" step="${step}"${min}${max}${title} data-param="${k}" data-kind="num" />`;
  }
  return `<label class="${cls}"${title}><span class="sp-lbl">${label}</span>${control}</label>`;
}

async function initStrategyParams() {
  const res = await window.api.loadParams();
  if (!res || !res.ok) return;
  const box = document.getElementById('bt-params');
  const meta = res.meta || {};
  if (!btParamsBuilt) {
    const groups = paramSections(res.keys);
    box.innerHTML = groups.map((g) => {
      const cols = Math.min(g.cols || 1, g.keys.length);
      const hint = g.hint ? `<span class="sp-hint">${g.hint}</span>` : '';
      const fields = g.keys
        .map((k) => btParamField(k, meta, g.labels && g.labels[k]))
        .join('');
      return `<div class="sp-section">` +
             `<div class="sp-head">${g.title}${hint}</div>` +
             `<div class="sp-grid" style="--sp-cols:${cols}">${fields}</div></div>`;
    }).join('');
    for (const el of btParamInputs()) {
      // Debounced save while typing.
      el.addEventListener('input', () => {
        clearTimeout(btParamsTimer);
        btParamsTimer = setTimeout(saveStrategyParams, 300);
      });
      // On commit (blur/Enter/spinner), snap to the step (e.g. 5-min intervals),
      // clamp to min, then save immediately so the field shows the stored value.
      el.addEventListener('change', () => {
        const step = parseFloat(el.step);
        let v = parseFloat(el.value);
        if (Number.isFinite(v) && Number.isFinite(step) && step > 0) {
          v = Math.round(v / step) * step;
          const min = parseFloat(el.min);
          if (Number.isFinite(min)) v = Math.max(v, min);
          const max = parseFloat(el.max);
          if (Number.isFinite(max)) v = Math.min(v, max);
          el.value = v;
        }
        clearTimeout(btParamsTimer);
        saveStrategyParams();
      });
    }
    btParamsBuilt = true;
  }
  for (const el of btParamInputs()) {
    const v = res.params[el.dataset.param];
    if (v != null) el.value = v;
  }
}

// "Run backtest": flush any pending input edits, then run the Python script.
// It reads backtest_params.json and writes data/backtest.json; on success
// we reload the results into the tab.
(function wireRunBacktest() {
  const btn = document.getElementById('bt-run');
  const msg = document.getElementById('bt-run-msg');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = 'Running…';
    msg.textContent = '';
    msg.classList.remove('err');
    clearTimeout(btParamsTimer);           // flush debounced param edits first
    await saveStrategyParams();
    const res = await window.api.runBacktest();
    btn.disabled = false;
    btn.textContent = label;
    if (res && res.ok) {
      msg.textContent = 'done ' + new Date().toLocaleTimeString();
      loadBacktest();
    } else {
      msg.textContent = (res && res.error) || 'run failed';
      msg.classList.add('err');
    }
  });
})();

// --------------------------------------------------------------------------- //
// Backtest tab — read-only viewer of data/backtest.json (written by
// `quant.py backtest`). The app doesn't compute anything here; it renders the
// stats, equity curve and trades straight from that file.
// --------------------------------------------------------------------------- //
const btPct = (v, dp = 2) => (v == null || !Number.isFinite(v) ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(dp) + '%');
const btCls = (v) => (v == null || !Number.isFinite(v) ? '' : v > 0 ? 'up' : v < 0 ? 'down' : '');
let btEqChart = null, btEqSeries = null, btLoaded = false;

function btTile(label, value, cls) {
  return `<div class="bt-tile"><span class="bt-tile-v ${cls || ''}">${value}</span>` +
         `<span class="bt-tile-l">${label}</span></div>`;
}

function initBtChart() {
  if (btEqChart) return;
  const c = makeChart(document.getElementById('bt-eq-chart'));
  btEqChart = c.chart;
  btEqSeries = c.series;
  btEqChart.subscribeCrosshairMove((p) => {
    const tip = document.getElementById('bt-eq-tip');
    if (!p.point || !p.time || !p.seriesData || !p.seriesData.get(btEqSeries)) { tip.classList.add('hidden'); return; }
    const v = p.seriesData.get(btEqSeries).value;
    tip.innerHTML = `<b>×${v.toFixed(2)}</b> &nbsp;<span class="tip-rev">${btPct(v - 1)}</span>&nbsp; ${formatTime(p.time, true)}`;
    tip.classList.remove('hidden');
    const wrap = document.querySelector('#tab-backtest .hero-chart').getBoundingClientRect();
    let left = p.point.x + 8;
    if (left + tip.offsetWidth > wrap.width - 8) left = p.point.x - tip.offsetWidth - 8;
    tip.style.left = left + 'px';
    tip.style.top = p.point.y + 'px';
  });
}

function renderBtStats(s) {
  if (!s) { document.getElementById('bt-stats').innerHTML = ''; return; }
  document.getElementById('bt-stats').innerHTML =
    btTile(`Net return (${s.cost_bps ?? ''} bps)`, btPct(s.net_return), btCls(s.net_return)) +
    (s.buy_hold_raw_return != null ? btTile('Buy & hold', btPct(s.buy_hold_raw_return), btCls(s.buy_hold_raw_return)) : '') +
    btTile('CAGR (net)', btPct(s.cagr), btCls(s.cagr)) +
    (s.equity_multiple != null ? btTile('Equity multiple', '×' + s.equity_multiple.toFixed(2), btCls(s.equity_multiple - 1)) : '') +
    (s.sharpe != null ? btTile('Sharpe (net)', Number.isFinite(s.sharpe) ? s.sharpe.toFixed(2) : '—', btCls(s.sharpe)) : '') +
    (s.max_drawdown != null ? btTile('Max drawdown', '−' + (Math.abs(s.max_drawdown) * 100).toFixed(1) + '%', 'down') : '') +
    (s.exposure != null ? btTile('Exposure', (s.exposure * 100).toFixed(1) + '%', '') : '') +
    btTile('Trades', s.n_trades ?? '—', '') +
    (s.win_rate != null ? btTile('Win rate', (s.win_rate * 100).toFixed(1) + '%', '') : '') +
    (s.avg_trade != null ? btTile('Avg / trade', btPct(s.avg_trade), btCls(s.avg_trade)) : '') +
    (s.best != null || s.worst != null ? btTile('Best / worst', btPct(s.best) + ' / ' + btPct(s.worst), '') : '');
}

function renderBtEquity(equity, s) {
  if (!equity || !equity.length) return;
  initBtChart();
  btEqSeries.setData(equity);
  btEqSeries.applyOptions({ color: (s && s.net_return >= 0) ? UP : DOWN });
  btEqChart.timeScale().fitContent();
  const mult = s && s.equity_multiple != null ? s.equity_multiple : equity[equity.length - 1].value;
  document.getElementById('bt-eq-stat').innerHTML = `<span class="hero-price">×${mult.toFixed(2)}</span>`;
  document.getElementById('bt-eq-sub').textContent = s
    ? `net of ${s.cost_bps ?? '?'} bps · ${s.n_trades ?? '?'} trades · ${btPct(s.net_return)}`
    : '';
}

// Shared trades-table renderer (used by both the Backtest and Live tabs).
const TRADES_MAX = 400;
function tradesCountText(trades, max = TRADES_MAX) {
  const n = (trades || []).length;
  return n > max ? `(newest ${max} of ${n})` : `(${n})`;
}
function renderTradesInto(tableId, trades, max = TRADES_MAX) {
  const rows = (trades || []).slice().reverse().slice(0, max);
  let h = '<thead><tr><th>Dir</th><th>Entry</th><th>Exit</th><th>Entry $</th><th>Exit $</th>' +
          '<th>Return</th><th>Hold</th><th>Entry reason</th><th>Exit reason</th></tr></thead><tbody>';
  for (const t of rows) {
    h += `<tr><td class="bt-${(t.dir || '').toLowerCase()}">${t.dir || ''}</td>` +
      `<td>${(t.entryTs || '').slice(0, 16)}</td><td>${(t.exitTs || '').slice(0, 16)}</td>` +
      `<td>${t.entryPx != null ? formatPrice(t.entryPx) : ''}</td>` +
      `<td>${t.exitPx != null ? formatPrice(t.exitPx) : ''}</td>` +
      `<td class="${btCls(t.ret)}">${btPct(t.ret)}</td>` +
      `<td>${t.holdDays != null ? t.holdDays.toFixed(1) + 'd' : ''}</td>` +
      `<td class="bt-reason">${esc(t.entry_reason)}</td>` +
      `<td class="bt-reason">${esc(t.exit_reason)}</td></tr>`;
  }
  document.getElementById(tableId).innerHTML = h + '</tbody>';
}

function renderBtTrades(trades) {
  trades = trades || [];
  document.getElementById('bt-trades-count').textContent = tradesCountText(trades);
  renderTradesInto('bt-table', trades);
}

// "Export full CSV": generate the full per-bar backtest CSV — every bar, every
// column, so every number in the tab can be audited. Main asks for a save
// location, then recomputes and writes the CSV straight there (nothing is left
// in the data folder); this only kicks it off and reports the outcome. The
// recompute means it can take a little while, hence the "Exporting…" state.
(function wireBtExport() {
  const btn = document.getElementById('bt-export');
  const msg = document.getElementById('bt-export-msg');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = 'Exporting…';
    if (msg) { msg.textContent = ''; msg.classList.remove('err'); }
    try {
      const res = await window.api.exportBacktestCsv();
      if (res && res.ok) {
        if (msg) msg.textContent = 'saved';
      } else if (res && res.error === 'canceled') {
        // user dismissed the save dialog — say nothing
      } else if (msg) {
        msg.textContent = (res && res.error) || 'export failed';
        msg.classList.add('err');
      }
    } catch (e) {
      if (msg) { msg.textContent = e.message || 'export failed'; msg.classList.add('err'); }
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
})();

async function loadBacktest() {
  const empty = document.getElementById('bt-empty');
  const results = document.getElementById('bt-results');
  const res = await window.api.loadBacktest();
  if (!res || !res.ok) {
    results.classList.add('hidden');
    empty.hidden = false;
    empty.textContent = res && res.error === 'no-file'
      ? 'No backtest yet — press Run backtest.'
      : 'Could not read backtest.json: ' + ((res && res.error) || 'unknown error');
    return;
  }
  empty.hidden = true;
  results.classList.remove('hidden');
  renderBtStats(res.stats);
  renderBtEquity(res.equity, res.stats);
  renderBtTrades(res.trades);
  btLoaded = true;
}

window.api.onBacktestChanged(() => {
  if (btLoaded || document.getElementById('tab-backtest').classList.contains('active')) loadBacktest();
});

// --------------------------------------------------------------------------- //
// Live tab — current signal off the latest data. Its own parameter set
// (live_params.json); editing it re-runs the live signal (`quant.py live`),
// which writes data/live_signal.json and pushes 'live:changed' back here.
// --------------------------------------------------------------------------- //
let liveParamsBuilt = false;
let liveParamsTimer = null;
let liveLoaded = false;
let liveLastTrades = [];
let liveLastSig = null;   // most recent signal payload, for the staleness re-check timer
const LIVE_TRADES_MAX = 20;   // Live tab shows only the most recent trades (table scrolls)

function liveParamInputs() {
  return Array.from(document.querySelectorAll('#live-params [data-param]'));
}

function collectLiveParams() {
  const patch = {};
  for (const el of liveParamInputs()) {
    if (el.dataset.kind === 'choice') {
      patch[el.dataset.param] = el.value;
    } else {
      const v = parseFloat(el.value);
      if (Number.isFinite(v)) patch[el.dataset.param] = v;
    }
  }
  return patch;
}

async function saveLiveParams() {
  const msg = document.getElementById('live-params-msg');
  const res = await window.api.saveLiveParams(collectLiveParams());
  if (res && res.ok) {
    msg.textContent = 'saved — recomputing…';
    msg.classList.remove('err');
  } else {
    msg.textContent = (res && res.error) || 'save failed';
    msg.classList.add('err');
  }
}

async function initLiveParams() {
  const res = await window.api.loadLiveParams();
  if (!res || !res.ok) return;
  const box = document.getElementById('live-params');
  const meta = res.meta || {};
  if (!liveParamsBuilt) {
    // Same section layout as the backtest params.
    const groups = paramSections(res.keys);
    box.innerHTML = groups.map((g) => {
      const cols = Math.min(g.cols || 1, g.keys.length);
      const fields = g.keys
        .map((k) => btParamField(k, meta, g.labels && g.labels[k]))
        .join('');
      return `<div class="sp-section">` +
             `<div class="sp-head">${g.title}</div>` +
             `<div class="sp-grid" style="--sp-cols:${cols}">${fields}</div></div>`;
    }).join('');

    for (const el of liveParamInputs()) {
      el.addEventListener('input', () => {
        clearTimeout(liveParamsTimer);
        liveParamsTimer = setTimeout(saveLiveParams, 400);
      });
      el.addEventListener('change', () => {
        const step = parseFloat(el.step);
        let v = parseFloat(el.value);
        if (Number.isFinite(v) && Number.isFinite(step) && step > 0) {
          v = Math.round(v / step) * step;
          const min = parseFloat(el.min);
          if (Number.isFinite(min)) v = Math.max(v, min);
          const max = parseFloat(el.max);
          if (Number.isFinite(max)) v = Math.min(v, max);
          el.value = v;
        }
        clearTimeout(liveParamsTimer);
        saveLiveParams();
      });
    }
    liveParamsBuilt = true;
  }
  for (const el of liveParamInputs()) {
    const v = res.params[el.dataset.param];
    if (v != null) el.value = v;
  }
}

// One coloured pill per signal state.
function liveStateClass(position) {
  return position > 0 ? 'live-long' : position < 0 ? 'live-short' : 'live-flat';
}

// --------------------------------------------------------------------------- //
// Live tab rendering. Everything below is purely presentational — it only shows
// the numbers live_signal.json carries; nothing is invented, and a missing
// field renders as “—”. The layout is fixed in index.html; these helpers fill
// the generic regions (hero, open-trade tile, position, equity, trades) and
// hand the strategy's UI its tiles / panels.
// --------------------------------------------------------------------------- //
const lvNum = (v, dp = 2) => (v == null || !Number.isFinite(v) ? '—' : v.toFixed(dp));
// 'pct' values arrive already expressed as a percent (e.g. -4.27).
const lvPct = (v, dp = 2) => (v == null || !Number.isFinite(v) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(dp) + '%');
const lvCls = (v) => (v == null || !Number.isFinite(v) ? '' : v > 0 ? 'up' : v < 0 ? 'down' : '');
const lvTime = (ts) => (ts ? ts.slice(0, 16).replace('T', ' ') : '—');

function lmRow(label, value, cls) {
  return `<div class="live-metric"><span class="lm-lbl">${label}</span>` +
         `<span class="lm-val ${cls || ''}">${value}</span></div>`;
}

// --- Hero: the CONFIRMED signal (last completed bar — what alerts fire on),
// plus the provisional one off the still-forming bar when there is one. ---
function renderLiveHero(sig) {
  const prov = sig.signal || {};
  const conf = sig.confirmed || prov;   // older files have no confirmed block
  const mkt = sig.market || {};
  const cls = liveStateClass(conf.position || 0);

  const card = document.getElementById('live-signal-card');
  if (card) card.className = 'live-hero ' + cls;

  const stateEl = document.getElementById('live-sig-state');
  stateEl.textContent = conf.label || '—';
  stateEl.className = 'live-sig-state ' + cls;

  document.getElementById('live-sig-reason').textContent =
    conf.reason ? `reason: ${conf.reason}` : '';

  const provEl = document.getElementById('live-provisional');
  if (sig.provisional) {
    const closes = lvTime(mkt.ts).slice(11);
    const differs = prov.label !== conf.label;
    const what = mkt.closed_reason === 'awaiting first trade'
      ? `awaiting first ${mkt.source || ''} trade`.replace('  ', ' ')
      : `<b class="${liveStateClass(prov.position || 0)}">${prov.label || '—'}</b>` +
        (prov.reason ? ` (${prov.reason})` : '');
    provEl.innerHTML = `Forming bar → ${closes}: ${what}` +
      (differs ? ' · provisional, confirms when the bar closes' : '');
    provEl.className = 'live-provisional' + (differs ? ' differs' : '');
    provEl.hidden = false;
  } else {
    provEl.hidden = true;
  }

  document.getElementById('live-updated').textContent =
    `Confirmed bar ${lvTime(conf.ts || mkt.ts)} ${CONFIG.tzLabel}` +
    (sig.generated_at ? ` · computed ${sig.generated_at.slice(11, 19)}` : '');

  const pill = document.getElementById('live-market-pill');
  if (pill) {
    const awaiting = mkt.closed_reason === 'awaiting first trade';
    const isOpen = !!mkt.active_open;
    pill.textContent = isOpen ? 'Market open' : awaiting ? 'Opening — no trade yet' : 'Market closed';
    pill.className = 'pill ' + (isOpen ? 'running' : 'idle');
  }
}

// --- "Position & performance": the open trade + a theoretical instant-fill summary. ---
function renderLivePosition(sig) {
  const trades = Array.isArray(sig.trades) ? sig.trades : [];
  const open = trades.find((t) => t.open);
  const closed = trades.filter((t) => !t.open);
  const wins = closed.filter((t) => Number.isFinite(t.ret_net) && t.ret_net > 0).length;
  const winRate = closed.length ? (wins / closed.length) * 100 : null;
  const equityMult = trades.reduce(
    (acc, t) => (Number.isFinite(t.ret_net) ? acc * (1 + t.ret_net) : acc), 1);
  const cumRet = (equityMult - 1) * 100;

  let html = `<div class="live-metrics">`;
  if (open) {
    html +=
      lmRow('Direction', (open.dir || '').replace(/^./, (c) => c.toUpperCase()) || '—',
        open.dir === 'long' ? 'up' : open.dir === 'short' ? 'down' : '') +
      lmRow('Entry date', lvTime(open.entryTs)) +
      lmRow('Entry → current px', lvNum(open.entryPx) + ' → ' + lvNum(open.exitPx)) +
      lmRow('Unrealised (net)', lvPct(Number.isFinite(open.ret_net) ? open.ret_net * 100 : null),
        lvCls(open.ret_net)) +
      lmRow('Holding', open.holdDays != null ? open.holdDays.toFixed(1) + ' d' : '—');
  } else {
    html += lmRow('Position', 'Flat — no open trade', 'dim');
  }
  html +=
    `<div class="live-metric-gap"></div>` +
    lmRow('Trades (all / closed)', trades.length + ' / ' + closed.length) +
    lmRow('Win rate (closed)', winRate == null ? '—' : winRate.toFixed(1) + '%') +
    lmRow('Cumulative (net)', '×' + equityMult.toFixed(2) + '  (' + lvPct(cumRet) + ')', lvCls(cumRet)) +
    `</div>`;
  document.getElementById('live-position').innerHTML = html;
}

// Warn when the data feed has gone quiet. The most direct measure is the age of
// the latest bar: if the market is open but no new bar has arrived in a while,
// the updater/API isn't pushing data. (A lagging bar while the market is CLOSED
// is normal, so we don't warn then.) Re-checked on a timer so the banner appears
// even when nothing new arrives — precisely the case we're trying to catch.
const LIVE_STALE_MIN = 20;
function fmtAge(min) {
  if (min < 60) return `${Math.round(min)} min`;
  const h = Math.floor(min / 60);
  return `${h}h ${Math.round(min - h * 60)}m`;
}
function renderLiveStale(sig) {
  const el = document.getElementById('live-stale');
  if (!el) return;
  const mkt = (sig && sig.market) || {};
  const t = mkt.ts ? Date.parse(mkt.ts) : NaN;
  const ageMin = Number.isFinite(t) ? (Date.now() - t) / 60000 : Infinity;
  if (mkt.active_open && ageMin > LIVE_STALE_MIN) {
    el.hidden = false;
    el.textContent = `⚠ Data feed is stale — last update was ${fmtAge(ageMin)} ago.`;
  } else {
    el.hidden = true;
  }
}

// --- Strategy equity curve (mirrors the backtest equity chart, live data). ---
let liveEqChart = null, liveEqSeries = null;
function initLiveEqChart() {
  if (liveEqChart) return;
  const c = makeChart(document.getElementById('live-eq-chart'));
  liveEqChart = c.chart;
  liveEqSeries = c.series;
  liveEqChart.subscribeCrosshairMove((p) => {
    const tip = document.getElementById('live-eq-tip');
    if (!p.point || !p.time || !p.seriesData || !p.seriesData.get(liveEqSeries)) { tip.classList.add('hidden'); return; }
    const v = p.seriesData.get(liveEqSeries).value;
    tip.innerHTML = `<b>×${v.toFixed(2)}</b> &nbsp;<span class="tip-rev">${btPct(v - 1)}</span>&nbsp; ${formatTime(p.time, true)}`;
    tip.classList.remove('hidden');
    const wrap = document.getElementById('live-eq-chart').parentElement.getBoundingClientRect();
    let left = p.point.x + 8;
    if (left + tip.offsetWidth > wrap.width - 8) left = p.point.x - tip.offsetWidth - 8;
    tip.style.left = left + 'px';
    tip.style.top = p.point.y + 'px';
  });
}

function renderLiveEquity(sig) {
  const card = document.getElementById('live-eq-card');
  const equity = Array.isArray(sig.equity) ? sig.equity : [];
  if (!equity.length) { if (card) card.classList.add('hidden'); return; }
  if (card) card.classList.remove('hidden');
  initLiveEqChart();
  liveEqSeries.setData(equity);
  const net = equity[equity.length - 1].value - 1;
  liveEqSeries.applyOptions({ color: net >= 0 ? UP : DOWN });
  liveEqChart.timeScale().fitContent();
  document.getElementById('live-eq-stat').innerHTML = `<span class="hero-price">×${(1 + net).toFixed(2)}</span>`;
  const nTrades = (sig.trades || []).length;
  const bps = sig.params && sig.params.bps != null ? sig.params.bps : '?';
  document.getElementById('live-eq-sub').textContent =
    `net of ${bps} bps · ${nTrades} trades · ${btPct(net)}`;
}

function renderLive(sig) {
  liveLastSig = sig;
  renderLiveHero(sig);
  renderLivePosition(sig);
  renderLiveEquity(sig);
  renderLiveStale(sig);

  // --- previous trades (show only the last 20; the table scrolls) ---
  liveLastTrades = Array.isArray(sig.trades) ? sig.trades : [];
  document.getElementById('live-trades-count').textContent = tradesCountText(liveLastTrades, LIVE_TRADES_MAX);
  renderTradesInto('live-table', liveLastTrades, LIVE_TRADES_MAX);

  // The strategy's own live views get the same payload.
  if (plugin.live && plugin.live.onLive) plugin.live.onLive(sig);
}

// Re-evaluate staleness every minute against the last payload, so the warning
// surfaces when data STOPS arriving (no live:changed would fire in that case).
setInterval(() => { if (liveLastSig) renderLiveStale(liveLastSig); }, 60000);

// Full per-bar CSV of the live recompute — same flow as the backtest's
// "Export full CSV" (recomputes in Python and writes straight to a chosen path).
(function wireLiveExport() {
  const btn = document.getElementById('live-export');
  const msg = document.getElementById('live-export-msg');
  if (!btn) return;
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();   // the button lives in a <summary>; don't toggle the section
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = 'Exporting…';
    if (msg) { msg.textContent = ''; msg.classList.remove('err'); }
    try {
      const res = await window.api.exportLiveCsv();
      if (res && res.ok) {
        if (msg) msg.textContent = 'saved';
      } else if (res && res.error === 'canceled') {
        // user dismissed the save dialog — say nothing
      } else if (msg) {
        msg.textContent = (res && res.error) || 'export failed';
        msg.classList.add('err');
      }
    } catch (e) {
      if (msg) { msg.textContent = e.message || 'export failed'; msg.classList.add('err'); }
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
})();

async function loadLive() {
  const empty = document.getElementById('live-empty');
  const results = document.getElementById('live-results');
  const res = await window.api.loadLive();
  if (!res || !res.ok) {
    results.classList.add('hidden');
    empty.hidden = false;
    empty.textContent = res && res.error === 'no-file'
      ? 'No live signal yet — it computes automatically when data updates.'
      : 'Could not read live_signal.json: ' + ((res && res.error) || 'unknown error');
    return;
  }
  empty.hidden = true;
  results.classList.remove('hidden');
  renderLive(res);
  liveLoaded = true;
  const msg = document.getElementById('live-params-msg');
  if (msg) msg.textContent = '';
}

window.api.onLiveChanged(() => {
  if (liveLoaded || document.getElementById('tab-live').classList.contains('active')) loadLive();
});

// A chart in a collapsed <details> has zero size; re-fit it when the section is
// expanded so it draws at the right dimensions.
document.getElementById('live-eq-details').addEventListener('toggle', (e) => {
  if (e.target.open && liveEqChart) requestAnimationFrame(() => liveEqChart.timeScale().fitContent());
});

// --------------------------------------------------------------------------- //
// Log tab
// --------------------------------------------------------------------------- //
const logEl = document.getElementById('log');
const startBtn = document.getElementById('start');
const stopBtn = document.getElementById('stop');
const stateEl = document.getElementById('updater-state');

function appendLog(data) {
  const div = document.createElement('div');
  if (data.stream === 'err') div.className = 'err';
  else if (data.stream === 'sys') div.className = 'sys';
  div.textContent = data.line;
  logEl.appendChild(div);
  if (document.getElementById('autoscroll').checked) logEl.scrollTop = logEl.scrollHeight;
}

function setUpdaterState(status) {
  const running = status && status.running;
  startBtn.disabled = running;
  stopBtn.disabled = !running;
  let label = 'stopped';
  if (running) label = status.source === 'external' ? 'running · external' : 'running';
  stateEl.textContent = label;
  stateEl.className = 'pill ' + (running ? 'running' : 'idle');
}

startBtn.addEventListener('click', async () => setUpdaterState(await window.api.startUpdater()));
stopBtn.addEventListener('click', async () => { await window.api.stopUpdater(); });
document.getElementById('clearlog').addEventListener('click', () => { logEl.innerHTML = ''; });

window.api.onUpdaterLog(appendLog);
window.api.onUpdaterStatus(setUpdaterState);

// --------------------------------------------------------------------------- //
// Strategy UI plug-in. The whole window is the strategy's tab (labelled with
// its manifest "symbol"); its sub-tabs Live / Backtest / Log are generic. The
// strategy's engine/strategies/<name>/ui/ui.js (+ ui.css) adds its own parts
// by calling QT.register({...}) with either or both of:
//   live:  { mount(el), onShow(), onLive(sig) }   its views on the Live
//          sub-tab, under the signal card (onLive: a new live payload —
//          signal, market, signals, params, trades, ...)
//   input: { mount(el), onShow() }   the Input sub-tab (hand-maintained
//          inputs); the sub-tab only appears when a strategy provides it
// and uses QT.api / QT.ui / QT.onDbChanged for data access and shared widgets.
// --------------------------------------------------------------------------- //
const plugin = {};

window.QT = {
  config: CONFIG,
  api: {
    // One column of the strategy's bar table as a chart series:
    // {column, range: '1D'|'1W'|'1M'|'3M'|'1Y'|'ALL', maxPoints, scale}
    getSeries: (args) => window.api.getSeries(args),
    listRows: (table) => window.api.listRows(table),         // a manifest data_tables table
    addRow: (table, row) => window.api.addRow(table, row),
    updateRow: (table, id, row) => window.api.updateRow(table, id, row),
    deleteRow: (table, id) => window.api.deleteRow(table, id),
    reloadDb: () => window.api.reloadDb(),
  },
  ui: {
    colors: { UP, DOWN, FLAT },
    esc, makeChart, formatPrice, formatTime, colorFor, setActiveRange,
    tile: btTile, metricRow: lmRow,
    num: lvNum, pct: lvPct, time: lvTime, tone: lvCls,
  },
  onDbChanged: (cb) => dbListeners.push(cb),
  register(p) { Object.assign(plugin, p); },
};

function loadAsset(tag, attrs) {
  return new Promise((resolve, reject) => {
    const el = Object.assign(document.createElement(tag), attrs);
    el.onload = resolve;
    el.onerror = () => reject(new Error(`could not load ${attrs.src || attrs.href}`));
    document.head.appendChild(el);
  });
}

// Let the strategy's UI build its parts.
function mountStrategyUi() {
  if (plugin.live) plugin.live.mount(document.getElementById('live-strategy'));
  if (plugin.input) {
    plugin.input.mount(document.getElementById('input-root'));
    document.querySelector('#subtabs .tab[data-tab="input"]').hidden = false;
  }
}

// --------------------------------------------------------------------------- //
// Boot
// --------------------------------------------------------------------------- //
(async function boot() {
  CONFIG = await window.api.getConfig();
  window.QT.config = CONFIG;
  document.getElementById('strategy-tab').textContent = CONFIG.symbol || CONFIG.title;
  // The strategy's own parts, if it ships a UI.
  try {
    if (CONFIG.ui.style) await loadAsset('link', { rel: 'stylesheet', href: CONFIG.ui.style });
    if (CONFIG.ui.script) {
      await loadAsset('script', { src: CONFIG.ui.script });
      mountStrategyUi();
    }
  } catch (e) {
    appendLog({ stream: 'err', line: `Strategy UI failed to load: ${e.message}` });
  }
  // Live is the default sub-tab, so populate it on startup (this also hands
  // the strategy's live views their first payload), then let them draw.
  initLiveParams();
  await loadLive();
  if (plugin.live && plugin.live.onShow) plugin.live.onShow();
  setUpdaterState(await window.api.getUpdaterStatus());
})();
