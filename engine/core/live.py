"""Generic live signal: the strategy's current signal off the latest data.

Uses the SAME math as the backtest (core.backtest.prepare) with the strategy's
live parameter set, modelling INSTANT fills (exec lag 0). Writes
the strategy's data/live_signal.json for the app's Live tab.

The latest data is the stored bar table (completed bars) plus
data/live_tail.json — the still-forming bar the updater rewrites every cycle
but only stores once it completes. Two signals come out of that:

  confirmed    — stored (completed) bars ONLY: exactly what the backtest does,
                 never revised. Telegram alerts fire on this.
  provisional  — the same math with the forming bar appended: what the signal
                 would be if the bar closed now. Display-only (never alerted).

They're computed separately because a strategy's features may look at later
bars in the same session, so appending the forming bar can revise earlier rows.
"""
from __future__ import annotations

import os
import sys
import json
import time

import pandas as pd

from . import notify
from .backtest import prepare, evaluate, build_equity, clean
from .data import load_market_data, load_rows_after, normalize, ts_string
from .rules import POSITION

# Ignore a live tail the updater hasn't rewritten for this long (updater stopped
# or the feed is down) — its forming bar would otherwise linger as "current".
TAIL_MAX_AGE = pd.Timedelta(minutes=10)
WATCH_POLL_SECONDS = 0.2


def local_iso(ts, tz):
    """Timestamp -> ISO string in `tz` (None-safe)."""
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    ts = ts.tz_convert(tz) if ts.tzinfo is not None else ts
    return ts.isoformat()


def mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def load_live_tail(strat, after):
    """The forming bar(s) from the updater, newer than `after` (the last stored
    ts), in the engine's frame format. None when missing, stale or unreadable."""
    try:
        tail = json.loads(strat.live_tail_json.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    written = pd.Timestamp(tail.get('written_at'))
    if pd.isna(written) or pd.Timestamp.now(tz='UTC') - written > TAIL_MAX_AGE:
        return None
    rows = pd.DataFrame(tail.get('rows') or [])
    if rows.empty:
        return None
    rows = normalize(strat, rows)
    if after is not None:
        rows = rows[rows[strat.ts_col] > after]
    return rows if not rows.empty else None


def with_tail(history, tail):
    """History + the forming bar(s): a new frame, so `history` is never mutated."""
    if tail is None:
        return history.copy()
    return pd.concat([history, tail.reindex(columns=history.columns)], ignore_index=True)


def _text(v):
    return None if v is None or pd.isna(v) else str(v)


def compute(strat, df, p):
    """Bars (history + forming bar) + live params -> the live payload."""
    df = prepare(strat, df, p, exec_lag_bars=0)   # instant fills

    last = df.iloc[-1]
    label = str(last['entry_exit'])
    position = POSITION.get(label, 0)

    # Trades of this (instant-fill) live signal — same breakdown as the backtest.
    df_bt, _, trades = evaluate(strat, df, bps=p.bps, mask_when_closed=False)
    trades_list = [{k: clean(v) for k, v in t.items()}
                   for t in trades.to_dict(orient='records')]

    signals = {c[len('signal_'):]: clean(last[c]) for c in df.columns if c.startswith('signal_')}
    # Strategy-specific numbers for its own UI (optional hook).
    extra = strat.logic.live_fields(last, p) if hasattr(strat.logic, 'live_fields') else {}

    return {
        'strategy': strat.name,
        'symbol': strat.symbol,
        'signal': {
            'label': label,                                  # Long / Short / Nothing / *Entry / *Exit
            'position': position,                             # +1 / -1 / 0
            'position_text': {1: 'Long', -1: 'Short', 0: 'Flat'}[position],
            'is_event': 'Entry' in label or 'Exit' in label, # a change happened on this bar
            'reason': str(last['entry_exit_reason'] or ''),
        },
        'market': {
            'ts': local_iso(last[strat.ts_col], strat.tz),
            'active_open': bool(last[strat.open_col]) if strat.open_col else True,
            'closed_reason': _text(last.get(strat.closed_reason_col)) if strat.closed_reason_col else None,
            'source': _text(last.get(strat.source_col)) if strat.source_col else None,
            'price': clean(last[strat.price_col]),
            **extra,
        },
        'signals': signals,
        'trades': trades_list,
        # Theoretical equity curve of trading instantly on these signals.
        'equity': build_equity(df_bt, strat.ts_col, strat.tz),
        'params': {k: clean(p[k]) for k in strat.params.names},
        'rows': int(len(df)),
        'generated_at': pd.Timestamp.now(tz=strat.tz).isoformat(),
    }


def combine(confirmed, provisional, forming):
    """The file the app reads: the provisional payload (latest numbers, tiles,
    equity) plus the confirmed signal. `forming` says whether the provisional one
    includes a forming bar at all (if not, the two are the same)."""
    out = dict(provisional)
    out['confirmed'] = {**confirmed['signal'],
                        'ts': confirmed['market']['ts'],
                        'price': confirmed['market']['price']}
    out['provisional'] = bool(forming)
    return out


def alert(strat, confirmed):
    """Telegram buy/sell alert on a CONFIRMED position change (fails soft)."""
    try:
        sent, info = notify.maybe_alert(confirmed, strat)
        if sent:
            print(f"telegram alert: {info}", flush=True)
    except Exception as e:  # pragma: no cover - defensive
        print(f"telegram alert skipped: {e}", flush=True)


def publish(strat, payload):
    """Write live_signal.json, swapped in whole so the app never reads half a file."""
    out_path = strat.live_signal_json
    os.makedirs(out_path.parent, exist_ok=True)
    tmp = out_path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2)
    for attempt in range(5):  # Windows: the app may briefly hold the file open
        try:
            tmp.replace(out_path)
            break
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    c = payload['confirmed']
    line = f"live signal: confirmed {c['label']} @ {c['ts']}"
    if payload['provisional']:
        line += f" | provisional {payload['signal']['label']} @ {payload['market']['ts']}"
    print(line, flush=True)


def run_once(strat):
    p = strat.params.load('live')
    history = load_market_data(strat)
    last = history[strat.ts_col].iloc[-1] if len(history) else None
    tail = load_live_tail(strat, last)
    confirmed = compute(strat, with_tail(history, None), p)
    provisional = compute(strat, with_tail(history, tail), p) if tail is not None else confirmed
    publish(strat, combine(confirmed, provisional, tail is not None))
    alert(strat, confirmed)


class History:
    """The bar table kept in memory; refresh() pulls in only newly appended rows
    (a full reload if the table changed any other way)."""

    def __init__(self, strat):
        self.strat = strat
        self.df = load_market_data(strat)

    @property
    def last_ts(self):
        return self.df[self.strat.ts_col].iloc[-1] if len(self.df) else None

    def refresh(self):
        last = self.last_ts
        after = '' if last is None else ts_string(last, self.strat.tz)
        new, count = load_rows_after(self.strat, after)
        if len(self.df) + len(new) != count:
            self.df = load_market_data(self.strat)      # rows edited/removed: start over
        elif len(new):
            self.df = pd.concat([self.df, new], ignore_index=True)


def watch(strat):
    """Recompute whenever an input changes, as fast as the math allows."""
    history = History(strat)
    print(f"live signal watch [{strat.name}]: {len(history.df)} stored rows loaded", flush=True)
    params_path = strat.params.path('live')
    seen = None
    confirmed = None
    while True:
        # What the signal depends on: the DB, the params, the forming bar, and
        # whether that forming bar has gone stale.
        tail_m = mtime(strat.live_tail_json)
        state = (mtime(strat.db_path), mtime(params_path), tail_m,
                 tail_m is not None and time.time() - tail_m / 1e9 < TAIL_MAX_AGE.total_seconds())
        if state != seen:
            try:
                db_changed = seen is None or state[0] != seen[0]
                if db_changed:
                    history.refresh()
                p = strat.params.load('live')
                # Confirmed: only when the stored bars (or params) change — i.e.
                # once per completed bar — and that's the only thing alerted on.
                new_confirmed = confirmed is None or db_changed or state[1] != seen[1]
                if new_confirmed:
                    confirmed = compute(strat, with_tail(history.df, None), p)
                tail = load_live_tail(strat, history.last_ts)
                provisional = (compute(strat, with_tail(history.df, tail), p)
                               if tail is not None else confirmed)
                publish(strat, combine(confirmed, provisional, tail is not None))
                if new_confirmed:
                    alert(strat, confirmed)
                seen = state
            except Exception as e:  # keep watching across a bad read / DB lock
                print(f"live signal failed: {e!r}", file=sys.stderr, flush=True)
                time.sleep(1)
        time.sleep(WATCH_POLL_SECONDS)
