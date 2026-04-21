"""
Yahoo Finance fallback feed for GME — fills data gaps after-hours and during outages.

Runs as a background thread and polls Yahoo Finance every 5 minutes for latest price.
Uses INSERT OR IGNORE so TradingView/Alpaca data takes priority.

Setup:
  No API key needed — yfinance is free and doesn't require registration.

Run from logger_daemon:
    from yahoo_finance_feed import start_yahoo_feed
    start_yahoo_feed()
"""
import csv
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    yf = None

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "gme_ticks.csv")
SYMBOL = "GME"
POLL_INTERVAL_S = int(os.getenv("YAHOO_POLL_INTERVAL_S", "300"))  # 5 minutes default


def _write_tick(ts: str, c: float):
    """Write a price tick from Yahoo Finance (low priority — allows overwrite from primary sources)."""
    if not yf:
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT OR IGNORE INTO price_ticks (symbol, timestamp, close, source) "
            "VALUES (?,?,?,?)",
            (SYMBOL, ts, c, "yahoo"),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[yahoo] DB write failed: {e}")
        return

    try:
        Path(os.path.dirname(CSV_PATH)).mkdir(parents=True, exist_ok=True)
        with open(CSV_PATH, "a", newline="") as f:
            csv.writer(f).writerow([ts, None, None, None, c, 0, "yahoo"])
    except Exception:
        pass

    log.debug(f"[yahoo] {ts} | Close: ${c:.2f}")


def _fetch_latest():
    """Fetch latest GME price from Yahoo Finance."""
    if not yf:
        log.warning("[yahoo] yfinance not installed — skipping")
        return

    try:
        ticker = yf.Ticker(SYMBOL)
        # Get the most recent data point
        hist = ticker.history(period="1d", interval="1m")
        if hist is None or hist.empty:
            log.debug("[yahoo] No recent data available")
            return

        # Get the last (most recent) row
        latest = hist.iloc[-1]
        close_price = latest.get("Close")

        if close_price and close_price > 0:
            ts = hist.index[-1].isoformat() if hasattr(hist.index[-1], "isoformat") else datetime.utcnow().isoformat()
            _write_tick(ts, float(close_price))
            log.info(f"[yahoo] GME ${close_price:.2f} at {ts}")
    except Exception as e:
        log.warning(f"[yahoo] Fetch failed: {e}")


def _yahoo_poller():
    """Poll Yahoo Finance every POLL_INTERVAL_S seconds."""
    log.info(f"[yahoo] Poller started (checks every {POLL_INTERVAL_S}s)")

    while True:
        try:
            _fetch_latest()
        except Exception as e:
            log.error(f"[yahoo] Poller error: {e}")

        time.sleep(POLL_INTERVAL_S)


# Public API
_thread: threading.Thread | None = None


def start_yahoo_feed() -> bool:
    """Start the Yahoo Finance poller in a daemon thread."""
    global _thread

    if not yf:
        try:
            import yfinance
            log.info("[yahoo] yfinance installed")
        except ImportError:
            log.warning(
                "[yahoo] yfinance not installed. Run: pip install yfinance\n"
                "Yahoo Finance fallback feed disabled."
            )
            return False

    if _thread and _thread.is_alive():
        log.info("[yahoo] Poller already running.")
        return True

    _thread = threading.Thread(target=_yahoo_poller, daemon=True, name="YahooFeed")
    _thread.start()
    log.info(f"[yahoo] Yahoo Finance fallback feed started for {SYMBOL}")
    return True


# Standalone entry point
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if not yf:
        print("[ERROR] yfinance not installed. Run: pip install yfinance")
        raise SystemExit(1)

    log.info("[yahoo] Starting standalone poller")
    start_yahoo_feed()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("[yahoo] Stopped")
