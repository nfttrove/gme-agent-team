"""
GME tick logger with two data paths:

  1. PRIMARY — TradingView webhook (1-second candles from your paid account)
     TradingView POSTs each bar close to POST /tick on this server.
     Requires a public URL: use ngrok locally, Railway in production.

  2. FALLBACK — yfinance polling (1-minute candles, free)
     Runs automatically if no webhook data arrives for > 5 minutes.

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
from pathlib import Path

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from alpaca_feed import start_alpaca_feed
from notifier import notify_watchdog_alert

load_dotenv()

DB_PATH   = os.path.join(os.path.dirname(__file__), "agent_memory.db")
CSV_PATH  = os.path.join(os.path.dirname(__file__), "data", "gme_ticks.csv")
GIT_REPO  = os.getenv("GIT_REPO_PATH", "")
SYMBOL    = "GME"
PORT      = int(os.getenv("LOGGER_PORT", "8765"))

app = Flask(__name__)

# Shared: time of last webhook tick (used by fallback watchdog)
_last_webhook_ts: float = 0.0
_lock = threading.Lock()


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
        repo.index.commit(f"data: {SYMBOL} tick {datetime.now().isoformat()}")
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
        ts  = data.get("timestamp", data.get("time", datetime.utcnow().isoformat()))
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
            ts = datetime.utcfromtimestamp(item.get("datetime", 0)).isoformat() if item.get("datetime") else datetime.utcnow().isoformat()
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


# ── yfinance fallback watchdog ─────────────────────────────────────────────────

WEBHOOK_STALE_ALERT_S = int(os.getenv("WEBHOOK_STALE_ALERT_S", "300"))  # 5 minutes default


def _webhook_watchdog(check_interval_s: int = 60):
    """
    Monitors webhook freshness. If no tick received for WEBHOOK_STALE_ALERT_S seconds,
    logs a CRITICAL alert and writes a system error to agent_logs.

    NOTE: We do NOT fall back to yfinance silently — mixed-frequency data
    corrupts indicators. An alert fires instead so the operator can intervene.
    """
    print(f"[watchdog] Webhook watchdog started (alerts if silent for {WEBHOOK_STALE_ALERT_S}s)")
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

        if not alerted:
            msg = (
                f"CRITICAL: TradingView webhook silent for {int(age)}s. "
                f"No fallback activated — data frequency mismatch would corrupt indicators. "
                f"Check ngrok/Railway tunnel and TradingView alert configuration."
            )
            print(f"[watchdog] {msg}")
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "INSERT INTO agent_logs (agent_name, timestamp, task_type, content, status) VALUES (?,?,?,?,?)",
                    ("WebhookWatchdog", datetime.now().isoformat(), "alert", msg, "error"),
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

    # Webhook watchdog — alerts if TradingView goes silent (no silent degradation)
    t = threading.Thread(target=_webhook_watchdog, daemon=True)
    t.start()

    # Alpaca 1-second backup feed — fills gaps if TradingView misses a tick
    start_alpaca_feed()

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
║  BACKUP 2: IBKR real-time feed (5-sec, via TWS)      ║
║  Webhook:  POST http://localhost:{port}/tick          ║
║  Health:   GET  http://localhost:{port}/health        ║
║  Priority: TradingView > Alpaca > IBKR               ║
╚══════════════════════════════════════════════════════╝
    """)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    start(args.port)
