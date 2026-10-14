"""Find and load a strategy plug-in.

A strategy is a folder ``engine/strategies/<name>/`` holding:

    manifest.json   what the generic engine and the app need to know: the bar
                    table and its key columns, timezone, bar size, the tunable
                    params and any hand-maintained data tables
    strategy.py     the signal logic (features / rules / live_fields)
    feed.py         (optional) how the live updater builds the bar table from
                    raw Bloomberg bars — only needed to run the updater
    ui/             (optional) ui.js + ui.css: the strategy's own views in the
                    app (its Live views and its Input sub-tab)
    data/           the strategy's bar database (quant.db) and everything the
                    engine generates for it (backtest / live outputs, logs)

See strategies/README.md for the exact functions each module provides.

The active strategy is ``$QUANT_STRATEGY`` if set, else ``"strategy"`` in
engine/config.json if that file exists, else the only folder in
engine/strategies/.
"""
from __future__ import annotations

import os
import json
import importlib
from pathlib import Path
from functools import cached_property

from . import paths
from .params import ParamSchema


def installed() -> list[str]:
    """Names of the strategy folders present (each has a manifest.json)."""
    if not paths.STRATEGIES.exists():
        return []
    return sorted(p.name for p in paths.STRATEGIES.iterdir()
                  if (p / "manifest.json").exists())


def active_name() -> str:
    name = os.environ.get("QUANT_STRATEGY")
    if name:
        return name
    try:
        return json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))["strategy"]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    found = installed()
    if len(found) == 1:
        return found[0]
    raise SystemExit(
        f"No strategy selected: {len(found)} strategy folders in {paths.STRATEGIES} "
        f"({', '.join(found) or 'none'}). Use --strategy, set QUANT_STRATEGY, or add "
        f'{{"strategy": "<name>"}} to {paths.CONFIG_PATH}')


class Strategy:
    def __init__(self, name: str):
        self.name = name
        self.folder = paths.STRATEGIES / name
        manifest = self.folder / "manifest.json"
        if not manifest.exists():
            raise SystemExit(f"Unknown strategy '{name}': {manifest} not found")
        self.manifest = json.loads(manifest.read_text(encoding="utf-8"))
        m = self.manifest
        self.title = m.get("title", name)
        self.symbol = m.get("symbol", name.upper())
        self.tz = m.get("timezone", "UTC")
        self.bar_minutes = int(m.get("bar_minutes", 5))
        self.table = m["market_table"]
        cols = m["columns"]
        self.ts_col = cols["ts"]
        self.price_col = cols["price"]
        self.open_col = cols.get("open")
        self.source_col = cols.get("source")
        self.closed_reason_col = cols.get("closed_reason")
        self.params = ParamSchema(m["params"], self.folder)

        # The strategy's own data: its bar database and every generated file.
        self.data_dir = self.folder / "data"
        self.db_path = Path(os.environ.get("QUANT_DB") or self.data_dir / "quant.db")
        self.backtest_json = self.data_dir / "backtest.json"
        self.live_signal_json = self.data_dir / "live_signal.json"
        self.live_tail_json = self.data_dir / "live_tail.json"     # forming bar (updater -> live)
        self.alert_state_json = self.data_dir / "live_alert_state.json"
        self.updater_log = self.data_dir / "updater.log"

    @cached_property
    def logic(self):
        """strategies/<name>/strategy.py"""
        return importlib.import_module(f"strategies.{self.name}.strategy")

    @cached_property
    def feed(self):
        """strategies/<name>/feed.py (only the updater needs it)."""
        return importlib.import_module(f"strategies.{self.name}.feed")

    def data_table(self, table: str) -> dict:
        for d in self.manifest.get("data_tables", []):
            if d["table"] == table:
                return d
        raise SystemExit(f"Strategy '{self.name}' has no data table '{table}'")


def load(name: str | None = None) -> Strategy:
    return Strategy(name or active_name())
