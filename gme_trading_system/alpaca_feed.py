"""
Alpaca Markets real-time 1-second data feed for GME.

This runs as a background thread alongside logger_daemon.py and writes
every incoming trade/quote directly to price_ticks with source='alpaca'.

Priority model (matches the INSERT OR IGNORE/REPLACE logic in logger_daemon):
  - TradingView webhook (1-sec, paid) → INSERT OR REPLACE  ← always wins
  - Alpaca IEX stream  (1-sec, free)  → INSERT OR IGNORE   ← fills gaps
  - yfinance polling   (1-min)        → never used (watchdog alerts instead)

So Alpaca automatically backs up TradingView with zero indicator pollution —
identical timestamps, same resolution, no data-frequency mismatch.

Setup:
  1. Create a free paper account at https://alpaca.markets
  2. Go to Paper Dashboard → API Keys → generate a key pair
  3. Add ALPACA_API_KEY and ALPACA_API_SECRET to your .env
  4. ALPACA_FEED=iex (free) or sip (paid Unlimited plan)

Run standalone:
    python alpaca_feed.py          # streams GME ticks to DB until killed

Run from logger_daemon / orchestrator:
    from alpaca_feed import start_alpaca_feed
    start_alpaca_feed()            # starts a daemon thread, returns immediately
"""
import csv
import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import websocket
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_FEED   = os.getenv("ALPACA_FEED", "iex").lower()   # 'iex' or 'sip'

DB_PATH  = os.path.join(os.path.dirname(__file__), "agent_memory.db")
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "gme_ticks.csv")
SYMBOL   = "GME"

# Alpaca stream endpoints
_WS_URL = {
    "iex": "wss://stream.data.alpaca.markets/v2/iex",
    "sip": "wss://stream.data.alpaca.markets/v2/sip",
}

# 1-second candle accumulator: timestamps → {open, high, low, close, volume}
_candles: dict = defaultdict(lambda: {"o": None, "h": -1e18, "l": 1e18, "c": None, "v": 0.0})
_candle_lock = threading.Lock()


# ── DB / CSV write ─────────────────────────────────────────────────────────────

def _write_tick(ts: str, o: float, h: float, l: float, c: float, v: float):
    """
    INSERT OR IGNORE — TradingView already owns the slot if it arrived first.
    Alpaca silently fills the gap if TradingView missed it.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT OR IGNORE INTO price_ticks (symbol, timestamp, open, high, low, close, volume, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (SYMBOL, ts, o, h, l, c, v, "alpaca"),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[alpaca] DB write failed: {e}")
        return

    try:
        Path(os.path.dirname(CSV_PATH)).mkdir(parents=True, exist_ok=True)
        with open(CSV_PATH, "a", newline="") as f:
            csv.writer(f).writerow([ts, o, h, l, c, v, "alpaca"])
    except Exception:
        pass

    log.debug(f"[alpaca] {ts} | O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f} V={int(v)}")


# ── 1-second candle builder ────────────────────────────────────────────────────

def _ingest_trade(price: float, size: float, ts_str: str):
    """
    Alpaca streams individual trades (not pre-built candles).
    We bucket them into 1-second candles ourselves using the trade timestamp.
    Flush the previous second's bucket when a trade from a new second arrives.
    """
    # Truncate to second boundary: "2026-04-20T14:35:01.123456789Z" → "2026-04-20T14:35:01"
    sec_ts = ts_str[:19]

    with _candle_lock:
        old_keys = [k for k in _candles if k < sec_ts]
        for old_ts in old_keys:
            c = _candles.pop(old_ts)
            if c["o"] is not None:
                _write_tick(old_ts, c["o"], c["h"], c["l"], c["c"], c["v"])

        bar = _candles[sec_ts]
        if bar["o"] is None:
            bar["o"] = price
        bar["h"] = max(bar["h"], price)
        bar["l"] = min(bar["l"], price)
        bar["c"] = price
        bar["v"] += size


def _flush_all():
    """Call on disconnect to flush any partial candle."""
    with _candle_lock:
        for ts, c in list(_candles.items()):
            if c["o"] is not None:
                _write_tick(ts, c["o"], c["h"], c["l"], c["c"], c["v"])
        _candles.clear()


# ── WebSocket callbacks ────────────────────────────────────────────────────────

class _AlpacaStream:
    def __init__(self):
        self._ws: websocket.WebSocketApp | None = None
        self._authenticated = False
        self._reconnect_delay = 5

    def _on_message(self, ws, message: str):
        try:
            events = json.loads(message)
        except json.JSONDecodeError:
            return

        for event in events:
            msg_type = event.get("T")

            if msg_type == "connected":
                log.info("[alpaca] WebSocket connected — authenticating")
                ws.send(json.dumps({"action": "auth", "key": ALPACA_KEY, "secret": ALPACA_SECRET}))

            elif msg_type == "success" and event.get("msg") == "authenticated":
                self._authenticated = True
                log.info("[alpaca] Authenticated — subscribing to GME trades")
                ws.send(json.dumps({"action": "subscribe", "trades": [SYMBOL]}))

            elif msg_type == "subscription":
                log.info(f"[alpaca] Subscription confirmed: {event}")

            elif msg_type == "t":  # trade event
                price = float(event.get("p", 0))
                size  = float(event.get("s", 0))
                ts    = event.get("t", datetime.utcnow().isoformat())
                if price > 0:
                    _ingest_trade(price, size, ts)

            elif msg_type == "error":
                log.error(f"[alpaca] Stream error: {event}")

    def _on_error(self, ws, error):
        log.error(f"[alpaca] WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        _flush_all()
        log.warning(f"[alpaca] WebSocket closed ({close_status_code}: {close_msg}) — reconnecting in {self._reconnect_delay}s")

    def _on_open(self, ws):
        log.info(f"[alpaca] WebSocket opened ({ALPACA_FEED.upper()} feed)")
        self._authenticated = False

    def run_forever(self):
        url = _WS_URL.get(ALPACA_FEED, _WS_URL["iex"])
        while True:
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.error(f"[alpaca] run_forever exception: {e}")

            _flush_all()
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)


# ── Public API ─────────────────────────────────────────────────────────────────

_stream: _AlpacaStream | None = None
_thread: threading.Thread | None = None


def start_alpaca_feed() -> bool:
    """
    Start the Alpaca stream in a daemon thread.
    Returns True if keys are configured, False if unconfigured (no-op).

    Called by logger_daemon.py on startup alongside the Flask server.
    """
    global _stream, _thread

    if not ALPACA_KEY or not ALPACA_SECRET or ALPACA_KEY == "your-alpaca-key":
        log.warning(
            "[alpaca] ALPACA_API_KEY / ALPACA_API_SECRET not set. "
            "1-second Alpaca backup feed is disabled. "
            "TradingView webhook is the sole data source."
        )
        return False

    if _thread and _thread.is_alive():
        log.info("[alpaca] Feed already running.")
        return True

    _stream = _AlpacaStream()
    _thread = threading.Thread(target=_stream.run_forever, daemon=True, name="AlpacaFeed")
    _thread.start()
    log.info(f"[alpaca] 1-second {ALPACA_FEED.upper()} feed started for {SYMBOL}")
    return True


def stop_alpaca_feed():
    global _stream
    if _stream and _stream._ws:
        _stream._ws.close()
        _flush_all()
        log.info("[alpaca] Feed stopped.")


# ── Standalone entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ALPACA_KEY or ALPACA_KEY == "your-alpaca-key":
        print(
            "\n[ERROR] Set ALPACA_API_KEY and ALPACA_API_SECRET in your .env file.\n"
            "Get a free paper key at: https://app.alpaca.markets/paper/dashboard/overview\n"
        )
        raise SystemExit(1)

    log.info(f"[alpaca] Starting standalone feed — {ALPACA_FEED.upper()} stream for {SYMBOL}")
    started = start_alpaca_feed()
    if started:
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            stop_alpaca_feed()
