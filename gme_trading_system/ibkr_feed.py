"""
IBKR Real-Time Price Feed
Streams live GME bid/ask/last directly from TWS via ib_insync.
Used as a third data source alongside TradingView webhook and Alpaca.
Priority: TradingView > Alpaca > IBKR (INSERT OR IGNORE prevents overwrites)
"""
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DB_PATH  = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL   = "GME"

IBKR_HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT", "7496"))
IBKR_CLIENT_ID = 3  # fixed — broker uses 1, diagnostics use 2


def _write_tick(price: float, volume: float = 0.0):
    ts = datetime.utcnow().isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO price_ticks (symbol, timestamp, open, high, low, close, volume, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (SYMBOL, ts, price, price, price, price, volume, "ibkr"),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[ibkr_feed] DB write failed: {e}")


def _run_feed():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        from ib_insync import IB, Stock
    except ImportError:
        log.warning("[ibkr_feed] ib_insync not installed — IBKR feed disabled")
        return

    ib = IB()
    contract = Stock(SYMBOL, "SMART", "USD")

    while True:
        try:
            ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, readonly=True)
            log.info(f"[ibkr_feed] Connected — streaming {SYMBOL} real-time prices")

            ib.qualifyContracts(contract)
            ticker = ib.reqMktData(contract, "", False, False)

            while ib.isConnected():
                ib.sleep(5)  # poll every 5 seconds (matches TradingView alert frequency)
                last = ticker.last or ticker.close or ticker.bid or 0.0
                vol  = ticker.volume or 0.0
                if last > 0:
                    _write_tick(float(last), float(vol))
                    log.debug(f"[ibkr_feed] {SYMBOL} last=${last:.2f} vol={vol}")

            log.warning("[ibkr_feed] Disconnected — reconnecting in 10s")

        except Exception as e:
            log.warning(f"[ibkr_feed] Error: {e} — retrying in 15s")

        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

        time.sleep(15)


def start_ibkr_feed():
    t = threading.Thread(target=_run_feed, daemon=True, name="ibkr-feed")
    t.start()
    log.info("[ibkr_feed] IBKR price feed thread started (clientId=3)")
    return t
