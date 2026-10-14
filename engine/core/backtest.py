"""Generic backtest: strategy signals -> labels -> P&L, stats, trades, equity.

    prepare(strat, df, p)   strategy features + rules -> entry_exit labels
    evaluate(strat, df)     labelled frame -> (per-bar frame, stats, trades)
    run(strat, p)           load the bar table, prepare, evaluate (and write
                            data/backtest.json for the app's Backtest tab)

Nothing here is instrument-specific: the price / timestamp / open columns,
display timezone and bar size all come from the strategy's manifest.
"""
from __future__ import annotations

import os
import json

import numpy as np
import pandas as pd

from . import rules as rules_mod
from .data import load_market_data


# --------------------------------------------------------------------------- #
# Signal -> labels
# --------------------------------------------------------------------------- #
def active_series(strat, df) -> pd.Series:
    if strat.open_col and strat.open_col in df.columns:
        return df[strat.open_col].astype(bool)
    return pd.Series(True, index=df.index)


def prepare(strat, df, p, exec_lag_bars=None):
    """Strategy features + entry/exit rules -> entry_exit labels/reasons.

    exec_lag_bars overrides the execution lag (in BARS); when None it uses the
    param's exec_lag (MINUTES) converted to bars. Pass 0 for instant fills
    (act on the bar the signal fires).
    """
    df, signals = strat.logic.features(df, p)
    for name, values in signals.items():   # signal_<name> columns: live payload + CSV
        df[f'signal_{name}'] = values
    if exec_lag_bars is None:
        exec_lag_bars = int(p.exec_lag) // strat.bar_minutes
    r = strat.logic.rules(df, signals, p)
    return rules_mod.apply(df, r, active_series(strat, df), exec_lag_bars)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def build_equity(df, ts_col, tz, equity_points=4000):
    """Downsampled net/gross equity curve from a frame with cum_net/cum_gross.

    Times are unix SECONDS of the local wall clock in `tz`, labelled as if UTC
    (the app's charts render numeric time as UTC, so this shows local time on
    the axis — see wallSeconds in app/main.js). Casting to datetime64[s] first
    makes this epoch-SECONDS regardless of datetime precision.
    """
    n = len(df)
    ts = pd.to_datetime(df[ts_col]) if ts_col in df.columns else df.index.to_series()
    eq_net = (1.0 + df['cum_net']).to_numpy()
    eq_gross = (1.0 + df['cum_gross']).to_numpy()
    if getattr(ts.dt, 'tz', None) is not None:
        wall = ts.dt.tz_convert(tz).dt.tz_localize(None)
    else:
        wall = ts
    t_sec = wall.to_numpy().astype('datetime64[s]').astype('int64')

    if equity_points and n > equity_points:
        idx = np.unique(np.linspace(0, n - 1, equity_points).round().astype(int))
    else:
        idx = np.arange(n)

    return [
        {'time': int(t_sec[k]), 'value': float(eq_net[k]), 'gross': float(eq_gross[k])}
        for k in idx
    ]


def clean(v):
    """Make numpy/pandas scalars JSON-serialisable (NaN -> None)."""
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, float) and np.isnan(v):
        return None
    return v


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def evaluate(strat, df, bps=4.0, mask_when_closed=False, ann_on_active_only=False,
             equity_points=4000, write=False, out_path=None):
    """
    General strategy evaluator driven by entry_exit labels.

    bps                : cost per unit of turnover, in basis points.
    mask_when_closed   : if True, zero out price moves on closed bars.
    ann_on_active_only : if True, annualize vol/Sharpe on active bars only.
    equity_points      : max points in the exported equity curve (downsampled).
    write / out_path   : write the JSON summary the app reads (default: the
                         strategy's data/backtest.json). The full per-bar CSV is produced
                         on demand by `quant.py export` instead.
    Returns (df_with_cols, stats_Series, trades_DataFrame).
    """
    price_col, ts_col = strat.price_col, strat.ts_col
    signal_col, reason_col = 'entry_exit', 'entry_exit_reason'
    df = df.copy()

    position = df[signal_col].astype(str).map(rules_mod.POSITION)
    unknown = df.loc[position.isna(), signal_col].unique()
    if len(unknown):
        raise ValueError(f"Unmapped labels: {list(unknown)}")
    position = position.astype(float)

    price = df[price_col].astype(float)
    price_ret = price.pct_change().fillna(0.0)
    active = active_series(strat, df)
    if mask_when_closed:
        price_ret = price_ret.where(active, 0.0)

    # --- per-bar P&L of a HELD position (no daily rebalancing) ---
    # Each position is put on at its entry price and marked to market against
    # THAT entry price for the whole life of the trade, instead of re-applying
    # the day's percentage move to a compounding base. For a long the two agree
    # exactly (the daily chain telescopes back to exit/entry), but for a short
    # they don't: chaining daily returns implicitly assumes you resize the short
    # every bar, whereas a position you actually hold earns (entry - exit)/entry.
    # Building the bar returns from the held mark-to-market makes every number
    # downstream — equity curve, totals, Sharpe, drawdown — a buy-and-hold-per-
    # trade result, matching how the book is really traded.
    if mask_when_closed:
        # freeze the price while the market is closed so no P&L accrues on bars
        # that could not be traded; it resumes from the next open price.
        px_eff = price.where(active).ffill().bfill()
    else:
        px_eff = price
    px_np = px_eff.to_numpy()
    pos_seg = position.to_numpy()
    n_bars = len(df)
    ret_gross_arr = np.zeros(n_bars)
    i = 0
    while i < n_bars:
        if pos_seg[i] == 0:
            i += 1
            continue
        d = pos_seg[i]
        entry = i
        j = i
        while j < n_bars and pos_seg[j] == d:
            j += 1
        exit_bar = j if j < n_bars else n_bars - 1
        p_entry = px_np[entry]
        # mark-to-market vs the entry price over the life of the trade; its
        # bar-over-bar change is the held position's daily return. Booked only
        # from the bar after entry (the entry bar opens the position, no P&L yet).
        mtm = 1.0 + d * (px_np[entry:exit_bar + 1] / p_entry - 1.0)
        ret_gross_arr[entry + 1:exit_bar + 1] = mtm[1:] / mtm[:-1] - 1.0
        i = j

    ret_gross = pd.Series(ret_gross_arr, index=df.index)
    turnover = position.diff().abs().fillna(position.abs())
    cost = turnover * (bps / 1e4)
    ret_net = ret_gross - cost

    df['position'] = position
    df['ret_gross'] = ret_gross
    df['cost'] = cost
    df['ret_net'] = ret_net
    df['cum_gross'] = (1 + ret_gross).cumprod() - 1
    df['cum_net'] = (1 + ret_net).cumprod() - 1

    ts = pd.to_datetime(df[ts_col]) if ts_col in df.columns else df.index.to_series()
    years = (ts.iloc[-1] - ts.iloc[0]).total_seconds() / (365.25 * 24 * 3600) if len(df) > 1 else np.nan
    # Timestamps emitted for display (trade start/end etc.) are in the
    # strategy's local timezone, matching the app's charts.
    ts_disp = ts.dt.tz_convert(strat.tz) if getattr(ts.dt, 'tz', None) is not None else ts

    if ann_on_active_only:
        r_for_vol = ret_net[active.to_numpy()]
        active_years = active.sum() / len(df) * years if years and years > 0 else np.nan
        ppy = active.sum() / active_years if active_years and active_years > 0 else np.nan
    else:
        r_for_vol = ret_net
        ppy = len(df) / years if years and years > 0 else np.nan

    def sharpe(r):
        sd = r.std()
        return (r.mean() / sd) * np.sqrt(ppy) if sd and sd > 0 and np.isfinite(ppy) else np.nan

    def max_dd(cum):
        eq = 1 + cum
        return (eq / eq.cummax() - 1).min()

    total_net = df['cum_net'].iloc[-1]
    total_gross = df['cum_gross'].iloc[-1]

    # --- per-trade breakdown: flip-safe, open-position aware ---
    pos_np = position.to_numpy()
    rg, rn = ret_gross.to_numpy(), ret_net.to_numpy()
    price_np = df[price_col].astype(float).to_numpy()
    reasons = (df[reason_col].astype(str).to_numpy()
               if reason_col in df.columns else np.full(len(df), ''))
    n = len(df)
    trades = []
    i = 0
    while i < n:
        if pos_np[i] == 0:
            i += 1
            continue
        d = pos_np[i]
        entry, j = i, i
        while j < n and pos_np[j] == d:
            j += 1
        open_trade = j >= n
        exit_bar = j if j < n else n - 1
        seg = slice(entry, exit_bar + 1)
        # Per-trade P&L compounded from the held mark-to-market bar returns built
        # above, so each trade reconciles exactly with the headline totals.
        ret_gross_trade = float(np.prod(1.0 + rg[seg]) - 1.0)
        ret_net_trade = float(np.prod(1.0 + rn[seg]) - 1.0)
        entry_px_t = price_np[entry]
        exit_px_t = price_np[exit_bar]
        hold_days = (ts.iloc[exit_bar] - ts.iloc[entry]).total_seconds() / 86400.0
        trades.append({
            'dir':          'long' if d > 0 else 'short',
            'start':        ts_disp.iloc[entry],
            'end':          ts_disp.iloc[exit_bar],
            'bars':         exit_bar - entry + 1,
            'ret_gross':    ret_gross_trade,
            'ret_net':      ret_net_trade,
            'open':         open_trade,
            # entry reason is on the entry bar; exit reason on the exit bar
            # (blank while the trade is still open).
            'entry_reason': reasons[entry],
            'exit_reason':  '' if open_trade else reasons[exit_bar],
            # --- app-viewer aliases (renderer reads these field names) ---
            'entryTs':      ts_disp.iloc[entry],
            'exitTs':       ts_disp.iloc[exit_bar],
            'entryPx':      entry_px_t,
            'exitPx':       exit_px_t,
            'ret':          ret_net_trade,
            'holdDays':     hold_days,
            'reason':       reasons[entry],
            'exitReason':   '' if open_trade else reasons[exit_bar],
        })
        i = j
    trades = pd.DataFrame(trades)

    n_trades = len(trades)
    closed = trades[~trades['open']] if n_trades else trades
    win_rate = (closed['ret_net'] > 0).mean() if len(closed) else np.nan
    avg_trade = closed['ret_net'].mean() if len(closed) else np.nan
    best_trade = closed['ret_net'].max() if len(closed) else np.nan
    worst_trade = closed['ret_net'].min() if len(closed) else np.nan

    # Buy & hold benchmark, in two flavours:
    #   buy_hold     — like-for-like: always long under the SAME tradeable-bar
    #                  assumption as the strategy (price_ret is masked on closed
    #                  bars when mask_when_closed).
    #   buy_hold_raw — the naive first->last price change. Reference only.
    buy_hold = float((1.0 + price_ret).prod() - 1.0) if len(df) > 1 else np.nan
    px = df[price_col].astype(float).dropna()
    buy_hold_raw = px.iloc[-1] / px.iloc[0] - 1.0 if len(px) > 1 else np.nan
    cagr_net = (1 + total_net) ** (1 / years) - 1 if years and years > 0 else np.nan
    exposure = (position != 0).mean()

    stats = pd.Series({
        'bps': bps,
        'total_return_net': total_net,
        'total_return_gross': total_gross,
        'cost_drag': total_gross - total_net,
        'total_cost': df['cost'].sum(),
        'cagr_net': cagr_net,
        'ann_vol_net': r_for_vol.std() * np.sqrt(ppy) if np.isfinite(ppy) else np.nan,
        'sharpe_net': sharpe(ret_net if not ann_on_active_only else r_for_vol),
        'sharpe_gross': sharpe(ret_gross),
        'max_drawdown_net': max_dd(df['cum_net']),
        'n_trades': n_trades,
        'n_closed_trades': int((~trades['open']).sum()) if n_trades else 0,
        'open_at_end': bool(trades['open'].iloc[-1]) if n_trades else False,
        'win_rate': win_rate,
        'avg_trade_ret': avg_trade,
        'time_in_market': exposure,
        'years': years,
        # --- app-viewer aliases (renderer / BT_STAT_KEYS read these names) ---
        'cost_bps': bps,
        'net_return': total_net,
        'gross_return': total_gross,
        'buy_hold_return': buy_hold,
        'buy_hold_raw_return': buy_hold_raw,
        'vs_buy_hold': total_net - buy_hold if np.isfinite(buy_hold) else np.nan,
        'cagr': cagr_net,
        'equity_multiple': 1.0 + total_net,
        'sharpe': sharpe(ret_net if not ann_on_active_only else r_for_vol),
        'max_drawdown': max_dd(df['cum_net']),
        'exposure': exposure,
        'avg_trade': avg_trade,
        'best': best_trade,
        'worst': worst_trade,
        'best_trade': best_trade,
        'worst_trade': worst_trade,
    })

    if write:
        payload = {
            'strategy': strat.name,
            'params': {'bps': bps, 'mask_when_closed': mask_when_closed,
                       'ann_on_active_only': ann_on_active_only},
            'stats': {k: clean(v) for k, v in stats.items()},
            'trades': [{k: clean(v) for k, v in t.items()}
                       for t in trades.to_dict(orient='records')],
            'equity': build_equity(df, ts_col, strat.tz, equity_points),
            'generated_at': pd.Timestamp.now(tz=strat.tz).isoformat(),
        }
        out_path = out_path or strat.backtest_json
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'w') as f:
            json.dump(payload, f, indent=2)
        stats['json_path'] = str(out_path)

    return df, stats, trades


def run(strat, p, write=True, exec_lag_bars=None, df=None):
    """Full backtest of `strat` with params `p` over the stored bar table."""
    if df is None:
        df = load_market_data(strat)
    df = prepare(strat, df, p, exec_lag_bars)
    return evaluate(strat, df, bps=p.bps, mask_when_closed=False, write=write)
