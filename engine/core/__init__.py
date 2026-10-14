"""core — the generic, strategy-agnostic half of the program.

Nothing in this package knows about any particular instrument or strategy. It provides
the machinery every strategy shares:

    strategy.py     find + load a strategy plug-in (engine/strategies/<name>/)
    params.py       tunable inputs, declared by the strategy's manifest.json
    data.py         read the strategy's bar table out of quant.db
    rules.py        the entry/exit state machine (long/short/flat, exec lag)
    backtest.py     evaluate a labelled frame -> stats, trades, equity curve
    live.py         live signal (confirmed + provisional), watch mode
    notify.py       Telegram buy/sell alerts
    table_store.py  edit the strategy's data-input tables
    updater.py      Bloomberg extract -> strategy transform -> append to quant.db

A strategy plugs in by providing a folder under engine/strategies/ — see
strategies/README.md for the interface.
"""
