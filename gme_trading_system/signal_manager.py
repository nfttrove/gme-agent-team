"""Signal confidence and feedback loop management.

Logs each alert with confidence score, tracks team decisions, and computes signal metrics.
"""
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any
import logging

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


class SignalManager:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def log_alert(
        self,
        agent_name: str,
        signal_type: str,
        confidence: float,
        severity: str = "MEDIUM",
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reasoning: str = "",
        telegram_message_id: Optional[int] = None,
    ) -> str:
        """Log a signal alert. Returns alert_id."""
        alert_id = str(uuid.uuid4())
        timestamp = datetime.now(ET).isoformat()

        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                INSERT INTO signal_alerts
                (id, agent_name, signal_type, confidence, severity, entry_price, stop_loss, take_profit, reasoning, telegram_message_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    agent_name,
                    signal_type,
                    confidence,
                    severity,
                    entry_price,
                    stop_loss,
                    take_profit,
                    reasoning,
                    telegram_message_id,
                    timestamp,
                ),
            )
            conn.commit()
            conn.close()
            log.info(f"Alert logged: {agent_name} | {signal_type} | confidence={confidence:.0%}")
            return alert_id
        except Exception as e:
            log.error(f"Failed to log alert: {e}")
            raise

    def log_feedback(
        self,
        alert_id: str,
        action_taken: str,  # 'executed', 'ignored', 'missed'
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        quantity: Optional[float] = None,
        team_member: Optional[str] = None,
        team_notes: str = "",
    ) -> str:
        """Log team feedback on an alert. Returns feedback_id."""
        feedback_id = str(uuid.uuid4())
        pnl = None
        pnl_pct = None

        if entry_price and exit_price and quantity:
            pnl = (exit_price - entry_price) * quantity
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100

        try:
            conn = sqlite3.connect(self.db_path)
            execution_timestamp = datetime.now(ET).isoformat() if action_taken == "executed" else None

            conn.execute(
                """
                INSERT INTO signal_feedback
                (id, alert_id, action_taken, execution_timestamp, entry_price, exit_price, quantity, pnl, pnl_pct, team_member, team_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    alert_id,
                    action_taken,
                    execution_timestamp,
                    entry_price,
                    exit_price,
                    quantity,
                    pnl,
                    pnl_pct,
                    team_member,
                    team_notes,
                ),
            )
            conn.commit()
            conn.close()
            log.info(f"Feedback logged: {alert_id[:8]} | {action_taken}")
            return feedback_id
        except Exception as e:
            log.error(f"Failed to log feedback: {e}")
            raise

    def get_signal_metrics(self, agent_name: Optional[str] = None, signal_type: Optional[str] = None, days: int = 30) -> Dict[str, Any]:
        """Compute win rate, execution rate, and other metrics."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # Build query
            query = """
            SELECT
              a.agent_name,
              a.signal_type,
              COUNT(a.id) as total_alerts,
              COUNT(f.id) as feedback_count,
              SUM(CASE WHEN f.action_taken = 'executed' THEN 1 ELSE 0 END) as executed_count,
              SUM(CASE WHEN f.action_taken = 'ignored' THEN 1 ELSE 0 END) as ignored_count,
              SUM(CASE WHEN f.action_taken = 'missed' THEN 1 ELSE 0 END) as missed_count,
              AVG(a.confidence) as avg_confidence,
              SUM(CASE WHEN f.pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
              AVG(f.pnl_pct) as avg_pnl_pct
            FROM signal_alerts a
            LEFT JOIN signal_feedback f ON a.id = f.alert_id
            WHERE datetime(a.timestamp) > datetime('now', '-{} days')
            """.format(days)

            params = []
            if agent_name:
                query += " AND a.agent_name = ?"
                params.append(agent_name)
            if signal_type:
                query += " AND a.signal_type = ?"
                params.append(signal_type)

            query += " GROUP BY a.agent_name, a.signal_type"

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            conn.close()

            metrics = []
            for row in rows:
                total = row["total_alerts"]
                executed = row["executed_count"] or 0
                execution_rate = executed / total if total > 0 else 0
                winning = row["winning_trades"] or 0
                win_rate = winning / executed if executed > 0 else 0

                metrics.append(
                    {
                        "agent": row["agent_name"],
                        "signal_type": row["signal_type"],
                        "total_alerts": total,
                        "execution_rate": execution_rate,
                        "executed": executed,
                        "ignored": row["ignored_count"] or 0,
                        "missed": row["missed_count"] or 0,
                        "avg_confidence": row["avg_confidence"],
                        "win_rate": win_rate,
                        "avg_pnl_pct": row["avg_pnl_pct"],
                    }
                )

            return {"metrics": metrics, "period_days": days}
        except Exception as e:
            log.error(f"Failed to compute metrics: {e}")
            return {"error": str(e)}

    def get_recent_alerts(self, limit: int = 10, agent_name: Optional[str] = None) -> list:
        """Get recent alerts (for dashboard/logging)."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            query = "SELECT * FROM signal_alerts WHERE 1=1"
            params = []

            if agent_name:
                query += " AND agent_name = ?"
                params.append(agent_name)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            conn.close()

            return [dict(row) for row in rows]
        except Exception as e:
            log.error(f"Failed to fetch alerts: {e}")
            return []

    def get_alert_with_feedback(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """Get alert + feedback (if exists)."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # Fetch alert
            alert_row = conn.execute("SELECT * FROM signal_alerts WHERE id = ?", (alert_id,)).fetchone()
            if not alert_row:
                return None

            alert = dict(alert_row)

            # Fetch feedback
            feedback_row = conn.execute("SELECT * FROM signal_feedback WHERE alert_id = ?", (alert_id,)).fetchone()
            alert["feedback"] = dict(feedback_row) if feedback_row else None

            conn.close()
            return alert
        except Exception as e:
            log.error(f"Failed to fetch alert: {e}")
            return None
