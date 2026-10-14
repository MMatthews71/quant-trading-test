"""Read a strategy's bar table out of quant.db.

Stored format (what the updater writes): the timestamp column is a local
wall-clock ISO string in the strategy's timezone ("YYYY-MM-DD HH:MM:SS+HH:MM"),
and the open flag is 0/1. In memory the engine
works on tz-aware UTC timestamps and a bool open flag.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

TS_FMT = "%Y-%m-%d %H:%M:%S"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_market_data(strat, db_path=None) -> pd.DataFrame:
    with sqlite3.connect(db_path or strat.db_path, timeout=30) as con:
        df = pd.read_sql_query(
            f"SELECT * FROM {_q(strat.table)} ORDER BY {_q(strat.ts_col)}", con)
    return normalize(strat, df)


def load_rows_after(strat, after: str, db_path=None) -> tuple[pd.DataFrame, int]:
    """Rows stored after the timestamp string `after`, plus the table's row count."""
    with sqlite3.connect(db_path or strat.db_path, timeout=30) as con:
        count = con.execute(f"SELECT COUNT(*) FROM {_q(strat.table)}").fetchone()[0]
        new = pd.read_sql_query(
            f"SELECT * FROM {_q(strat.table)} WHERE {_q(strat.ts_col)} > ? "
            f"ORDER BY {_q(strat.ts_col)}", con, params=(after,))
    return normalize(strat, new), count


def normalize(strat, df: pd.DataFrame) -> pd.DataFrame:
    """Stored rows -> the frame the strategy works on (UTC ts, bool open flag)."""
    df = df.copy()
    ts = strat.ts_col
    naive = pd.to_datetime(df[ts].str.slice(0, 19), format=TS_FMT)
    df[ts] = naive.dt.tz_localize(strat.tz).dt.tz_convert("UTC")
    if "row_id" in df.columns:
        df = df.drop(columns=["row_id"])
    if strat.open_col and strat.open_col in df.columns:
        df[strat.open_col] = df[strat.open_col].astype(bool)
    return df


def ts_strings(s: pd.Series, tz: str) -> pd.Series:
    """tz-aware timestamps -> the stored string form in `tz`
    ("YYYY-MM-DD HH:MM:SS+HH:MM"). For a fixed-offset zone these sort
    lexicographically in time order, which the updater relies on."""
    local = s.dt.tz_convert(tz)
    return (local.dt.strftime(TS_FMT + "%z")
            .str.replace(r"([+-]\d\d)(\d\d)$", r"\1:\2", regex=True))


def ts_string(ts: pd.Timestamp, tz: str) -> str:
    return ts_strings(pd.Series([ts]), tz).iloc[0]
