"""Telegram buy/sell alerts for the live signal (any strategy).

Posts a message whenever the live signal's POSITION changes (flat -> long is a
BUY, long -> flat a SELL, etc.). Called from core.live with the CONFIRMED
signal only (completed bars) — never the provisional forming-bar signal.

Config (never committed):
    engine/telegram_config.json  {"enabled": true, "bot_token": "...", "chat_id": "..."}
  or environment variables TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Fails soft: missing config or a network error is logged and swallowed so it
can never break the live-signal computation. CLI: `quant.py notify`.
"""
from __future__ import annotations

import os
import re
import json
import logging
import urllib.parse
import urllib.request

from . import paths

logger = logging.getLogger("notify")

# position -> (emoji, headline) shown when it BECOMES that position from flat.
_OPEN = {
    1:  ("\U0001F7E2", "BUY — OPEN LONG"),          # 🟢
    -1: ("\U0001F534", "SELL SHORT — OPEN SHORT"),  # 🔴
}
_CLOSE = {
    1:  ("⚪", "SELL — CLOSE LONG"),          # ⚪ (was long)
    -1: ("⚪", "BUY TO COVER — CLOSE SHORT"),  # ⚪ (was short)
}


def load_config():
    try:
        cfg = json.loads(paths.TELEGRAM_CONFIG.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cfg = {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("bot_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("chat_id")
    enabled = cfg.get("enabled", True)
    # Ignore the untouched template placeholders.
    if token and "PASTE" in str(token):
        token = None
    if chat and "PASTE" in str(chat):
        chat = None
    return token, str(chat) if chat else None, bool(enabled)


def is_configured() -> bool:
    token, chat, enabled = load_config()
    return bool(enabled and token and chat)


def send_message(text: str):
    """Post a plain-text message. Returns (ok: bool, info: str)."""
    token, chat, enabled = load_config()
    if not enabled:
        return False, "disabled"
    if not token or not chat:
        return False, "not configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
            return ok, "sent" if ok else f"http {resp.status}"
    except Exception as e:  # network / auth / bad chat id
        logger.warning("Telegram send failed: %s", e)
        return False, str(e)


def _read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(path, state):
    try:
        os.makedirs(path.parent, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write alert state: %s", e)


def format_alert(prev: int, pos: int, payload: dict, symbol: str) -> str:
    mkt = payload.get("market", {})
    sig = payload.get("signal", {})
    price = mkt.get("price")
    price_txt = f"${price:,.2f}" if isinstance(price, (int, float)) else "—"
    source = mkt.get("source") or ""
    reason = sig.get("reason") or ""
    # drop the execution-lag tag (e.g. " (+2b)") — not useful in a live ping.
    reason = re.sub(r"\s*\(\+\d+b\)$", "", reason)

    if prev == 0:                       # opening a fresh position
        emoji, head = _OPEN[pos]
    elif pos == 0:                      # closing to flat
        emoji, head = _CLOSE[prev]
    elif pos == -1:                     # direct flip long -> short
        emoji, head = "\U0001F501", "FLIP — CLOSE LONG then SELL SHORT"
    else:                               # direct flip short -> long
        emoji, head = "\U0001F501", "FLIP — CLOSE SHORT then BUY LONG"

    lines = [f"{emoji} {head} — {symbol}"]
    if reason:
        lines.append(reason)
    nums = [f"Price {price_txt}" + (f" ({source})" if source else "")]
    for name, v in (payload.get("signals") or {}).items():
        if isinstance(v, (int, float)):
            nums.append(f"{name}={v:.3f}")
    lines.append("  ".join(nums))
    return "\n".join(lines)


def now_local(strat) -> str:
    """'YYYY-MM-DD HH:MM <tz label>' in the strategy's timezone (for pings)."""
    import datetime
    import zoneinfo
    now = datetime.datetime.now(zoneinfo.ZoneInfo(strat.tz)).strftime("%Y-%m-%d %H:%M")
    return f"{now} {strat.manifest.get('timezone_label', strat.tz)}"


def print_chat_ids():
    """Print the chat ids that have messaged the bot (to fill in chat_id)."""
    token, _, _ = load_config()
    if not token:
        print("No bot token configured (telegram_config.json or TELEGRAM_BOT_TOKEN).")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print("getUpdates failed:", e)
        return
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    if not seen:
        print("No chats found. Send a message to your bot first, then re-run.")
    for cid, name in seen.items():
        print(f"chat_id: {cid}   ({name})")


def maybe_alert(payload: dict, strat):
    """Post a Telegram alert if the live position changed since the last alert.

    Returns (sent: bool, info: str). The first time it runs (no prior state) it
    records the current position WITHOUT sending, so you don't get a spurious
    ping for a position that was already open.
    """
    if not is_configured():
        return False, "not configured"
    try:
        pos = int(payload["signal"]["position"])
    except (KeyError, TypeError, ValueError):
        return False, "no position in payload"

    state = _read_state(strat.alert_state_json)
    prev = state.get("position")
    ts = payload.get("market", {}).get("ts")
    if prev is None:
        _write_state(strat.alert_state_json, {"position": pos, "ts": ts})
        return False, "initialized (no alert)"
    if pos == int(prev):
        return False, "no change"

    sent, info = send_message(format_alert(int(prev), pos, payload, strat.symbol))
    # Advance the state only on a successful send, so a transient failure retries
    # on the next recompute instead of silently missing the alert.
    if sent:
        _write_state(strat.alert_state_json, {"position": pos, "ts": ts})
    return sent, info
