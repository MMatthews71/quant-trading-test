"""Engine-wide locations (absolute, so scripts work from any directory).

Everything belonging to a strategy — its bar database, generated outputs and
logs — lives inside the strategy's own folder (see core/strategy.py), not here.
"""
from __future__ import annotations

from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent      # engine/
ROOT = ENGINE.parent                                  # repo root
STRATEGIES = ENGINE / "strategies"

# Optional: {"strategy": "<name>"} picks the active strategy when more than one
# strategy folder exists (see core/strategy.py).
CONFIG_PATH = ENGINE / "config.json"
TELEGRAM_CONFIG = ENGINE / "telegram_config.json"
