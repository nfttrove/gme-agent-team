"""
Data-freshness diagnostic for the GME agent team.

Answers a single question: do the tables our agents read from agree with the
live tick stream right now? If not, any agent reading the stale table will
produce confident-but-wrong narratives (see: Trendy calling a breakout
"sideways" on 2026-04-22 because daily_candles had no row for today).

Run:
    python -m gme_trading_system.data_freshness
    python gme_trading_system/data_freshness.py

Exits non-zero when any check fails, so it can be wired into pytest or a
pre-signal gate.
"""
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
ET = ZoneInfo("America/New_York")
SYMBOL = "GME"


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def check(db_path: str = DB_PATH, today: str | None = None) -> list[tuple[str, bool, str]]:
    """Return list of (name, ok, detail). Pure — no printing."""
    today = today or _today_et()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out: list[tuple[str, bool, str]] = []

    # 1. Live ticks exist for today
    tick = conn.execute(
        "SELECT COUNT(*) n, MIN(low) lo, MAX(high) hi, MAX(timestamp) last_ts "
        "FROM price_ticks WHERE symbol=? AND timestamp LIKE ?",
        (SYMBOL, f"{today}%"),
    ).fetchone()
    ticks_ok = tick["n"] > 0
    out.append((
        "price_ticks_today",
        ticks_ok,
        f"{tick['n']} ticks, range ${tick['lo']}-${tick['hi']}, last={tick['last_ts']}"
        if ticks_ok else "no ticks for today",
    ))

    # 2. Today's daily_candle exists
    candle = conn.execute(
        "SELECT high, low, close FROM daily_candles WHERE symbol=? AND date=?",
        (SYMBOL, today),
    ).fetchone()
    candle_ok = candle is not None
    out.append((
        "daily_candle_today",
        candle_ok,
        f"H:{candle['high']} L:{candle['low']} C:{candle['close']}"
        if candle_ok else f"no daily_candles row for {today} — agents reading this table see yesterday",
    ))

    # 3. If both exist, candle must bracket the live range
    if ticks_ok and candle_ok:
        agrees = candle["high"] >= tick["hi"] and candle["low"] <= tick["lo"]
        out.append((
            "candle_matches_ticks",
            agrees,
            f"candle [{candle['low']}, {candle['high']}] vs ticks [{tick['lo']}, {tick['hi']}]"
            if not agrees else "candle envelopes live tick range",
        ))

    # 4. Critical agents have recent successful runs today
    for agent in ("Valerie", "Chatty", "Synthesis", "Trendy"):
        row = conn.execute(
            "SELECT MAX(timestamp) last_ok FROM agent_logs "
            "WHERE agent_name=? AND status='ok' AND substr(timestamp,1,10)=?",
            (agent, today),
        ).fetchone()
        last = row["last_ok"]
        out.append((
            f"agent_{agent.lower()}_ran_today",
            last is not None,
            f"last ok run: {last}" if last else f"no successful {agent} run today",
        ))

    conn.close()
    return out


def main() -> int:
    results = check()
    width = max(len(name) for name, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        mark = "OK " if ok else "BAD"
        print(f"[{mark}] {name.ljust(width)}  {detail}")
        if not ok:
            failed += 1
    print()
    if failed:
        print(f"{failed} check(s) failed — agents reading stale data will fabricate narratives.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
