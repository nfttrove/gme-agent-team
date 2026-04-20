"""Database maintenance utilities for agent_memory.db."""
import sqlite3
import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


def cleanup_old_logs(days_to_keep: int = 90):
    """Delete agent logs older than specified days."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cutoff_date = (datetime.now(ET) - timedelta(days=days_to_keep)).isoformat()

        cursor = conn.cursor()
        cursor.execute("DELETE FROM agent_logs WHERE timestamp < ?", (cutoff_date,))
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted_count > 0:
            log.info(f"[DB Maintenance] Deleted {deleted_count} log entries older than {days_to_keep} days")
        return deleted_count
    except Exception as e:
        log.error(f"[DB Maintenance] Cleanup failed: {e}")
        return 0


def get_db_size():
    """Return database size in MB."""
    try:
        size_bytes = os.path.getsize(DB_PATH)
        return size_bytes / (1024 * 1024)  # Convert to MB
    except Exception:
        return 0


def get_log_count():
    """Return total number of agent logs."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM agent_logs")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def get_oldest_log_date():
    """Return timestamp of oldest log entry."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(timestamp) FROM agent_logs")
        result = cursor.fetchone()[0]
        conn.close()
        return result
    except Exception:
        return None


def print_db_stats():
    """Print database statistics."""
    size_mb = get_db_size()
    log_count = get_log_count()
    oldest = get_oldest_log_date()

    print(f"\n📊 Database Statistics")
    print(f"  Size: {size_mb:.1f} MB")
    print(f"  Total logs: {log_count:,}")
    print(f"  Oldest entry: {oldest or 'N/A'}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        cleanup_old_logs(days)
        print_db_stats()
    else:
        print_db_stats()
