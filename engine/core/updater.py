"""Generic incremental updater for a strategy's bar table (in its data/quant.db).

End to end, in one run:
  1. EXTRACT  - pull the latest intraday bars for every security the strategy's
                feed asks for, from the Bloomberg API (needs blpapi + a live
                Bloomberg Terminal session).
  2. TRANSFORM- hand the wide raw frame to the strategy's feed.transform(),
                which returns rows in the strategy's table schema.
  3. APPEND   - write only the rows newer than what is already stored.

The raw frame covers a trailing window (`context_days` before the last stored
bar) so the strategy's transform has enough history to be correct at the join
boundary. Only genuinely new timestamps are appended, so re-runs are idempotent.

Live mode (--loop, what the app runs): the still-forming bar streams in real time
from Bloomberg's //blp/mktbar service (BarStream) and is written to
the strategy's data/live_tail.json within ~1s of each trade, for the live signal. The history
request runs once per bar, just after it closes, and remains the ONLY writer to
the DB — so stored bars are identical to a plain fetch. If the stream is
unavailable or stops delivering a security the history shows trading, the loop
polls the history request every ~1s instead and retries the stream later.

The strategy's feed module (strategies/<name>/feed.py) provides
resolve_tickers / load_context / transform / STORED_COLUMNS / SOURCE_COLUMN —
see strategies/README.md.
"""
from __future__ import annotations

import sys
import json
import math
import time
import sqlite3
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone

import pandas as pd

from .data import ts_strings, ts_string

try:
    import blpapi
except ImportError:  # pragma: no cover - only importable on the Bloomberg box
    raise ImportError(
        "blpapi is not installed. Install the Bloomberg Python API and run this "
        "on a machine with a Bloomberg Terminal session (localhost:8194)."
    )

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logger = logging.getLogger("updater")

# A rolling log the Electron app's Log tab tails, so both app-spawned and
# manually-started runs show their output there.
_LOG_FMT = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")


def setup_logging(level: int = logging.INFO, log_path: "Path | None" = None) -> logging.Logger:
    """Configure the module logger. Safe to re-run (won't duplicate handlers)."""
    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_LOG_FMT)
    logger.addHandler(stream)
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fileh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2,
                                        encoding="utf-8")
            fileh.setFormatter(_LOG_FMT)
            logger.addHandler(fileh)
        except OSError:
            pass  # stdout-only is fine if the log file can't be opened
    return logger


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BLOOMBERG_HOST = "localhost"
BLOOMBERG_PORT = 8194
REFDATA_SVC = "//blp/refdata"
MKTBAR_SVC = "//blp/mktbar"      # real-time streaming bars (see BarStream)
_BAR_MESSAGES = {"MarketBarStart", "MarketBarUpdate", "MarketBarEnd"}
_FAIL_MESSAGES = {"SubscriptionFailure", "SubscriptionTerminated", "SessionTerminated",
                  "SessionConnectionDown", "SessionStartupFailure"}
# Stream health: give up on the stream (and poll instead) after a security the
# history shows trading goes unreported by it this many bars running; retry the
# stream this often while polling.
STREAM_MAX_MISSES = 3
STREAM_RETRY_SECONDS = 300
STREAM_NEAR_TOL = 5e-4    # "near" match in the stream-vs-history check (0.05%)

# A bar labelled HH:MM (its CLOSE) only counts as complete — and is stored — this
# many seconds after HH:MM, so trades printed in its final moments are included.
BAR_SETTLE_SECONDS = 5
# Closed reason given to a forming bar whose price source hasn't traded yet in
# its current session (the app's Live tab recognises this string).
AWAITING_TRADE = "awaiting first trade"
# Level for the routine step-by-step lines of a cycle. --loop drops it to DEBUG
# after the first cycle (it cycles every ~2s) and logs a heartbeat instead.
CYCLE_LOG = logging.INFO
HEARTBEAT_SECONDS = 60


def parse_ts(s: pd.Series) -> pd.Series:
    """Parse a ts column to tz-aware UTC, tolerant of mixed sub-second formats."""
    return pd.to_datetime(s, utc=True, format="mixed", errors="coerce")


# ===========================================================================
# 1. EXTRACT - Bloomberg API
# ===========================================================================
class BloombergClient:
    def __init__(self, host: str = BLOOMBERG_HOST, port: int = BLOOMBERG_PORT):
        self.host = host
        self.port = port
        self.session: "blpapi.Session | None" = None

    def start(self) -> None:
        logger.info("Starting Bloomberg session at %s:%s", self.host, self.port)
        opts = blpapi.SessionOptions()
        opts.setServerHost(self.host)
        opts.setServerPort(self.port)
        self.session = blpapi.Session(opts)
        if not self.session.start():
            raise ConnectionError("Failed to start Bloomberg API session.")
        if not self.session.openService(REFDATA_SVC):
            raise ConnectionError(f"Failed to open Bloomberg service {REFDATA_SVC}.")
        logger.info("Session started; service %s open.", REFDATA_SVC)

    def stop(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None
            logger.info("Bloomberg session stopped.")

    def get_reference_field(self, ticker: str, field: str):
        svc = self.session.getService(REFDATA_SVC)
        req = svc.createRequest("ReferenceDataRequest")
        req.getElement("securities").appendValue(ticker)
        req.getElement("fields").appendValue(field)
        self.session.sendRequest(req)
        while True:
            event = self.session.nextEvent()
            for msg in event:
                if not msg.hasElement("securityData"):
                    continue
                sec_data = msg.getElement("securityData")
                if sec_data.numValues() == 0:
                    continue
                sec = sec_data.getValueAsElement(0)
                if not sec.hasElement("fieldData"):
                    continue
                fd = sec.getElement("fieldData")
                if not fd.hasElement(field):
                    continue
                val = fd.getElement(field).getValue()
                if hasattr(val, "year"):
                    return datetime(val.year, val.month, val.day)
            if event.eventType() == blpapi.Event.RESPONSE:
                return None

    def get_intraday_bars(self, ticker, start_dt, end_dt, interval):
        """Return a list of (naive_utc_datetime, close) for the window."""
        return self.get_intraday_bars_many([ticker], start_dt, end_dt, interval)[ticker]

    def get_intraday_bars_many(self, tickers, start_dt, end_dt, interval):
        """Like get_intraday_bars for several tickers at once: every request is
        sent up front and the responses are collected as they arrive, so the
        round-trips overlap instead of running one after another.
        Returns {ticker: [(naive_utc_datetime, close), ...]}."""
        svc = self.session.getService(REFDATA_SVC)
        pending: dict[int, str] = {}
        for i, ticker in enumerate(tickers):
            req = svc.createRequest("IntradayBarRequest")
            req.set("security", ticker)
            req.set("eventType", "TRADE")
            req.set("interval", interval)
            req.set("startDateTime", start_dt.strftime("%Y-%m-%dT%H:%M:%S"))
            req.set("endDateTime", end_dt.strftime("%Y-%m-%dT%H:%M:%S"))
            self.session.sendRequest(req, correlationId=blpapi.CorrelationId(i))
            pending[i] = ticker

        bars: dict[str, list[tuple[datetime, float]]] = {t: [] for t in tickers}
        while pending:
            event = self.session.nextEvent()
            finished = set()
            for msg in event:
                cid = msg.correlationId().value()
                ticker = pending.get(cid)
                if ticker is None:
                    continue  # session/service status, not one of our requests
                if event.eventType() == blpapi.Event.RESPONSE:
                    finished.add(cid)  # final chunk of this request's reply
                if not msg.hasElement("barData"):
                    continue
                bar_data = msg.getElement("barData")
                if not bar_data.hasElement("barTickData"):
                    continue
                ticks = bar_data.getElement("barTickData")
                for i in range(ticks.numValues()):
                    tick = ticks.getValueAsElement(i)
                    if not (tick.hasElement("time") and tick.hasElement("close")):
                        continue
                    t = tick.getElement("time").getValue()
                    if not hasattr(t, "year"):
                        continue
                    dt = datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)
                    bars[ticker].append((dt, float(tick.getElement("close").getValue())))
            for cid in finished:
                del pending[cid]
        return bars


def _utc(value) -> "pd.Timestamp | None":
    """A blpapi datetime -> tz-aware UTC Timestamp (naive values are UTC)."""
    if not (hasattr(value, "year") and hasattr(value, "hour")):
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


class BarStream:
    """Real-time bars for every tracked security from Bloomberg's //blp/mktbar
    service — the streaming form of the IntradayBarRequest bars the DB is built
    from. Every trade pushes a MarketBarUpdate carrying the bar's running CLOSE,
    and the first trade in a new interval sends MarketBarStart, so "has this
    listing traded in the forming bar yet" is known the moment it happens.

    Its own session, drained from the updater loop via poll(). It never writes
    to the DB: streamed bars are only overlaid onto the not-yet-stored rows that
    feed the live tail. Stored bars always come from the history request."""

    def __init__(self, ticker_map: list[dict], interval: int, tz: str,
                 host: str = BLOOMBERG_HOST, port: int = BLOOMBERG_PORT):
        self.columns = {s["fetch"]: s["column"] for s in ticker_map}   # ticker -> column
        self.interval = interval
        self.tz = tz
        self.host, self.port = host, port
        self.session: "blpapi.Session | None" = None
        self.bars: dict[str, dict[pd.Timestamp, float]] = {}  # column -> {label (UTC): close}
        self.version = 0        # bumps whenever any bar's close changes
        self.updates = 0        # bar messages received (heartbeat)
        self.failed: "str | None" = None
        self.started_at: "pd.Timestamp | None" = None
        self._cid_col: dict[int, str] = {}
        self._misses: dict[str, int] = {}

    @property
    def tickers(self) -> list[str]:
        return list(self.columns)

    def start(self) -> None:
        opts = blpapi.SessionOptions()
        opts.setServerHost(self.host)
        opts.setServerPort(self.port)
        self.session = blpapi.Session(opts)
        if not self.session.start():
            raise ConnectionError("Failed to start Bloomberg stream session.")
        if not self.session.openService(MKTBAR_SVC):
            raise ConnectionError(f"Failed to open Bloomberg service {MKTBAR_SVC}.")
        subs = blpapi.SubscriptionList()
        for i, (ticker, col) in enumerate(self.columns.items()):
            subs.add(f"{MKTBAR_SVC}/ticker/{ticker}", "LAST_PRICE",
                     f"interval={self.interval}", blpapi.CorrelationId(i))
            self._cid_col[i] = col
        self.session.subscribe(subs)
        self.started_at = pd.Timestamp.now(tz="UTC")
        logger.info("Stream: subscribed to %d securities via %s (%d-min bars).",
                    len(self.columns), MKTBAR_SVC, self.interval)

    def stop(self) -> None:
        if self.session is not None:
            session, self.session = self.session, None
            try:
                session.stop()
            except Exception:
                pass

    def poll(self, timeout_ms: int = 100) -> None:
        """Handle every waiting event, blocking up to `timeout_ms` for the first."""
        event = self.session.nextEvent(timeout_ms)
        while event is not None:
            self._handle(event)
            event = self.session.tryNextEvent()

    def _handle(self, event) -> None:
        for msg in event:
            kind = str(msg.messageType())
            cid = msg.correlationId().value() if msg.correlationId() else None
            if kind in _BAR_MESSAGES:
                col = self._cid_col.get(cid)
                if col is None or not (msg.hasElement("CLOSE") and msg.hasElement("DATE_TIME")):
                    continue
                start = _utc(msg.getElement("DATE_TIME").getValue())   # bar START
                if start is None:
                    continue
                # label by bar CLOSE, like the history bars (see fetch_raw_df)
                label = start.floor(f"{self.interval}min") + pd.Timedelta(minutes=self.interval)
                close = float(msg.getElement("CLOSE").getValue())
                self.updates += 1
                bars = self.bars.setdefault(col, {})
                if bars.get(label) != close:
                    bars[label] = close
                    self.version += 1
            elif kind in _FAIL_MESSAGES and self.session is not None:
                col = self._cid_col.get(cid)
                what = f" for {col}" if col else ""
                try:
                    why = msg.getElement("reason").getElementAsString("description")
                except Exception:
                    why = ""
                self.failed = f"{kind}{what}" + (f": {why}" if why else "")

    def overlay(self, raw: pd.DataFrame, after: "pd.Timestamp | None") -> pd.DataFrame:
        """`raw` with every streamed bar newer than `after` (the last STORED bar)
        written in — for unstored bars the stream is newer than the last history
        fetch. Adds the row for a bar the fetch hasn't seen yet."""
        r = raw.set_index("ts")
        for col, bars in self.bars.items():
            for label, close in bars.items():
                if after is None or label > after:
                    r.loc[ts_string(label, self.tz), col] = close
        return r.sort_index().reset_index()

    def prune(self, through: pd.Timestamp) -> None:
        """Forget bars at/before `through` (now stored from history)."""
        for bars in self.bars.values():
            for label in [lb for lb in bars if lb <= through]:
                del bars[label]

    def check(self, raw: pd.DataFrame, label: pd.Timestamp) -> "tuple[int, int, int] | None":
        """Compare the stream's bar at `label` (just completed) with the history
        fetch: returns (exact, near, traded) — of the securities that traded in
        it, how many the stream reported at exactly the history close, and how
        many within STREAM_NEAR_TOL. (Fast FX pairs tick thousands of times a bar
        and the stream's last update can land ~1s after the close, so the two can
        end on neighbouring ticks — a one-pip difference, not a fault.)
        A security the history shows trading but the stream never reports,
        STREAM_MAX_MISSES bars running, marks the stream failed (e.g. delayed or
        unentitled real-time data). None when the bar started before the
        subscription (nothing to compare)."""
        if self.started_at is None or label - pd.Timedelta(minutes=self.interval) < self.started_at:
            return None
        key = ts_string(label, self.tz)
        hist = raw.set_index("ts")
        if key not in hist.index:
            return None
        row = hist.loc[key]
        exact = near = traded = 0
        for ticker, col in self.columns.items():
            if pd.isna(row.get(col)):
                continue
            traded += 1
            streamed = self.bars.get(col, {}).get(label)
            if streamed is None:
                self._misses[col] = self._misses.get(col, 0) + 1
                if self._misses[col] >= STREAM_MAX_MISSES:
                    self.failed = (f"no real-time bars for {ticker} in {self._misses[col]} bars "
                                   "that history shows trading (delayed/unentitled data?)")
            else:
                self._misses[col] = 0
                if math.isclose(streamed, float(row[col]), rel_tol=1e-9):
                    exact += 1
                elif math.isclose(streamed, float(row[col]), rel_tol=STREAM_NEAR_TOL):
                    near += 1
        return exact, near, traded


# ===========================================================================
# The updater for one strategy
# ===========================================================================
class Updater:
    def __init__(self, strat, db_path: Path | None = None):
        self.strat = strat
        self.feed = strat.feed
        self.db_path = Path(db_path or strat.db_path)
        self.interval = strat.bar_minutes
        self.tz = strat.tz
        self.table = strat.table
        self.ts_col = strat.ts_col
        # Resolved ticker map, reused for the rest of the UTC day (contract rolls
        # happen on expiry) so a fast --loop doesn't re-resolve every cycle.
        self._ticker_cache: "tuple[object, list[dict]] | None" = None
        self._last_tail_rows: "list | None" = None

    # ---- extract ------------------------------------------------------------
    def ticker_map(self, client: BloombergClient) -> list[dict]:
        today = datetime.now(timezone.utc).date()
        if self._ticker_cache is not None and self._ticker_cache[0] == today:
            return self._ticker_cache[1]
        resolved = self.feed.resolve_tickers(client)
        logger.info("Ticker map: %s", ", ".join(s["fetch"] for s in resolved))
        self._ticker_cache = (today, resolved)
        return resolved

    def fetch_raw_df(self, start: datetime, end: datetime,
                     client: "BloombergClient | None" = None) -> pd.DataFrame:
        """Pull every tracked security's intraday bars over [start, end] (naive UTC),
        snap each bar to the bar grid, pivot wide. Every column is always present,
        even with zero bars. `ts` is the canonical stored timestamp string.

        If `client` is given, its (already-started) session is reused and left open
        for the caller — this is how --loop keeps one session alive across cycles.
        When None, a session is started and stopped for this single call."""
        interval = self.interval
        logger.log(CYCLE_LOG, "Fetch window %s -> %s UTC @ %dmin", start, end, interval)
        t0 = time.perf_counter()
        owns_client = client is None
        if owns_client:
            client = BloombergClient()
            client.start()
        try:
            ticker_map = self.ticker_map(client)
            columns = [s["column"] for s in ticker_map]
            all_bars = client.get_intraday_bars_many([s["fetch"] for s in ticker_map],
                                                     start, end, interval)
            frames = []
            for s in ticker_map:
                bars = all_bars[s["fetch"]]
                level = logging.WARNING if not bars else logging.DEBUG
                logger.log(level, "  %-14s %-16s %7d bars", s["column"], s["fetch"], len(bars))
                if not bars:
                    continue
                col = pd.DataFrame(bars, columns=["bar_utc", s["column"]])
                col[s["column"]] *= s.get("scale", 1.0)
                # Bloomberg stamps bars at interval START; history labels by bar CLOSE.
                # Add one interval so live bars line up with the stored convention.
                bt = (pd.to_datetime(col["bar_utc"]).dt.floor(f"{interval}min")
                      + pd.Timedelta(minutes=interval))
                col["ts"] = ts_strings(bt.dt.tz_localize("UTC"), self.tz)
                col = col.drop_duplicates("ts", keep="last")
                frames.append(col.set_index("ts")[[s["column"]]])
        finally:
            if owns_client:
                client.stop()

        if not frames:
            logger.warning("No bars returned for any security; empty frame.")
            return pd.DataFrame(columns=["ts"] + columns)

        wide = pd.concat(frames, axis=1).reindex(columns=columns)
        wide.index.name = "ts"
        wide = wide.sort_index().reset_index()
        logger.log(CYCLE_LOG, "Wide frame: %d rows x %d cols in %.2fs",
                   wide.shape[0], wide.shape[1], time.perf_counter() - t0)
        return wide

    def to_grid(self, wide: pd.DataFrame, ffill: bool = True) -> pd.DataFrame:
        """Reindex a wide frame onto a continuous bar grid. With ffill=True each
        row carries the last known price of every instrument (dense)."""
        if wide.empty:
            return wide
        interval = self.interval
        idx = parse_ts(wide["ts"])
        g = wide.drop(columns="ts").set_index(idx).sort_index()
        g = g[~g.index.duplicated(keep="last")]
        grid = pd.date_range(g.index.min().floor(f"{interval}min"),
                             g.index.max().ceil(f"{interval}min"),
                             freq=f"{interval}min", tz="UTC")
        g = g.reindex(g.index.union(grid))
        if ffill:
            g = g.ffill()
        g = g.reindex(grid)
        out = g.reset_index().rename(columns={"index": "ts"})
        out["ts"] = ts_strings(out["ts"], self.tz)
        logger.log(CYCLE_LOG, "Gridded -> %d rows @ %dmin (ffill=%s)", len(out), interval, ffill)
        return out

    # ---- load / append ------------------------------------------------------
    def _max(self, column):
        """MAX(column) of the bar table; None while the DB/table doesn't exist yet
        (a new strategy starts from an empty data folder)."""
        if not self.db_path.exists():
            return None
        with sqlite3.connect(self.db_path) as conn:
            try:
                return conn.execute(f'SELECT MAX("{column}") FROM "{self.table}"').fetchone()[0]
            except sqlite3.OperationalError:   # no such table yet
                return None

    def _max_ts(self):
        return self._max(self.ts_col)

    def last_stored_utc_naive(self) -> datetime | None:
        """Most recent stored ts as a naive-UTC datetime (or None)."""
        last = self._max_ts()
        if last is None:
            return None
        logger.log(CYCLE_LOG, "Last stored bar: %s", last)
        return pd.to_datetime(last, utc=True).tz_localize(None).to_pydatetime()

    def last_stored_ts(self) -> "pd.Timestamp | None":
        """Label of the newest stored bar (tz-aware UTC), or None if the table is empty."""
        last = self._max_ts()
        return None if last is None else pd.Timestamp(last).tz_convert("UTC")

    def to_stored_rows(self, final: pd.DataFrame) -> pd.DataFrame:
        """Transformed rows -> the stored format: ts as the local string and the
        open flag as 0/1 (no row_id)."""
        out = final.copy()
        out[self.ts_col] = ts_strings(out[self.ts_col], self.tz)
        if self.strat.open_col:
            out[self.strat.open_col] = out[self.strat.open_col].astype(int)
        return out[self.feed.STORED_COLUMNS]

    def append_new_rows(self, final: pd.DataFrame, dry_run: bool = False) -> int:
        """Append only rows newer than the current max ts, continuing row_id."""
        last, max_id = self._max_ts(), self._max("row_id")

        out = self.to_stored_rows(final)
        if last is not None:  # zero-padded fixed-offset ISO -> lexicographic == chronological
            out = out[out[self.ts_col] > last]

        if out.empty:
            logger.log(CYCLE_LOG, "No new rows to append (%s already current).", self.table)
            return 0

        start_id = (max_id + 1) if max_id is not None else 0
        out.insert(0, "row_id", range(start_id, start_id + len(out)))

        if dry_run:
            logger.info("[dry-run] would append %d rows (%s -> %s).",
                        len(out), out[self.ts_col].iloc[0], out[self.ts_col].iloc[-1])
            print(out.tail())
            return len(out)

        with sqlite3.connect(self.db_path) as conn:
            out.to_sql(self.table, conn, if_exists="append", index=False)
        logger.info("Appended %d rows (%s -> %s).",
                    len(out), out[self.ts_col].iloc[0], out[self.ts_col].iloc[-1])
        return len(out)

    def gate_unprinted(self, pending: pd.DataFrame, raw: pd.DataFrame,
                       final: pd.DataFrame) -> pd.DataFrame:
        """Mark an unstored bar closed (AWAITING_TRADE) until its price source has
        traded at least once in its current session. Until then the price is the
        previous session's close carried forward (e.g. yesterday's close in the
        seconds after the open, before the opening auction prints). Once the
        source has traded this session, a quiet bar just carries its last print —
        exactly what the stored bars and the backtest do, so it isn't gated.

        Needs the strategy's open/source/closed_reason columns and its feed's
        SOURCE_COLUMN map; a strategy without them is never gated.
        `raw` is the wide frame BEFORE gridding (a missing value there means "not
        traded", not "carried forward"); `final` is the transformed frame the
        pending rows came from, used to find where the current session began."""
        s = self.strat
        source_column = getattr(self.feed, "SOURCE_COLUMN", None)
        if pending.empty or not (s.open_col and s.source_col and s.closed_reason_col
                                 and source_column):
            return pending
        printed = raw.set_index("ts") if not raw.empty else pd.DataFrame()
        labels = ts_strings(final[self.ts_col], self.tz).to_numpy()
        sources = final[s.source_col].to_numpy()
        is_open = final[s.open_col].to_numpy(bool)
        row_of = {label: i for i, label in enumerate(labels)}
        pending = pending.copy()
        pending_labels = ts_strings(pending[self.ts_col], self.tz)
        for idx, label, src in zip(pending.index, pending_labels, pending[s.source_col]):
            col = source_column.get(src)
            if col is None:
                continue  # already closed
            # walk back to the first bar of this run of open bars priced off `src`
            i = j = row_of[label]
            while j > 0 and is_open[j - 1] and sources[j - 1] == src:
                j -= 1
            traded = any(lb in printed.index and pd.notna(printed.at[lb, col])
                         for lb in labels[j:i + 1])
            if not traded:
                pending.at[idx, s.open_col] = False
                pending.at[idx, s.closed_reason_col] = AWAITING_TRADE
        return pending

    def write_live_tail(self, forming: pd.DataFrame) -> None:
        """Write the forming (not-yet-stored) bars for the live signal. Skipped
        when unchanged, so the file's mtime only moves on a real price change;
        written to a temp file then swapped in, so a reader never sees half a file."""
        path = self.strat.live_tail_json
        rows = json.loads(self.to_stored_rows(forming).to_json(orient="records", double_precision=15))
        if rows == self._last_tail_rows:
            return
        payload = {"written_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        for attempt in range(5):  # Windows: the reader may briefly hold the file
            try:
                tmp.replace(path)
                break
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        self._last_tail_rows = rows

    # ---- orchestration ------------------------------------------------------
    def fetch_window(self, context_days: int = 10, default_lookback_days: int = 365,
                     client: "BloombergClient | None" = None) -> pd.DataFrame:
        """History request: raw bars from `context_days` before the last stored bar
        (so the strategy's transform has context) up to now."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)   # first run: create it
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        last = self.last_stored_utc_naive()
        if last is None:
            logger.info("%s empty; pulling last %dd.", self.table, default_lookback_days)
            start = end - timedelta(days=default_lookback_days)
        else:
            start = last - timedelta(days=context_days)
        return self.fetch_raw_df(start, end, client=client)

    def process(self, raw: pd.DataFrame, context=None, store: bool = True,
                dry_run: bool = False) -> int:
        """Raw bars -> stored rows. With store=True the COMPLETED bars are appended
        (bars are labelled by their CLOSE, so one counts as complete once it's
        BAR_SETTLE_SECONDS past its label). Every row newer than what's stored
        then goes to the live tail, gated on a real print.
        Returns the number of rows appended."""
        wide = self.to_grid(raw, ffill=True)
        if context is None:
            context = self.feed.load_context(self.db_path)
        final = self.feed.transform(wide, context)
        logger.log(CYCLE_LOG, "Transformed -> %d final rows.", len(final))
        final = final[final[self.ts_col].dt.minute % self.interval == 0]
        n = 0
        if store:
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=BAR_SETTLE_SECONDS)
            n = self.append_new_rows(final[final[self.ts_col] <= cutoff], dry_run=dry_run)
        if not dry_run:
            last = self.last_stored_ts()
            pending = final if last is None else final[final[self.ts_col] > last]
            self.write_live_tail(self.gate_unprinted(pending, raw, final))
        return n

    def run(self, context_days: int = 10, default_lookback_days: int = 365,
            dry_run: bool = False, client: "BloombergClient | None" = None) -> int:
        """One history fetch -> transform -> append (+ live tail)."""
        raw = self.fetch_window(context_days, default_lookback_days, client)
        return self.process(raw, dry_run=dry_run)

    def _next_fetch_time(self) -> float:
        """Unix time of the next bar close + BAR_SETTLE_SECONDS."""
        now = pd.Timestamp.now(tz="UTC")
        step = pd.Timedelta(minutes=self.interval)
        return (now.floor(step) + step + pd.Timedelta(seconds=BAR_SETTLE_SECONDS)).timestamp()

    def _start_stream(self, client: BloombergClient) -> "BarStream | None":
        live = None
        try:
            live = BarStream(self.ticker_map(client), self.interval, self.tz)
            live.start()
            return live
        except Exception as e:
            if live is not None:
                live.stop()
            logger.warning("Stream unavailable (%s); polling instead, retrying in %d min.",
                           e, STREAM_RETRY_SECONDS // 60)
            return None

    def run_loop(self, context_days: int, default_lookback_days: int,
                 stream: bool = True) -> None:
        """Keep the bar table and the live tail current until stopped. This is the
        mode the Electron app launches. A cycle's errors are logged and swallowed
        (and the session reconnected) so a transient Bloomberg/DB hiccup doesn't
        kill the loop.

        Streaming (default): the forming bar comes from BarStream as each trade
        happens, and the live tail is refreshed from it straight away. The history
        request runs once per bar, just after it closes, and is the only thing
        that writes to the DB. Polling (--no-stream, or while the stream is
        unavailable/unhealthy): the history request runs every ~1s and supplies
        the forming bar too."""
        global CYCLE_LOG
        logger.info("Loop mode [%s]: %s. Ctrl-C to stop.", self.strat.name,
                    "real-time stream + one history fetch per bar" if stream
                    else "polling continuously")
        client: "BloombergClient | None" = None   # history requests (//blp/refdata)
        live: "BarStream | None" = None           # real-time bars (//blp/mktbar)
        retry_stream_at = 0.0 if stream else math.inf
        raw = context = None
        next_fetch = 0.0
        seen_version = -1
        beat_t0, beat_cycles, beat_busy, beat_tails = time.time(), 0, 0.0, 0
        beat_updates, beat_match = 0, None
        try:
            while True:
                t0 = time.perf_counter()
                try:
                    if client is None:
                        client = BloombergClient()
                        client.start()
                    if live is not None and live.failed:
                        logger.warning("Stream stopped (%s); polling instead, retrying in %d min.",
                                       live.failed, STREAM_RETRY_SECONDS // 60)
                        live.stop()
                        live, retry_stream_at = None, time.time() + STREAM_RETRY_SECONDS
                    if live is None and time.time() >= retry_stream_at:
                        live = self._start_stream(client)
                        if live is None:
                            retry_stream_at = time.time() + STREAM_RETRY_SECONDS
                        next_fetch, beat_updates = 0.0, 0   # fresh history under the stream

                    if live is None:
                        # ---- polling: the history request does everything, every ~1s ----
                        n = self.run(context_days, default_lookback_days, client=client)
                        logger.log(logging.INFO if n or CYCLE_LOG == logging.INFO else logging.DEBUG,
                                   "Cycle done: %d row(s) appended.", n)
                        CYCLE_LOG = logging.DEBUG   # full detail for the first cycle only
                        beat_cycles += 1
                        beat_busy += time.perf_counter() - t0
                        if time.time() - beat_t0 >= HEARTBEAT_SECONDS:
                            logger.info("Live (polling): %d cycles in the last %ds (%.1fs each); "
                                        "forming bar -> %s", beat_cycles, HEARTBEAT_SECONDS,
                                        beat_busy / beat_cycles, self.strat.live_tail_json.name)
                            beat_t0, beat_cycles, beat_busy = time.time(), 0, 0.0
                        time.sleep(1)
                        continue

                    # ---- streaming ----
                    if time.time() >= next_fetch:
                        # A bar just closed: store it from history (the only DB writer),
                        # and check the stream reported that bar the same way.
                        raw = self.fetch_window(context_days, default_lookback_days, client=client)
                        context = self.feed.load_context(self.db_path)
                        self.process(raw, context)
                        step = pd.Timedelta(minutes=self.interval)
                        closed = (pd.Timestamp.now(tz="UTC")
                                  - pd.Timedelta(seconds=BAR_SETTLE_SECONDS)).floor(step)
                        beat_match = (closed, live.check(raw, closed))
                        stored = self.last_stored_ts()
                        if stored is not None:
                            live.prune(stored)
                        if [s["fetch"] for s in self.ticker_map(client)] != live.tickers:
                            logger.info("Ticker map changed (contract roll); resubscribing the stream.")
                            live.stop()
                            live, retry_stream_at = None, 0.0
                            continue
                        next_fetch = self._next_fetch_time()
                        CYCLE_LOG = logging.DEBUG
                        seen_version = -1   # re-apply the stream on top of the fresh history
                    if live.version != seen_version:
                        seen_version = live.version
                        self.process(live.overlay(raw, self.last_stored_ts()), context, store=False)
                        beat_tails += 1
                    live.poll(timeout_ms=100)
                    if time.time() - beat_t0 >= HEARTBEAT_SECONDS:
                        match = ""
                        if beat_match and beat_match[1]:
                            label, (exact, near, traded) = beat_match
                            match = (f"; bar {label.tz_convert(self.tz).strftime('%H:%M')} vs history: "
                                     f"{exact}/{traded} exact")
                            if near:
                                match += f", {near} within {STREAM_NEAR_TOL:.2%} (last-tick timing)"
                            if exact + near < traded:
                                match += f", {traded - exact - near} off or missing"
                        logger.info("Live (stream): %d bar updates, %d tail refreshes in the last %ds%s",
                                    live.updates - beat_updates, beat_tails, HEARTBEAT_SECONDS, match)
                        beat_t0, beat_tails, beat_updates = time.time(), 0, live.updates
                        beat_match = None   # report each bar's comparison once
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # keep the loop alive across a bad cycle
                    logger.error("Cycle failed: %s", e)
                    CYCLE_LOG = logging.INFO   # show the recovery cycle in full
                    next_fetch = 0.0
                    # The sessions may be broken (disconnect/timeout); drop them so the
                    # next cycle reconnects with fresh ones.
                    if live is not None:
                        live.stop()
                        live, retry_stream_at = None, time.time()
                    if client is not None:
                        try: client.stop()
                        except Exception: pass
                        client = None
                    time.sleep(1)
        finally:
            if live is not None:
                live.stop()
            if client is not None:
                client.stop()
