"""Goal alignment and mission tracking service."""
import sqlite3
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from models.enums import AgentName, TeamName, TaskType

log = logging.getLogger(__name__)

DB_PATH = "agent_memory.db"


class GoalService:
    """Manages mission, goals, and task alignment."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def get_mission(self, mission_id: int = 1) -> Optional[Dict[str, Any]]:
        """Get the current mission."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, name, description, created_at FROM missions WHERE id = ?",
                (mission_id,)
            ).fetchone()
            conn.close()

            if row:
                return dict(row)
            return None
        except Exception as e:
            log.error(f"[goal_service] Get mission failed: {e}")
            return None

    def get_team_goals(self, mission_id: int = 1) -> List[Dict[str, Any]]:
        """Get all goals for a mission."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, mission_id, team, goal, quarterly_target, created_at
                   FROM team_goals WHERE mission_id = ? ORDER BY team""",
                (mission_id,)
            ).fetchall()
            conn.close()

            return [dict(row) for row in rows]
        except Exception as e:
            log.error(f"[goal_service] Get team goals failed: {e}")
            return []

    def get_agent_goal(self, agent_name: str, goal_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Get which goal an agent serves."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            if goal_id:
                row = conn.execute(
                    "SELECT * FROM agent_tasks WHERE agent_name = ? AND goal_id = ?",
                    (agent_name, goal_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM agent_tasks WHERE agent_name = ? LIMIT 1",
                    (agent_name,)
                ).fetchone()

            conn.close()

            if row:
                return dict(row)
            return None
        except Exception as e:
            log.error(f"[goal_service] Get agent goal failed: {e}")
            return None

    def get_mission_progress(self, mission_id: int = 1) -> Dict[str, Any]:
        """Get progress toward mission."""
        try:
            mission = self.get_mission(mission_id)
            goals = self.get_team_goals(mission_id)

            team_progress = []
            for goal in goals:
                # Get agent runs for this goal
                conn = sqlite3.connect(self.db_path)
                count = conn.execute(
                    """SELECT COUNT(*) FROM agent_logs WHERE
                       task_type IN (?, ?, ?) AND status = 'ok'
                       AND DATE(timestamp) = DATE('now')""",
                    ("validation", "synthesis", "prediction")
                ).fetchone()[0]
                conn.close()

                team_progress.append({
                    "team": goal["team"],
                    "goal": goal["goal"],
                    "target": goal["quarterly_target"],
                    "status": "ON_TRACK" if count > 0 else "NO_RECENT_DATA"
                })

            return {
                "mission": mission["name"] if mission else "Unknown",
                "teams": team_progress
            }
        except Exception as e:
            log.error(f"[goal_service] Get mission progress failed: {e}")
            return {"mission": "Error", "teams": []}

    def get_agent_alignment(self, agent_name: str) -> Dict[str, Any]:
        """Show agent's purpose and alignment."""
        try:
            agent_task = self.get_agent_goal(agent_name)

            if not agent_task:
                return {
                    "agent": agent_name,
                    "role": "Unknown",
                    "error": "No goal found for agent"
                }

            # Get team goal
            conn = sqlite3.connect(self.db_path)
            goal_row = conn.execute(
                "SELECT * FROM team_goals WHERE id = ?",
                (agent_task["goal_id"],)
            ).fetchone()

            # Get mission
            if goal_row:
                mission_row = conn.execute(
                    "SELECT * FROM missions WHERE id = ?",
                    (goal_row[1],)  # mission_id is second column
                ).fetchone()
            else:
                mission_row = None

            # Get last run
            last_run = conn.execute(
                "SELECT timestamp, status FROM agent_logs WHERE agent_name = ? ORDER BY timestamp DESC LIMIT 1",
                (agent_name,)
            ).fetchone()

            conn.close()

            return {
                "agent": agent_name,
                "role": agent_task.get("task", "Unknown"),
                "team_goal": goal_row[2] if goal_row else "Unknown",  # goal column
                "mission": mission_row[1] if mission_row else "Unknown",  # name column
                "contribution": f"Contributes to: {goal_row[2] if goal_row else 'Unknown goal'}",
                "last_run": last_run[0] if last_run else None,
                "last_status": last_run[1] if last_run else None
            }
        except Exception as e:
            log.error(f"[goal_service] Get agent alignment failed: {e}")
            return {"agent": agent_name, "error": str(e)}

    def create_mission(self, name: str, description: str = "") -> int:
        """Create a new mission (for initialization)."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "INSERT INTO missions (name, description) VALUES (?, ?)",
                (name, description)
            )
            conn.commit()
            mission_id = cursor.lastrowid
            conn.close()
            return mission_id
        except Exception as e:
            log.error(f"[goal_service] Create mission failed: {e}")
            return -1

    def bootstrap_goals(self, mission_id: int):
        """Initialize default goals for the GME system."""
        goals = [
            (mission_id, "research", "Identify 3+ strong signals daily", 100),
            (mission_id, "trading", "Execute profitable trades with >60% win rate", 50000),
            (mission_id, "risk", "Maintain zero blown positions (hard stop active)", 0),
            (mission_id, "monitoring", "Track all agent health and costs", 500),
        ]

        try:
            conn = sqlite3.connect(self.db_path)
            for mission_id, team, goal, target in goals:
                conn.execute(
                    "INSERT OR IGNORE INTO team_goals (mission_id, team, goal, quarterly_target) VALUES (?, ?, ?, ?)",
                    (mission_id, team, goal, target)
                )
            conn.commit()
            conn.close()
            log.info("[goal_service] Bootstrapped default goals")
        except Exception as e:
            log.error(f"[goal_service] Bootstrap failed: {e}")
