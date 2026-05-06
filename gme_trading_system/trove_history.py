"""
Trove forward-return tracker.

Logs every ticker that hits a Trove score >= threshold, then revisits each
entry on its 30/90/365-day anniversaries to record forward returns. This is
the only way to know if the rubric is generating real edge for *this* user
on *this* watchlist over time.

Two jobs, both safe to run daily:
  - log_daily_scores(threshold=65)  — runs the full watchlist, inserts new rows
  - resolve_forward_returns()       — fills 30/90/365d return columns when
                                       the anniversary has passed
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trove_score_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    score_date      TEXT    NOT NULL,
    score           REAL    NOT NULL,
    rating          TEXT    NOT NULL,
    pillar_a        REAL    NOT NULL,
    pillar_b        REAL    NOT NULL,
    pillar_c        REAL    NOT NULL,
    pillar_d        REAL    NOT NULL,
    insider_count   INTEGER NOT NULL DEFAULT 0,
    insider_dollars REAL    NOT NULL DEFAULT 0,
    price_at_score  REAL,
    return_30d      REAL,
    return_90d      REAL,
    return_365d     REAL,
    resolved_30d_at TEXT,
    resolved_90d_at TEXT,
    resolved_365d_at TEXT,
    UNIQUE(ticker, score_date)
);
CREATE INDEX IF NOT EXISTS idx_trove_hist_date     ON trove_score_history(score_date);
CREATE INDEX IF NOT EXISTS idx_trove_hist_ticker   ON trove_score_history(ticker);
CREATE INDEX IF NOT EXISTS idx_trove_hist_unresolved
    ON trove_score_history(score_date)
    WHERE return_30d IS NULL OR return_90d IS NULL OR return_365d IS NULL;
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _close_price(ticker: str, on_date: date) -> Optional[float]:
    """Most recent close on/before the given date. Returns None if not available."""
    try:
        import yfinance as yf
        # Pull a 7-day window ending on the date; take the last available close.
        start = (on_date - timedelta(days=7)).isoformat()
        end   = (on_date + timedelta(days=1)).isoformat()
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.debug("close-price fetch failed for %s @ %s: %s", ticker, on_date, e)
        return None


# ── Logging ───────────────────────────────────────────────────────────────────

def log_daily_scores(threshold: float = 65.0) -> dict:
    """Score the full default watchlist, insert any rows scoring >= threshold.

    Idempotent per (ticker, score_date) thanks to UNIQUE constraint.
    """
    from trove import run_screen, DEFAULT_WATCHLIST

    today = date.today().isoformat()
    results = run_screen(DEFAULT_WATCHLIST, max_tickers=len(DEFAULT_WATCHLIST))
    qualifying = [r for r in results if r["score"] >= threshold]

    inserted = 0
    skipped = 0
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_schema(conn)
        for r in qualifying:
            price = _close_price(r["ticker"], date.today())
            try:
                conn.execute(
                    """INSERT INTO trove_score_history
                       (ticker, score_date, score, rating, pillar_a, pillar_b,
                        pillar_c, pillar_d, insider_count, insider_dollars,
                        price_at_score)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["ticker"], today, r["score"], r["rating"],
                     r["pillar_A"], r["pillar_B"], r["pillar_C"], r["pillar_D"],
                     r["insider_buy_count"], r["insider_buy_dollars"], price),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1  # already logged today
        conn.commit()
    finally:
        conn.close()

    log.info("[trove_history] logged %d new (skipped %d dupes) at threshold %.1f",
             inserted, skipped, threshold)
    return {"inserted": inserted, "skipped": skipped, "threshold": threshold,
            "scored_total": len(results), "qualifying": len(qualifying)}


# ── Resolver ──────────────────────────────────────────────────────────────────

_HORIZONS = (("return_30d",  "resolved_30d_at",  30),
             ("return_90d",  "resolved_90d_at",  90),
             ("return_365d", "resolved_365d_at", 365))


def resolve_forward_returns() -> dict:
    """For every horizon column still NULL whose anniversary has passed,
    fetch the close price on that anniversary and write the return."""
    today = date.today()
    counts = {30: 0, 90: 0, 365: 0}
    failures = 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        for ret_col, res_col, days in _HORIZONS:
            cutoff = (today - timedelta(days=days)).isoformat()
            rows = conn.execute(
                f"""SELECT id, ticker, score_date, price_at_score
                    FROM trove_score_history
                    WHERE {ret_col} IS NULL
                      AND price_at_score IS NOT NULL
                      AND score_date <= ?""",
                (cutoff,),
            ).fetchall()

            for row in rows:
                anniv = (datetime.strptime(row["score_date"], "%Y-%m-%d").date()
                         + timedelta(days=days))
                price_then = _close_price(row["ticker"], anniv)
                if price_then is None or row["price_at_score"] in (None, 0):
                    failures += 1
                    continue
                ret = (price_then / row["price_at_score"]) - 1.0
                conn.execute(
                    f"""UPDATE trove_score_history
                        SET {ret_col} = ?, {res_col} = ?
                        WHERE id = ?""",
                    (ret, today.isoformat(), row["id"]),
                )
                counts[days] += 1
        conn.commit()
    finally:
        conn.close()

    log.info("[trove_history] resolved 30d=%d 90d=%d 365d=%d (failures=%d)",
             counts[30], counts[90], counts[365], failures)
    return {"resolved_30d": counts[30], "resolved_90d": counts[90],
            "resolved_365d": counts[365], "failures": failures}


# ── Reporting ─────────────────────────────────────────────────────────────────

def summarize() -> dict:
    """Quick stats — average forward return at each horizon, hit rate."""
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_schema(conn)
        out = {"total_logged": conn.execute(
            "SELECT COUNT(*) FROM trove_score_history").fetchone()[0]}
        for ret_col, _, days in _HORIZONS:
            row = conn.execute(
                f"""SELECT COUNT(*), AVG({ret_col}),
                           SUM(CASE WHEN {ret_col} > 0 THEN 1 ELSE 0 END)
                    FROM trove_score_history WHERE {ret_col} IS NOT NULL"""
            ).fetchone()
            n, avg, wins = row
            out[f"{days}d"] = {"n": n,
                                "avg_return": round(avg, 4) if avg is not None else None,
                                "hit_rate": round(wins / n, 3) if n else None}
        return out
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "log":      print(log_daily_scores())
    elif cmd == "resolve":print(resolve_forward_returns())
    else:                 print(summarize())
