"""Tunable strategy inputs, declared by the strategy (manifest.json "params").

Each strategy keeps two parameter sets beside its manifest:

    backtest_params.json   edited on the app's Backtest tab
    live_params.json       edited on the Live tab (falls back to the backtest
                           set until it exists)

Every param is declared once in the manifest as
``{"key", "default", "step"?, "min"?, "max"?, "choices"?, "label"?, "title"?}``;
both this loader and the app (app/main.js) enforce the same rules, so a
hand-edited JSON is normalised the same way everywhere:

    * choice params keep their value only if it is one of ``choices``;
    * numeric params are snapped to a multiple of ``step``, then clamped to
      ``min``/``max``; a whole-number step makes the value an ``int`` (so it can
      be used as an index, a range() bound, a Timedelta, ...), otherwise float.

Two keys are reserved for the generic engine and should be declared by every
strategy: ``bps`` (cost per unit turnover, basis points) and ``exec_lag``
(execution lag in minutes, a multiple of the bar size).

    p = strategy.params.load("live")
    p.T, p["bps"]            # attribute and item access both work
"""
from __future__ import annotations

import json
from pathlib import Path

FILES = {"backtest": "backtest_params.json", "live": "live_params.json"}


class Params(dict):
    """A plain dict of the params that also allows attribute access."""

    def __getattr__(self, name: str) -> object:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class ParamSchema:
    def __init__(self, specs: list[dict], folder: Path):
        self.specs = {s["key"]: s for s in specs}
        self.names = tuple(self.specs)
        self.folder = folder

    def path(self, which: str) -> Path:
        return self.folder / FILES[which]

    def default(self, key: str):
        d = self.specs[key]["default"]
        return d if self.specs[key].get("choices") else float(d)

    def coerce(self, raw: dict | None) -> Params:
        """Merge `raw` over the defaults and normalise every value."""
        raw = raw or {}
        values = {}
        for name, spec in self.specs.items():
            choices = spec.get("choices")
            if choices is not None:
                values[name] = raw[name] if raw.get(name) in choices else spec["default"]
                continue
            v = float(spec["default"])
            if name in raw:
                try:
                    v = float(raw[name])
                except (TypeError, ValueError):
                    pass  # keep the default for anything non-numeric
            step = spec.get("step")
            if step:
                v = round(v / step) * step
            if spec.get("min") is not None and v < spec["min"]:
                v = float(spec["min"])
            if spec.get("max") is not None and v > spec["max"]:
                v = float(spec["max"])
            if step is not None and float(step).is_integer():
                v = int(v)
            values[name] = v
        return Params(values)

    def load(self, which: str = "backtest") -> Params:
        """Read a parameter set ("backtest" or "live"), merged over the defaults."""
        raw = _read_json(self.path(which))
        if raw is None and which == "live":
            raw = _read_json(self.path("backtest"))   # live starts from the backtest set
        return self.coerce(raw)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
