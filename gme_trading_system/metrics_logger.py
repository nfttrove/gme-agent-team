"""
Structured metrics logger.

Records per-cycle timing, LLM usage, prediction accuracy, and trade P&L
into agent_logs and a dedicated metrics.jsonl file for easy analysis.

Usage (from orchestrator or run_single_agent):
    from metrics_logger import MetricsLogger
    ml = MetricsLogger()
    with ml.cycle("daily_trend"):
        result = crew.kickoff()
    ml.record_prediction("1h", predicted=22.10, confidence=0.68)
    ml.snapshot()   # prints a one-line summary to stdout
"""
import json
import os
import sqlite3
import time
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
METRICS_FILE = os.path.join(os.path.dirname(__file__), "metrics.jsonl")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class MetricsLogger:
    def __init__(self):
        self._cycle_start: float | None = None
        self._cycle_name: str = ""
        self._session: dict = {
            "cycles": 0,
            "errors": 0,
            "total_duration_s": 0.0,
            "fallbacks": 0,
        }

    # ── Cycle timing ──────────────────────────────────────────────────────────

    @contextmanager
    def cycle(self, name: str):
        """Context manager that times a crew cycle and logs it."""
        self._cycle_name = name
        start = time.perf_counter()
        log.info(f"[metrics] Starting cycle: {name}")
        error = None
        try:
            yield
        except Exception as e:
            error = e
            self._session["errors"] += 1
            self._write_log(name, f"ERROR: {e}", "error")
            raise
        finally:
            duration = round(time.perf_counter() - start, 2)
            self._session["cycles"] += 1
            self._session["total_duration_s"] += duration
            status = "error" if error else "ok"
            log.info(f"[metrics] {name} finished in {duration}s — status={status}")
            self._append_jsonl({
                "event": "cycle",
                "name": name,
                "duration_s": duration,
                "status": status,
                "ts": datetime.now().isoformat(),
            })
            self._write_log(name, f"duration={duration}s status={status}", status)

    # ── Prediction tracking ───────────────────────────────────────────────────

    def record_prediction(self, horizon: str, predicted: float, confidence: float, reasoning: str = ""):
        """Log a futurist prediction to the DB."""
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO predictions (timestamp, horizon, predicted_price, confidence, reasoning) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), horizon, predicted, confidence, reasoning),
        )
        conn.commit()
        conn.close()
        log.info(f"[metrics] Prediction {horizon}: ${predicted:.2f} @ {confidence:.0%}")

    def update_prediction_accuracy(self, horizon: str, actual: float):
        """Once the real price is known, fill in actual_price and error_pct."""
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id, predicted_price FROM predictions WHERE horizon=? AND actual_price IS NULL "
            "ORDER BY timestamp DESC LIMIT 1",
            (horizon,),
        ).fetchone()
        if row:
            pred_id, predicted = row
            error_pct = round((actual - predicted) / predicted * 100, 4)
            conn.execute(
                "UPDATE predictions SET actual_price=?, error_pct=? WHERE id=?",
                (actual, error_pct, pred_id),
            )
            conn.commit()
            log.info(f"[metrics] {horizon} accuracy: predicted={predicted:.2f} actual={actual:.2f} error={error_pct:+.2f}%")
        conn.close()

    # ── Trade tracking ────────────────────────────────────────────────────────

    def record_trade(self, action: str, quantity: float, entry_price: float,
                     stop_loss: float, take_profit: float, confidence: float,
                     status: str = "pending", notes: str = "",
                     order_id: str | None = None) -> tuple[int, str]:
        """Insert a trade decision and return (row_id, order_id).
        order_id is a UUID4 that prevents double-execution on orchestrator restart."""
        oid = order_id or str(uuid.uuid4())
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Idempotency check: if this order_id already exists, return existing row
        existing = cur.execute(
            "SELECT id FROM trade_decisions WHERE order_id=?", (oid,)
        ).fetchone()
        if existing:
            conn.close()
            log.warning(f"[metrics] Duplicate order_id {oid} — skipping insertion")
            return existing[0], oid

        cur.execute(
            """INSERT INTO trade_decisions
               (order_id, timestamp, action, symbol, quantity, entry_price, stop_loss, take_profit,
                confidence, status, paper_trade, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
            (oid, datetime.now().isoformat(), action, "GME", quantity, entry_price,
             stop_loss, take_profit, confidence, status, notes),
        )
        trade_id = cur.lastrowid
        conn.commit()
        conn.close()
        log.info(f"[metrics] Trade logged: {action} {quantity} GME @ {entry_price} [{status}] id={oid}")
        return trade_id, oid

    def close_trade(self, trade_id: int, exit_price: float):
        """Mark a paper trade as closed and compute P&L."""
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT action, quantity, entry_price FROM trade_decisions WHERE id=?", (trade_id,)
        ).fetchone()
        if row:
            action, qty, entry = row
            pnl = (exit_price - entry) * qty if action == "BUY" else (entry - exit_price) * qty
            conn.execute(
                "UPDATE trade_decisions SET exit_price=?, pnl=?, status='closed' WHERE id=?",
                (exit_price, round(pnl, 4), trade_id),
            )
            conn.commit()
            log.info(f"[metrics] Trade {trade_id} closed @ {exit_price:.2f} | P&L: ${pnl:+.2f}")
        conn.close()

    # ── Fallback tracking ─────────────────────────────────────────────────────

    def record_fallback(self, agent_role: str, from_model: str, to_model: str):
        self._session["fallbacks"] += 1
        self._append_jsonl({
            "event": "fallback",
            "agent": agent_role,
            "from": from_model,
            "to": to_model,
            "ts": datetime.now().isoformat(),
        })
        log.warning(f"[metrics] Fallback: {agent_role} switched {from_model} → {to_model}")

    # ── Summary ───────────────────────────────────────────────────────────────

    def snapshot(self):
        """Print a one-line session summary."""
        s = self._session
        avg = round(s["total_duration_s"] / max(s["cycles"], 1), 1)
        log.info(
            f"[metrics] SESSION — cycles={s['cycles']} errors={s['errors']} "
            f"fallbacks={s['fallbacks']} avg_duration={avg}s"
        )
        self._trade_summary()

    def _trade_summary(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT COUNT(*), SUM(pnl), AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END) "
                "FROM trade_decisions WHERE paper_trade=1 AND status='closed'"
            ).fetchone()
            conn.close()
            if row and row[0]:
                log.info(f"[metrics] PAPER TRADES — count={row[0]} total_pnl=${row[1]:.2f} win_rate={row[2]:.0%}")
        except Exception:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _write_log(self, agent: str, content: str, status: str):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO agent_logs (agent_name, timestamp, task_type, content, status) VALUES (?,?,?,?,?)",
                (agent, datetime.now().isoformat(), "metrics", content, status),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _append_jsonl(self, record: dict):
        try:
            with open(METRICS_FILE, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass
