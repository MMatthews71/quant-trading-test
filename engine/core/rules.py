"""The generic entry/exit state machine.

A strategy describes WHEN to trade as named boolean conditions (one value per
bar) and this module turns them into position labels, the same way for every
strategy:

    long entry   ALL of `long_entry` conditions true   (flat -> long)
    long exit    ANY of `long_exit` conditions true    (long -> flat)
    short entry  ALL of `short_entry` conditions true  (flat -> short)
    short exit   ANY of `short_exit` conditions true   (short -> flat)

Bars where the market is closed (`active`) or the source bar's signal is
invalid (`valid`) just carry the current position. An execution lag of N bars
makes a condition seen at bar t act at bar t+N.

Labels written to df['entry_exit']: Nothing, Long Entry, Long, Long Exit,
Short Entry, Short, Short Exit. The reason ('entry_exit_reason') names the
condition(s) that fired.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

Conditions = list  # [(name: str, values: bool ndarray / Series), ...]

POSITION = {
    'Nothing': 0,
    'Long Entry': 1, 'Long': 1, 'Long Exit': 0,
    'Short Entry': -1, 'Short': -1, 'Short Exit': 0,
}


@dataclass
class Rules:
    long_entry: Conditions
    long_exit: Conditions
    short_entry: Conditions
    short_exit: Conditions
    valid: object                 # bool per bar: is the signal defined here?
    entry_prefix: str = ''        # prepended to entry reasons, e.g. 'both: '


def _arr(v) -> np.ndarray:
    return np.asarray(getattr(v, 'to_numpy', lambda: v)(), dtype=bool)


def _shift(arr, d):
    if d <= 0:
        return arr
    out = np.zeros_like(arr, dtype=bool)
    if d < len(arr):
        out[d:] = arr[:-d]
    return out


def apply(df, rules: Rules, active, exec_delay: int = 0):
    """Label every bar of `df` (adds entry_exit / entry_exit_reason)."""
    exec_delay = int(exec_delay)
    d = exec_delay

    def group(conds):
        return [(name, _shift(_arr(v), d)) for name, v in conds]

    le, lx = group(rules.long_entry), group(rules.long_exit)
    se, sx = group(rules.short_entry), group(rules.short_exit)
    long_entry_sig = np.logical_and.reduce([v for _, v in le])
    short_entry_sig = np.logical_and.reduce([v for _, v in se])
    long_exit_sig = np.logical_or.reduce([v for _, v in lx])
    short_exit_sig = np.logical_or.reduce([v for _, v in sx])
    active = _arr(active)
    valid_at_signal = _shift(_arr(rules.valid), d)   # was the source bar valid?

    tag = f' (+{exec_delay}b)' if exec_delay else ''
    long_reason = rules.entry_prefix + ' & '.join(n for n, _ in le) + tag
    short_reason = rules.entry_prefix + ' & '.join(n for n, _ in se) + tag

    n = len(df)
    labels = np.empty(n, dtype=object)
    reasons = np.empty(n, dtype=object)
    hold_label = {'flat': 'Nothing', 'long': 'Long', 'short': 'Short'}
    state = 'flat'

    for i in range(n):
        # must be able to fill at execution time...
        if not active[i]:
            labels[i] = hold_label[state]
            reasons[i] = 'inactive'
            continue
        # ...and the signal executing here must have come from a valid bar
        if not valid_at_signal[i]:
            labels[i] = hold_label[state]
            reasons[i] = 'nan_signal'
            continue

        if state == 'flat':
            if long_entry_sig[i]:
                labels[i], reasons[i], state = 'Long Entry', long_reason, 'long'
            elif short_entry_sig[i]:
                labels[i], reasons[i], state = 'Short Entry', short_reason, 'short'
            else:
                labels[i], reasons[i] = 'Nothing', ''
        elif state == 'long':
            if long_exit_sig[i]:
                labels[i] = 'Long Exit'
                reasons[i] = ' & '.join(nm for nm, v in lx if v[i]) + tag
                state = 'flat'
            else:
                labels[i], reasons[i] = 'Long', ''
        else:  # short
            if short_exit_sig[i]:
                labels[i] = 'Short Exit'
                reasons[i] = ' & '.join(nm for nm, v in sx if v[i]) + tag
                state = 'flat'
            else:
                labels[i], reasons[i] = 'Short', ''

    df['entry_exit'] = labels
    df['entry_exit_reason'] = reasons
    return df
