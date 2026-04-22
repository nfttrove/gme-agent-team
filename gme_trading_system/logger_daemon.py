"""
GME tick logger with multi-source data paths:

  1. PRIMARY — TradingView webhook (1-second, paid)
     TradingView POSTs each bar close to POST /tick.
     Requires a public URL: use ngrok locally, Railway in production.

  2. BACKUP 1 — Alpaca IEX stream (1-second, free)
     Fills gaps if TradingView misses a tick. Auto-enabled with API keys.

  3. BACKUP 2 — Yahoo Finance polling (5-minute, after-hours)
     Runs every 5 min, fills after-hours gaps. No setup needed.
     Watchdog only alerts during market hours (no after-hours alerts).

  4. BACKUP 3 — IBKR real-time feed (5-second, via TWS)
     Lowest priority, only writes if no other source has timestamp.

Run:
    python logger_daemon.py              # starts on port 8765
    python logger_daemon.py --port 9000  # custom port
"""
import argparse
import csv
import os
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

from alpaca_feed import start_alpaca_feed
from market_hours import is_market_open
from notifier import notify_watchdog_alert
from circuit_breaker import list_breakers

load_dotenv()

ET = ZoneInfo("America/New_York")
DB_PATH   = os.path.join(os.path.dirname(__file__), "agent_memory.db")
CSV_PATH  = os.path.join(os.path.dirname(__file__), "data", "gme_ticks.csv")
GIT_REPO  = os.getenv("GIT_REPO_PATH", "")
SYMBOL    = "GME"
PORT      = int(os.getenv("LOGGER_PORT", "8765"))

app = Flask(__name__)

# Shared: time of last webhook tick (used by fallback watchdog)
_last_webhook_ts: float = 0.0
_lock = threading.Lock()

# Prometheus metrics
AGENT_CYCLES = Counter('gme_agent_cycles_total', 'Crew kickoffs', ['name', 'status'])
AGENT_DURATION = Histogram('gme_agent_duration_seconds', 'Crew run time', ['name'])
DB_SIZE_BYTES = Gauge('gme_db_size_bytes', 'SQLite DB file size')
TICK_COUNT = Gauge('gme_price_ticks_total', 'Rows in price_ticks')
CIRCUIT_STATE = Gauge('gme_circuit_state', 'Circuit breaker state (0=closed, 1=half, 2=open)', ['service'])
CIRCUIT_FAILURES = Counter('gme_circuit_failures_total', 'CB failure count', ['service'])


# ── Database / CSV helpers ─────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_ticks (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol  TEXT    NOT NULL DEFAULT 'GME',
            timestamp TEXT  NOT NULL,
            open    REAL, high REAL, low REAL, close REAL, volume REAL,
            source  TEXT DEFAULT 'tradingview'
        );
    """)
    conn.commit()
    conn.close()


def init_csv():
    Path(os.path.dirname(CSV_PATH)).mkdir(parents=True, exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "open", "high", "low", "close", "volume", "source"])


def write_tick(ts: str, o: float, h: float, l: float, c: float, v: float, source: str = "tradingview"):
    conn = sqlite3.connect(DB_PATH)
    # INSERT OR REPLACE ensures tradingview data overwrites yfinance for same timestamp.
    # If tradingview row already exists, yfinance INSERT OR IGNORE is a no-op.
    policy = "OR REPLACE" if source == "tradingview" else "OR IGNORE"
    conn.execute(
        f"INSERT {policy} INTO price_ticks (symbol, timestamp, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (SYMBOL, ts, o, h, l, c, v, source),
    )
    conn.commit()
    conn.close()

    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow([ts, o, h, l, c, v, source])

    print(f"[tick] {ts} | O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f} V={int(v)} [{source}]")


def git_push():
    if not GIT_REPO:
        return
    try:
        import git
        repo = git.Repo(GIT_REPO)
        repo.index.add([CSV_PATH])
        repo.index.commit(f"data: {SYMBOL} tick {datetime.now(ET).isoformat()}")
        repo.remote("origin").push()
    except Exception as e:
        print(f"[git] push failed: {e}")


# ── TradingView webhook endpoint ───────────────────────────────────────────────

@app.route("/tick", methods=["POST"])
def receive_tick():
    """
    TradingView sends JSON to this endpoint on each bar close.

    Expected payload (matches the Pine Script alert template):
    {
      "symbol": "GME",
      "time":   "{{time}}",          -- Pine Script placeholder
      "open":   {{open}},
      "high":   {{high}},
      "low":    {{low}},
      "close":  {{close}},
      "volume": {{volume}}
    }
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "no JSON body"}), 400

    try:
        ts  = data.get("timestamp", data.get("time", datetime.now(ET).isoformat()))
        o   = float(data.get("open",  data.get("price", 0)))
        h   = float(data.get("high",  data.get("price", 0)))
        l   = float(data.get("low",   data.get("price", 0)))
        c   = float(data.get("close", data.get("price", 0)))
        v   = float(data.get("volume", 0))
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 422

    write_tick(ts, o, h, l, c, v, source="tradingview")

    global _last_webhook_ts
    with _lock:
        _last_webhook_ts = time.time()

    return jsonify({"status": "ok", "close": c}), 200


@app.route("/finnhub", methods=["POST"])
def finnhub_webhook():
    """
    Finnhub news/event webhook.
    Configure in Finnhub dashboard → Webhooks → URL: https://YOUR_HOST/finnhub

    Finnhub sends a list of news items:
    [{"category": "company", "datetime": 1234567890, "headline": "...",
      "source": "...", "summary": "...", "url": "...", "related": "GME"}]
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "no JSON body"}), 400

    items = data if isinstance(data, list) else [data]
    saved = 0
    conn = sqlite3.connect(DB_PATH)
    for item in items:
        try:
            ts = datetime.utcfromtimestamp(item.get("datetime", 0)).isoformat() if item.get("datetime") else datetime.now(ET).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO news_analysis (timestamp, headline, source, sentiment_score, sentiment_label, relevance_score, summary) VALUES (?,?,?,?,?,?,?)",
                (ts, item.get("headline", ""), item.get("source", "finnhub"),
                 None, "NEUTRAL", None, item.get("summary", "")),
            )
            saved += 1
        except Exception as e:
            print(f"[finnhub] row error: {e}")
    conn.commit()
    conn.close()
    print(f"[finnhub] Received {len(items)} items, saved {saved}")
    return jsonify({"status": "ok", "saved": saved}), 200


@app.route("/health", methods=["GET"])
def health():
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM price_ticks WHERE symbol=?", (SYMBOL,)).fetchone()[0]
    conn.close()
    return jsonify({"status": "ok", "tick_count": count, "symbol": SYMBOL})


def _refresh_metrics():
    """Update all gauge metrics from current state."""
    try:
        conn = sqlite3.connect(DB_PATH)
        tick_count = conn.execute("SELECT COUNT(*) FROM price_ticks WHERE symbol=?", (SYMBOL,)).fetchone()[0]
        conn.close()
        TICK_COUNT.set(tick_count)

        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        DB_SIZE_BYTES.set(db_size)

        breakers = list_breakers()
        for service, state_info in breakers.items():
            CIRCUIT_STATE.labels(service=service).set(state_info["state_code"])
            CIRCUIT_FAILURES.labels(service=service)._value.get()  # ensure label exists
    except Exception:
        pass


@app.route("/metrics", methods=["GET"])
def metrics():
    """Prometheus metrics endpoint."""
    _refresh_metrics()
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


# ── yfinance fallback watchdog ─────────────────────────────────────────────────

WEBHOOK_STALE_ALERT_S = int(os.getenv("WEBHOOK_STALE_ALERT_S", "300"))  # 5 minutes default


def _webhook_watchdog(check_interval_s: int = 60):
    """
    Monitors webhook freshness. If no tick received for WEBHOOK_STALE_ALERT_S seconds
    DURING MARKET HOURS, logs a CRITICAL alert.

    NOTE: No alerts during after-hours since no data is expected.
    Yahoo Finance fallback fills after-hours gaps if needed.
    """
    print(f"[watchdog] Webhook watchdog started (alerts if silent for {WEBHOOK_STALE_ALERT_S}s during market hours)")
    alerted = False

    while True:
        time.sleep(check_interval_s)

        with _lock:
            age = time.time() - _last_webhook_ts

        if age < WEBHOOK_STALE_ALERT_S:
            if alerted:
                print(f"[watchdog] Webhook resumed — data flow restored.")
                alerted = False
            continue

        # Skip alerts during after-hours — Yahoo Finance handles it
        if not is_market_open():
            if alerted:
                print(f"[watchdog] After-hours — no alert needed")
                alerted = False
            continue

        if not alerted:
            msg = (
                f"CRITICAL: TradingView webhook silent for {int(age)}s during market hours. "
                f"Check ngrok/Railway tunnel and TradingView alert configuration."
            )
            print(f"[watchdog] {msg}")
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "INSERT INTO agent_logs (agent_name, timestamp, task_type, content, status) VALUES (?,?,?,?,?)",
                    ("WebhookWatchdog", datetime.now(ET).isoformat(), "alert", msg, "error"),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
            notify_watchdog_alert(int(age))
            alerted = True


# ── Entry point ────────────────────────────────────────────────────────────────

def start(port: int = PORT):
    init_db()
    init_csv()

    # Webhook watchdog — alerts if TradingView goes silent during market hours only
    t = threading.Thread(target=_webhook_watchdog, daemon=True)
    t.start()

    # Alpaca 1-second backup feed — fills gaps if TradingView misses a tick
    start_alpaca_feed()

    # Yahoo Finance poller — after-hours and outage fallback (free, no API key)
    try:
        from yahoo_finance_feed import start_yahoo_feed
        start_yahoo_feed()
    except Exception as e:
        print(f"[yahoo_feed] Could not start: {e}")

    # IBKR real-time feed — third source, uses INSERT OR IGNORE (lowest priority)
    try:
        from ibkr_feed import start_ibkr_feed
        start_ibkr_feed()
    except Exception as e:
        print(f"[ibkr_feed] Could not start: {e}")

    print(f"""
╔══════════════════════════════════════════════════════╗
║  GME Tick Logger                                     ║
║  PRIMARY:  TradingView webhook (5-sec, paid)         ║
║  BACKUP 1: Alpaca IEX stream   (1-sec, free)         ║
║  BACKUP 2: Yahoo Finance       (5-min, after-hours)  ║
║  BACKUP 3: IBKR real-time feed (5-sec, via TWS)      ║
║  Webhook:  POST http://localhost:{port}/tick          ║
║  Health:   GET  http://localhost:{port}/health        ║
║  Priority: TradingView > Alpaca > Yahoo > IBKR       ║
╚══════════════════════════════════════════════════════╝
    """)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    start(args.port)
