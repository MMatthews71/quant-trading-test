"""quant.py — the engine's one command-line entry point.

    python engine/quant.py backtest                    # full backtest -> <strategy>/data/backtest.json
    python engine/quant.py live [--watch]              # live signal -> <strategy>/data/live_signal.json
    python engine/quant.py export backtest|live OUT    # full per-bar CSV to OUT
    python engine/quant.py update [--loop] [...]       # Bloomberg -> <strategy>/data/quant.db
    python engine/quant.py table TABLE CMD [...]       # edit a hand-maintained data table
    python engine/quant.py notify test|chatid|started|stopped

Every command takes --strategy NAME (default: $QUANT_STRATEGY, else
engine/config.json if present, else the only folder in engine/strategies/). The logic lives in engine/core/ (generic) and
engine/strategies/<name>/ (the strategy plug-in).
"""
from __future__ import annotations

import sys
import json
import logging
import argparse
from pathlib import Path


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_backtest(strat, args):
    """Run the strategy's backtest params over all stored bars; write the strategy's data/backtest.json."""
    from core import backtest
    _df, stats, _trades = backtest.run(strat, strat.params.load("backtest"), write=True)
    print(stats)


def cmd_live(strat, args):
    """Compute the live signal once, or keep recomputing it with --watch.
    --watch is what the app runs. See core/live.py."""
    from core import live
    live.watch(strat) if args.watch else live.run_once(strat)


def cmd_export(strat, args):
    """Full per-bar CSV (every bar, every column), written straight to OUT.
    backtest: the backtest params with their execution lag;
    live: the live params with instant fills (matches the Live tab)."""
    from core import backtest
    lag = 0 if args.which == "live" else None
    df, _stats, _trades = backtest.run(strat, strat.params.load(args.which),
                                       write=False, exec_lag_bars=lag)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"wrote {len(df)} rows, {df.shape[1]} columns to {args.out}")


def cmd_update(strat, args):
    """Fetch -> transform -> append the strategy's bars (core/updater.py + its feed.py)."""
    from core import updater
    updater.setup_logging(logging.DEBUG if args.debug else logging.INFO, strat.updater_log)
    u = updater.Updater(strat, args.db)
    if args.loop:
        try:
            u.run_loop(args.context_days, args.lookback_days, stream=not args.no_stream)
        except KeyboardInterrupt:
            updater.logger.info("Loop stopped.")
        return
    n = u.run(args.context_days, args.lookback_days, dry_run=args.dry_run)
    updater.logger.info("Done. %d row(s) %s.", n, "pending (dry-run)" if args.dry_run else "appended")


def cmd_table(strat, args):
    """Edit a hand-maintained data table (the app reads quant.db read-only, so it writes via this).
    TABLE is one of the manifest's data_tables ('all' with ensure)."""
    from core import table_store
    if args.table == "all" and args.cmd == "ensure":
        for spec in strat.manifest.get("data_tables", []):
            table_store.connect(spec, strat.db_path).close()
        return
    spec = strat.data_table(args.table)
    try:
        row = json.loads(args.row)
        if args.cmd == "ensure":
            table_store.connect(spec, strat.db_path).close()
        elif args.cmd == "add":
            table_store.add(spec, strat.db_path, row)
        elif args.cmd == "update":
            table_store.update(spec, strat.db_path, args.id or row.get(spec["key"]), row)
        else:
            table_store.delete(spec, strat.db_path, args.id)
    except ValueError as e:
        sys.exit(str(e))


def cmd_notify(strat, args):
    """Telegram helpers: test message, list chat ids, online/offline pings."""
    from core import notify
    logging.basicConfig(level=logging.INFO)
    if args.what == "chatid":
        notify.print_chat_ids()
        return
    text = {
        "test": "✅ Test message from your quant trading bot. Buy/sell alerts will post here.",
        "started": "\U0001F7E2 Quant Trading is ONLINE — live signals running.",
        "stopped": "\U0001F534 Quant Trading is OFFLINE — live signals stopped.",
    }[args.what]
    if args.what != "test":
        text += f"\n{notify.now_local(strat)}"
    ok, info = notify.send_message(text)
    print("sent" if ok else f"not sent ({info})")


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Quant trading engine")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--strategy", help="folder under engine/strategies "
                                           "(default: the only one; or $QUANT_STRATEGY / engine/config.json)")
    sub = ap.add_subparsers(dest="command", required=True)

    def add(name, fn):
        p = sub.add_parser(name, parents=[common], help=fn.__doc__.splitlines()[0],
                           description=fn.__doc__)
        p.set_defaults(fn=fn)
        return p

    add("backtest", cmd_backtest)

    p = add("live", cmd_live)
    p.add_argument("--watch", action="store_true", help="keep running and recompute on change")

    p = add("export", cmd_export)
    p.add_argument("which", choices=["backtest", "live"])
    p.add_argument("out", type=Path)

    p = add("update", cmd_update)
    p.add_argument("--db", type=Path, default=None, help="path to quant.db")
    p.add_argument("--context-days", type=int, default=10,
                   help="lookback (days) before the last stored bar for stitching context")
    p.add_argument("--lookback-days", type=int, default=365,
                   help="initial pull window when the table is empty")
    p.add_argument("--loop", action="store_true",
                   help="run continuously: real-time stream for the forming bar, "
                        "history fetch per bar for the DB")
    p.add_argument("--no-stream", action="store_true",
                   help="with --loop: poll the history request every ~1s instead of streaming")
    p.add_argument("--dry-run", action="store_true", help="compute but do not write")
    p.add_argument("--debug", action="store_true", help="verbose logging")

    p = add("table", cmd_table)
    p.add_argument("table", help="one of the manifest's data_tables ('all' with ensure)")
    p.add_argument("cmd", choices=["ensure", "add", "update", "delete"])
    p.add_argument("--id", help="key of the row to update/delete")
    p.add_argument("--row", default="{}", help="JSON object of column -> value")

    p = add("notify", cmd_notify)
    p.add_argument("what", nargs="?", default="test", choices=["test", "chatid", "started", "stopped"])
    return ap


def main(argv=None):
    from core import strategy
    args = build_parser().parse_args(argv)
    args.fn(strategy.load(args.strategy), args)


if __name__ == "__main__":
    main()
