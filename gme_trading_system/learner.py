"""
Agent Learning System — post-market debrief and continuous improvement.

Runs two learning sessions:
  1. Daily Debrief (4:30 PM ET) — score predictions vs actuals, compute agent metrics
  2. Weekly Review (Fridays 5:00 PM ET) — Boss + Memoria propose strategy threshold adjustments

All parameter changes require Boss sign-off and are logged to strategy_history
so they are fully auditable and reversible.

Usage:
    from learner import AgentLearner
    learner = AgentLearner()
    learner.post_market_debrief()   # called by orchestrator at 4:30 PM ET
    learner.weekly_strategy_review() # called by orchestrator Fridays at 5 PM ET
"""
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from notifier import notify_daily_summary

log = logging.getLogger(__name__)

DB_PATH       = os.path.join(os.path.dirname(__file__), "agent_memory.db")
STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "strategy.json")

# Maximum allowed single-step change per parameter (guardrail — prevents runaway adaptation)
_MAX_DELTA = {
    "long_entry.rsi14":              5.0,   # RSI threshold ± 5 points max per review
    "short_entry.rsi14":             5.0,
    "exit_conditions.long.hard_stop_pct":   0.005,  # stop ± 0.5% max
    "exit_conditions.short.hard_stop_pct":  0.005,
    "exit_conditions.long.take_profit_pct": 0.01,
    "exit_conditions.short.take_profit_pct": 0.01,
    "risk.max_position_pct":         0.005,
}

# Boss won't approve a change that degrades win rate below this floor
_MIN_WIN_RATE = 0.40


class AgentLearner:
    def __init__(self):
        self._ensure_tables()

    def _ensure_tables(self):
        """Idempotent — schema migration for new tables if they don't exist yet."""
        conn = sqlite3.connect(DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS performance_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                sample_size INTEGER DEFAULT 0,
                notes TEXT,
                UNIQUE(date, agent_name, metric)
            );
            CREATE TABLE IF NOT EXISTS strategy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                parameter TEXT NOT NULL,
                old_value REAL,
                new_value REAL,
                reason TEXT,
                approved_by TEXT DEFAULT 'Boss',
                reverted INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS learning_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                session_type TEXT NOT NULL,
                summary TEXT,
                changes_made INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ok'
            );
        """)
        conn.commit()
        conn.close()

    # ── Daily debrief ──────────────────────────────────────────────────────────

    def post_market_debrief(self):
        """
        4:30 PM ET — market just closed.

        1. Fetch today's actual GME close from daily_candles.
        2. Fill in actual_price on any open predictions for today.
        3. Score each agent's prediction accuracy.
        4. Score trade P&L for the day.
        5. Write performance_scores rows.
        6. Log summary to learning_sessions.
        """
        today = date.today().isoformat()
        log.info(f"[learner] === Daily debrief for {today} ===")

        actual_close = self._get_actual_close(today)
        if actual_close is None:
            log.warning(f"[learner] No daily_candles row for {today} — debrief skipped (aggregator may not have run yet)")
            self._log_session("daily_debrief", f"skipped — no close price for {today}", 0, "skipped")
            return

        log.info(f"[learner] Actual GME close: ${actual_close:.2f}")

        # Fill in prediction accuracy
        filled = self._fill_prediction_actuals(today, actual_close)
        log.info(f"[learner] Updated {filled} prediction(s) with actual close")

        # Score predictions
        pred_metrics = self._score_predictions(today)

        # Score trades
        trade_metrics = self._score_trades(today)

        # Persist
        scores_written = 0
        for agent_name, metric, value, sample_size, notes in pred_metrics + trade_metrics:
            self._upsert_score(today, agent_name, metric, value, sample_size, notes)
            scores_written += 1

        summary = (
            f"Close=${actual_close:.2f} | "
            f"Predictions scored={filled} | "
            f"Scores written={scores_written}"
        )
        log.info(f"[learner] {summary}")
        self._log_session("daily_debrief", summary, scores_written)

        # Push daily summary to Telegram
        pnl_row = self._get_today_pnl(today)
        notify_daily_summary(
            pnl=pnl_row.get("total_pnl", 0.0),
            win_rate=pnl_row.get("win_rate", 0.0),
            trades=pnl_row.get("trades", 0),
            pred_error_pct=sum(abs(e[1]) for e in [(None, r) for r in []]) or 0.0,
            gme_close=actual_close,
        )

    def _get_actual_close(self, date_str: str) -> float | None:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT close FROM daily_candles WHERE symbol='GME' AND date=? ORDER BY id DESC LIMIT 1",
            (date_str,),
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def _fill_prediction_actuals(self, date_str: str, actual: float) -> int:
        """Fill actual_price on predictions timestamped today that are still NULL."""
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "UPDATE predictions SET actual_price=?, error_pct=ROUND((? - predicted_price)/predicted_price*100, 4) "
            "WHERE actual_price IS NULL AND timestamp LIKE ?",
            (actual, actual, f"{date_str}%"),
        )
        count = cur.rowcount
        conn.commit()
        conn.close()
        return count

    def _score_predictions(self, date_str: str) -> list:
        """Return list of (agent, metric, value, sample_size, notes) tuples."""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT horizon, ABS(error_pct), error_pct FROM predictions "
            "WHERE timestamp LIKE ? AND actual_price IS NOT NULL",
            (f"{date_str}%",),
        ).fetchall()
        conn.close()

        if not rows:
            return []

        # Aggregate: average absolute error across all horizons
        abs_errors = [r[1] for r in rows]
        avg_abs_err = round(sum(abs_errors) / len(abs_errors), 4)
        return [
            ("Futurist", "prediction_error_pct", avg_abs_err, len(rows),
             f"horizons={[r[0] for r in rows]}"),
        ]

    def _score_trades(self, date_str: str) -> list:
        """Score today's closed paper trades."""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT pnl FROM trade_decisions "
            "WHERE timestamp LIKE ? AND status='closed' AND paper_trade=1",
            (f"{date_str}%",),
        ).fetchall()
        conn.close()

        if not rows:
            return []

        pnls = [r[0] for r in rows if r[0] is not None]
        if not pnls:
            return []

        wins = sum(1 for p in pnls if p > 0)
        win_rate = round(wins / len(pnls), 4)
        avg_pnl  = round(sum(pnls) / len(pnls), 4)

        return [
            ("Futurist",   "win_rate", win_rate, len(pnls), f"trades={len(pnls)}"),
            ("Trader Joe", "avg_pnl",  avg_pnl,  len(pnls), f"total_pnl=${sum(pnls):.2f}"),
        ]

    def _upsert_score(self, date_str, agent, metric, value, sample_size, notes):
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO performance_scores (date, agent_name, metric, value, sample_size, notes) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(date, agent_name, metric) DO UPDATE SET "
            "value=excluded.value, sample_size=excluded.sample_size, notes=excluded.notes",
            (date_str, agent, metric, value, sample_size, notes),
        )
        conn.commit()
        conn.close()

    # ── Weekly strategy review ─────────────────────────────────────────────────

    def weekly_strategy_review(self):
        """
        Fridays 5:00 PM ET — Boss + Memoria review trailing performance.

        1. Pull 7-day win rate and average prediction error.
        2. For each tracked parameter, check if performance justifies a tweak.
        3. Apply guardrails: max delta, minimum win rate floor.
        4. Boss approves (or rejects). Changes written to strategy.json + strategy_history.
        """
        today = date.today().isoformat()
        log.info(f"[learner] === Weekly strategy review ({today}) ===")

        window_start = (date.today() - timedelta(days=7)).isoformat()
        win_rate = self._trailing_win_rate(window_start)
        avg_err  = self._trailing_prediction_error(window_start)

        log.info(f"[learner] 7-day win_rate={win_rate:.1%}  avg_pred_error={avg_err:.2f}%")

        if win_rate is None:
            log.info("[learner] Insufficient trade data for weekly review — skipping parameter adaptation")
            self._log_session("weekly_review", "skipped — no closed trades in window", 0, "skipped")
            return

        proposals = self._generate_proposals(win_rate, avg_err)
        approved  = self._boss_approval(proposals, win_rate)

        changes = 0
        for param, old_val, new_val, reason in approved:
            self._apply_change(param, old_val, new_val, reason)
            changes += 1

        summary = (
            f"win_rate={win_rate:.1%} avg_err={avg_err:.2f}% | "
            f"proposals={len(proposals)} approved={changes}"
        )
        log.info(f"[learner] {summary}")
        self._log_session("weekly_review", summary, changes)

    def _trailing_win_rate(self, since: str) -> float | None:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT pnl FROM trade_decisions WHERE timestamp >= ? AND status='closed' AND paper_trade=1",
            (since,),
        ).fetchall()
        conn.close()
        pnls = [r[0] for r in rows if r[0] is not None]
        if len(pnls) < 3:  # need at least 3 trades for meaningful signal
            return None
        return sum(1 for p in pnls if p > 0) / len(pnls)

    def _trailing_prediction_error(self, since: str) -> float:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT AVG(ABS(error_pct)) FROM predictions WHERE timestamp >= ? AND actual_price IS NOT NULL",
            (since,),
        ).fetchone()
        conn.close()
        return round(row[0] or 0.0, 4)

    def _generate_proposals(self, win_rate: float, avg_err: float) -> list:
        """
        Rule-based proposals. Each is (param_path, old_value, new_value, reason).

        Rules:
          - Win rate < 45% for 2+ weeks → relax RSI entry thresholds (fewer trades, higher quality)
          - Win rate > 65% for 2+ weeks → tighten take-profit to lock in more gains
          - Prediction error > 5% → widen stop loss slightly to avoid premature exits
        """
        strategy = self._load_strategy()
        proposals = []

        rsi14_long  = strategy["long_entry"]["conditions"][3]["value"]   # RSI(14) ≤ X
        rsi14_short = strategy["short_entry"]["conditions"][3]["value"]  # RSI(14) ≥ X
        stop_long   = strategy["exit_conditions"]["long"]["hard_stop_pct"]
        tp_long     = strategy["exit_conditions"]["long"]["take_profit_pct"]

        # Poor win rate → be more selective (tighten RSI long entry from 45 → 40)
        if win_rate < 0.45:
            new_rsi = max(30.0, rsi14_long - 5.0)
            if abs(new_rsi - rsi14_long) > 0:
                proposals.append((
                    "long_entry.rsi14", rsi14_long, new_rsi,
                    f"win_rate={win_rate:.1%} < 45% — tighten long RSI threshold for higher-quality entries"
                ))
            new_rsi_s = min(70.0, rsi14_short + 5.0)
            if abs(new_rsi_s - rsi14_short) > 0:
                proposals.append((
                    "short_entry.rsi14", rsi14_short, new_rsi_s,
                    f"win_rate={win_rate:.1%} < 45% — tighten short RSI threshold"
                ))

        # Strong win rate → lock in more gains by tightening TP
        if win_rate > 0.65:
            new_tp = min(0.12, tp_long + 0.01)
            if abs(new_tp - tp_long) > 1e-9:
                proposals.append((
                    "exit_conditions.long.take_profit_pct", tp_long, new_tp,
                    f"win_rate={win_rate:.1%} > 65% — extend take-profit target"
                ))

        # High prediction error → widen stop to avoid premature exit on noise
        if avg_err > 5.0:
            new_stop = min(0.06, stop_long + 0.005)
            if abs(new_stop - stop_long) > 1e-9:
                proposals.append((
                    "exit_conditions.long.hard_stop_pct", stop_long, new_stop,
                    f"avg_pred_error={avg_err:.2f}% > 5% — widen stop to tolerate signal noise"
                ))

        return proposals

    def _boss_approval(self, proposals: list, current_win_rate: float) -> list:
        """
        Boss guardrails — deterministic sign-off rules (no LLM call needed for safety).

        A proposal is approved if:
          1. The delta is within _MAX_DELTA for that parameter.
          2. The change doesn't further penalize an already bad win rate.
          3. New value is within sensible absolute bounds.
        """
        approved = []
        for param, old_val, new_val, reason in proposals:
            delta = abs(new_val - old_val)

            if delta > _MAX_DELTA.get(param, 99):
                log.warning(f"[boss] REJECTED {param}: delta={delta:.4f} exceeds max={_MAX_DELTA.get(param)}")
                continue

            if current_win_rate < _MIN_WIN_RATE:
                log.warning(f"[boss] REJECTED {param}: win_rate={current_win_rate:.1%} below floor — no changes until performance improves")
                continue

            # Absolute value sanity bounds
            if "rsi" in param and not (20 <= new_val <= 80):
                log.warning(f"[boss] REJECTED {param}: new_value={new_val} out of RSI bounds [20,80]")
                continue
            if "pct" in param and not (0.005 <= new_val <= 0.20):
                log.warning(f"[boss] REJECTED {param}: new_value={new_val} out of pct bounds [0.5%,20%]")
                continue

            log.info(f"[boss] APPROVED {param}: {old_val} → {new_val} | {reason}")
            approved.append((param, old_val, new_val, reason))

        return approved

    def _apply_change(self, param: str, old_val: float, new_val: float, reason: str):
        """Write the change to strategy.json and log to strategy_history."""
        strategy = self._load_strategy()
        keys = param.split(".")

        # Navigate nested dict and apply change
        node = strategy
        for k in keys[:-1]:
            if k.isdigit():
                node = node[int(k)]
            else:
                node = node[k]
        leaf = keys[-1]

        # Special case: long_entry.rsi14 and short_entry.rsi14 are inside conditions list
        if leaf == "rsi14" and isinstance(node, dict) and "conditions" in node:
            for cond in node["conditions"]:
                if cond.get("indicator") == "rsi14":
                    cond["value"] = new_val
                    break
        elif leaf.isdigit():
            node[int(leaf)] = new_val
        else:
            node[leaf] = new_val

        with open(STRATEGY_PATH, "w") as f:
            json.dump(strategy, f, indent=2)

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO strategy_history (timestamp, parameter, old_value, new_value, reason) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), param, old_val, new_val, reason),
        )
        conn.commit()
        conn.close()
        log.info(f"[learner] Applied: {param} = {new_val} (was {old_val})")

    # ── Revert last change ─────────────────────────────────────────────────────

    def revert_last_change(self):
        """
        Emergency revert — undoes the most recent approved strategy change.
        Call from orchestrator or manually if live performance degrades sharply.
        """
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id, parameter, old_value, new_value FROM strategy_history "
            "WHERE reverted=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        if not row:
            log.info("[learner] Nothing to revert.")
            return

        hist_id, param, old_val, new_val = row
        log.warning(f"[learner] REVERTING {param}: {new_val} → {old_val}")
        self._apply_change(param, new_val, old_val, f"REVERT of change id={hist_id}")

        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE strategy_history SET reverted=1 WHERE id=?", (hist_id,))
        conn.commit()
        conn.close()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_today_pnl(self, date_str: str) -> dict:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT COUNT(*), SUM(pnl), AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END) "
            "FROM trade_decisions WHERE timestamp LIKE ? AND status='closed' AND paper_trade=1",
            (f"{date_str}%",),
        ).fetchone()
        conn.close()
        if row and row[0]:
            return {"trades": row[0], "total_pnl": row[1] or 0.0, "win_rate": row[2] or 0.0}
        return {"trades": 0, "total_pnl": 0.0, "win_rate": 0.0}

    def _load_strategy(self) -> dict:
        with open(STRATEGY_PATH) as f:
            return json.load(f)

    def _log_session(self, session_type: str, summary: str, changes: int, status: str = "ok"):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO learning_sessions (timestamp, session_type, summary, changes_made, status) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(), session_type, summary, changes, status),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[learner] Failed to log session: {e}")

    # ── Public entry points ────────────────────────────────────────────────────

    def score_agent(self, agent_name: str, metric: str, value: float,
                    sample_size: int = 1, notes: str = ""):
        """
        Ad-hoc score recording — any module can call this to report an agent metric.
        Example: metrics_logger calls this after each cycle to track LLM latency.
        """
        today = date.today().isoformat()
        self._upsert_score(today, agent_name, metric, value, sample_size, notes)
