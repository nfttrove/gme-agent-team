#!/usr/bin/env python3
"""
log_trades_from_db.py — Export completed trades from agent_memory.db to episodic memory.

Run this once to backfill episodic memory with existing trades:
  python3 .agent/memory/log_trades_from_db.py

This creates trades.jsonl from the trades table in agent_memory.db (if it exists).
"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime

AGENT_ROOT = Path(__file__).parent.parent
EPISODIC_DIR = AGENT_ROOT / "memory" / "episodic"
DB_PATH = AGENT_ROOT.parent / "gme_trading_system" / "agent_memory.db"

def log_trades_from_db():
    """Export trades from SQLite to episodic JSON."""
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

    EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
    trades_file = EPISODIC_DIR / "trades.jsonl"

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Try to fetch from trades table (adjust schema as needed)
        rows = conn.execute("""
            SELECT * FROM trades
            WHERE status='completed' OR status='closed'
            ORDER BY closed_at DESC
        """).fetchall()

        if not rows:
            print("No completed trades found in database.")
            conn.close()
            return

        with open(trades_file, "a") as f:
            for row in rows:
                trade = {
                    "timestamp": row.get("closed_at") or datetime.utcnow().isoformat() + "Z",
                    "agent": "Trader",
                    "action": "sell_put" if "put" in row.get("side", "").lower() else row.get("action", "trade"),
                    "symbol": row.get("symbol", "GME"),
                    "entry_price": float(row.get("entry_price", 0)),
                    "exit_price": float(row.get("exit_price", 0)),
                    "pnl": float(row.get("realized_pnl", 0)),
                    "outcome": "profitable" if float(row.get("realized_pnl", 0)) > 0 else "loss",
                    "reason": row.get("reason", "strategic trade"),
                    "tags": ["manual-backfill"],
                }
                f.write(json.dumps(trade) + "\n")

        print(f"✓ Logged {len(rows)} trades to {trades_file}")
        conn.close()

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    log_trades_from_db()
