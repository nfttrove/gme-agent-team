"""Output validation layer — validates and sanitizes agent outputs before DB insertion."""
import json
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from models.agent_outputs import AgentResult

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def validate_agent_output(output: str, agent_name: str, task_type: str) -> dict:
    """
    Validate and sanitize agent output.

    Returns a dict with 'status', 'content', and 'recovery_action' keys.
    On validation failure, gracefully sanitizes the output instead of rejecting it.
    """
    try:
        # Try parsing as JSON first (agents often return JSON)
        try:
            parsed = json.loads(output)
            content = json.dumps(parsed)[:5000]  # Re-serialize for consistency
        except json.JSONDecodeError:
            content = output

        # Validate with Pydantic
        result = AgentResult(
            task_type=task_type,
            status='ok',
            content=content,
            agent_name=agent_name,
            timestamp=datetime.now(ET).isoformat()
        )

        return {
            'status': 'valid',
            'content': result.content,
            'recovery_action': 'accepted'
        }

    except ValidationError as e:
        # Log validation error and return sanitized version
        log.warning(f"[Validator] {agent_name} output validation failed: {str(e)[:200]}")
        sanitized_content = f"[VALIDATION ERROR] {agent_name}: {str(e)[:500]}"

        return {
            'status': 'invalid',
            'content': sanitized_content,
            'recovery_action': 'sanitized'
        }

    except Exception as e:
        # Catch-all for unexpected errors
        log.error(f"[Validator] Unexpected error validating {agent_name} output: {e}")
        error_content = f"[PARSER ERROR] {str(e)[:500]}"

        return {
            'status': 'error',
            'content': error_content,
            'recovery_action': 'sanitized'
        }


def log_validation_result(
    db_path: str,
    agent_name: str,
    task_type: str,
    original_output: str,
    validation_result: dict
) -> None:
    """Log validation result to validation_log table."""
    try:
        conn = sqlite3.connect(db_path)
        timestamp = datetime.now(ET).isoformat()

        conn.execute("""
            INSERT INTO validation_log
            (timestamp, agent_name, task_type, original_output, validation_status, recovery_action)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            timestamp,
            agent_name,
            task_type,
            original_output[:500],  # Store first 500 chars only
            validation_result['status'],
            validation_result['recovery_action']
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[Validator] Failed to log validation result: {e}")


def ensure_validation_table(db_path: str) -> None:
    """Create validation_log table if it doesn't exist."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS validation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                task_type TEXT,
                original_output TEXT,
                validation_status TEXT,
                recovery_action TEXT,
                FOREIGN KEY (agent_name) REFERENCES agent_logs(agent_name)
            )
        """)
        # Create index for fast lookups
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_validation_timestamp
            ON validation_log(timestamp DESC)
        """)
        conn.commit()
        conn.close()
        log.info("[Validator] validation_log table ready")
    except Exception as e:
        log.error(f"[Validator] Failed to create validation_log table: {e}")
