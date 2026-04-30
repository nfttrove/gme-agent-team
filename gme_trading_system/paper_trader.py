"""
paper_trader.py — Automatic hypothetical paper trades for every signal.

When a signal fires with entry_price + TP + SL, a paper trade opens
automatically. A background job (check_and_close_open_trades) runs
every 5 min and closes trades on TP/SL first-touch against real price
ticks, or expires them at the 4h window boundary. This replaces the
manual /executed /ignored /missed loop with real outcome data the
learning pipeline consumes.

Win = TP hit first. Loss = SL hit first. Expired = neither within 4h
(closed at end-of-window price, so still produces a real PnL figure).
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

TRADE_WINDOW = timedelta(hours=4)   # mirror calibration's signal window


# ─── Schema ──────────────────────────────────────────────────────────────────


def ensure_paper_trades_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id          TEXT PRIMARY KEY,
            signal_id   TEXT NOT NULL,
            agent_name  TEXT NOT NULL,
            signal_type TEXT,
            direction   TEXT NOT NULL,   -- 'bull' or 'bear'
            entry_price REAL NOT NULL,
            stop_loss   REAL,
            take_profit REAL,
            opened_at   TEXT NOT NULL,
            closed_at   TEXT,
            exit_price  REAL,
            outcome     TEXT,            -- 'tp_hit' | 'sl_hit' | 'expired' | NULL=open
            pnl_pct     REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pt_signal  ON paper_trades(signal_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pt_outcome ON paper_trades(outcome)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pt_agent   ON paper_trades(agent_name)"
    )
    conn.commit()


# ─── Open ─────────────────────────────────────────────────────────────────────


def open_paper_trade(
    conn: sqlite3.Connection,
    signal_id: str,
    agent_name: str,
    signal_type: str,
    entry_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
) -> Optional[str]:
    """Insert an open paper trade row. Returns trade_id or None if no TP/SL."""
    if not (entry_price and take_profit and stop_loss):
        return None
    direction = "bull" if float(take_profit) > float(entry_price) else "bear"
    trade_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        ensure_paper_trades_table(conn)
        conn.execute(
            "INSERT INTO paper_trades "
            "(id, signal_id, agent_name, signal_type, direction, "
            " entry_price, stop_loss, take_profit, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, signal_id, agent_name, signal_type, direction,
             float(entry_price), float(stop_loss), float(take_profit), now),
        )
        conn.commit()
        log.info(
            f"[paper_trader] opened {trade_id[:8]} {agent_name} {direction} "
            f"entry={entry_price:.2f} tp={take_profit:.2f} sl={stop_loss:.2f}"
        )
        return trade_id
    except Exception as e:
        log.warning(f"[paper_trader] open failed: {e}")
        return None


# ─── Close-checker ────────────────────────────────────────────────────────────


def check_and_close_open_trades(db_path: str, symbol: str = "GME") -> dict:
    """Scan open paper trades against price_ticks; close on TP/SL first-touch
    or 4h expiry. Designed to run every 5 min alongside Valerie.
    Returns summary: {checked, closed, tp_hits, sl_hits, expired}."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_paper_trades_table(conn)
        open_trades = conn.execute(
            "SELECT * FROM paper_trades WHERE outcome IS NULL"
        ).fetchall()

        if not open_trades:
            return {"checked": 0, "closed": 0, "tp_hits": 0, "sl_hits": 0, "expired": 0}

        now = datetime.now(timezone.utc)
        counts = {"tp_hits": 0, "sl_hits": 0, "expired": 0}

        for trade in open_trades:
            try:
                opened_at = datetime.fromisoformat(trade["opened_at"])
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            window_end = opened_at + TRADE_WINDOW
            entry = float(trade["entry_price"])
            tp = float(trade["take_profit"]) if trade["take_profit"] else None
            sl = float(trade["stop_loss"]) if trade["stop_loss"] else None
            bull = trade["direction"] == "bull"

            # Scan ticks from open to min(now, window_end)
            scan_end = min(now, window_end)
            ticks = conn.execute(
                "SELECT close FROM price_ticks "
                "WHERE symbol=? AND timestamp > ? AND timestamp <= ? "
                "ORDER BY timestamp ASC",
                (symbol, trade["opened_at"], scan_end.isoformat()),
            ).fetchall()

            outcome: Optional[str] = None
            exit_price: Optional[float] = None

            for (price,) in ticks:
                price = float(price)
                if bull:
                    if tp is not None and price >= tp:
                        outcome, exit_price = "tp_hit", tp
                        break
                    if sl is not None and price <= sl:
                        outcome, exit_price = "sl_hit", sl
                        break
                else:
                    if tp is not None and price <= tp:
                        outcome, exit_price = "tp_hit", tp
                        break
                    if sl is not None and price >= sl:
                        outcome, exit_price = "sl_hit", sl
                        break

            # Expire at window_end if window has closed and no TP/SL hit
            if outcome is None and now >= window_end:
                end_tick = conn.execute(
                    "SELECT close FROM price_ticks "
                    "WHERE symbol=? AND timestamp <= ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (symbol, window_end.isoformat()),
                ).fetchone()
                exit_price = float(end_tick[0]) if end_tick else entry
                outcome = "expired"

            if outcome:
                if bull:
                    pnl_pct = round((exit_price - entry) / entry * 100, 4)
                else:
                    pnl_pct = round((entry - exit_price) / entry * 100, 4)
                conn.execute(
                    "UPDATE paper_trades "
                    "SET closed_at=?, exit_price=?, outcome=?, pnl_pct=? "
                    "WHERE id=?",
                    (now.isoformat(), exit_price, outcome, pnl_pct, trade["id"]),
                )
                counts[outcome + "s"] = counts.get(outcome + "s", 0) + 1

        conn.commit()
        closed = sum(counts.values())
        return {"checked": len(open_trades), "closed": closed, **counts}
    finally:
        conn.close()


# ─── Stats for standup ────────────────────────────────────────────────────────


def get_trade_stats(conn: sqlite3.Connection, days: int = 1) -> list[dict]:
    """Per-agent paper trade stats over the last N days."""
    conn.row_factory = sqlite3.Row
    ensure_paper_trades_table(conn)
    rows = conn.execute(
        """
        SELECT agent_name,
               COUNT(*)                                              AS total,
               SUM(CASE WHEN outcome IS NULL     THEN 1 ELSE 0 END) AS open,
               SUM(CASE WHEN outcome = 'tp_hit'  THEN 1 ELSE 0 END) AS tp_hits,
               SUM(CASE WHEN outcome = 'sl_hit'  THEN 1 ELSE 0 END) AS sl_hits,
               SUM(CASE WHEN outcome = 'expired' THEN 1 ELSE 0 END) AS expired,
               AVG(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct END)  AS avg_pnl,
               SUM(CASE WHEN pnl_pct IS NOT NULL THEN 1 ELSE 0 END) AS closed
        FROM paper_trades
        WHERE opened_at > datetime('now', ?)
        GROUP BY agent_name
        ORDER BY total DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]
