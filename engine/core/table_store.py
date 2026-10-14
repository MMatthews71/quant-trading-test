"""Edit a strategy's hand-maintained tables (manifest.json "data_tables").

A data table is a small table in the strategy's quant.db that people edit by
hand (through the strategy's own UI) and its feed reads. The manifest declares
each one, and only declared tables/columns can be written:

    {"table": "<name>", "key": "<key column>",
     "columns": {"<key column>": "text", "<other>": "text" | "real", ...}}

The Electron app reads the DB read-only (sql.js), so it makes every write
through `quant.py table`.
"""
from __future__ import annotations

import sqlite3


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect(spec: dict, db_path) -> sqlite3.Connection:
    """Open the DB, creating the table if it doesn't exist yet."""
    defs = [f"{_q(c)} {'TEXT' if t == 'text' else 'REAL'}"
            + (" PRIMARY KEY" if c == spec["key"] else "")
            for c, t in spec["columns"].items()]
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_q(spec['table'])} ({', '.join(defs)})")
    return conn


def _clean_row(spec: dict, row: dict) -> dict:
    unknown = set(row) - set(spec["columns"])
    if unknown:
        raise ValueError(f"unknown column(s) for {spec['table']}: {sorted(unknown)}")
    out = {}
    for c, v in row.items():
        if v in (None, ""):
            continue
        out[c] = str(v) if spec["columns"][c] == "text" else float(v)
    return out


def add(spec: dict, db_path, row: dict) -> None:
    row = _clean_row(spec, row)
    key = spec["key"]
    if not row.get(key):
        raise ValueError(f"{key} is required")
    with connect(spec, db_path) as conn:
        try:
            conn.execute(f"INSERT INTO {_q(spec['table'])} ({', '.join(map(_q, row))}) "
                         f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
        except sqlite3.IntegrityError:
            raise ValueError(f"a row already exists for {row[key]}")


def update(spec: dict, db_path, key_value: str, row: dict) -> None:
    """Update the row keyed `key_value` (the row may re-key itself)."""
    sets = _clean_row(spec, row)
    if not sets:
        raise ValueError("nothing to update")
    with connect(spec, db_path) as conn:
        cur = conn.execute(
            f"UPDATE {_q(spec['table'])} SET {', '.join(f'{_q(c)} = ?' for c in sets)} "
            f"WHERE {_q(spec['key'])} = ?", [*sets.values(), key_value])
        if cur.rowcount == 0:
            raise ValueError(f"no row found for {key_value}")


def delete(spec: dict, db_path, key_value: str) -> None:
    with connect(spec, db_path) as conn:
        cur = conn.execute(f"DELETE FROM {_q(spec['table'])} WHERE {_q(spec['key'])} = ?",
                           [key_value])
        if cur.rowcount == 0:
            raise ValueError(f"no row found for {key_value}")
