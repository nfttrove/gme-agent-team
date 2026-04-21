"""Database maintenance utilities for agent_memory.db."""
import sqlite3
import os
import logging
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "data", "backups")
BACKUP_RETENTION_DAYS = 14


def enable_wal_mode(db_path: str = DB_PATH) -> str:
    """Enable WAL journal mode + NORMAL synchronous.

    WAL allows concurrent reads while a writer is active — critical because
    the orchestrator, dashboard, and discord bot all hit the same DB.
    Setting journal_mode is persistent across connections.

    Returns the active journal mode (e.g. "wal").
    """
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
        log.info(f"[DB] journal_mode={mode}, synchronous=NORMAL")
        return mode
    finally:
        conn.close()


def backup_db(db_path: str = DB_PATH, backup_dir: str = BACKUP_DIR) -> str:
    """Create a consistent backup of the SQLite database.

    Uses SQLite's online backup API (via Connection.backup) so the file is
    safe even if writers are active. WAL files are handled correctly.

    Returns the path to the new backup file.
    """
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"agent_memory_{stamp}.db")

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        log.info(f"[DB] backup written: {dest} ({os.path.getsize(dest) / 1e6:.1f} MB)")
        return dest
    finally:
        src.close()
        dst.close()


def prune_old_backups(backup_dir: str = BACKUP_DIR, days: int = BACKUP_RETENTION_DAYS) -> int:
    """Delete backups older than `days`. Returns count deleted."""
    if not os.path.isdir(backup_dir):
        return 0
    cutoff = datetime.now(ET) - timedelta(days=days)
    deleted = 0
    for name in os.listdir(backup_dir):
        if not name.startswith("agent_memory_") or not name.endswith(".db"):
            continue
        path = os.path.join(backup_dir, name)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), ET)
        if mtime < cutoff:
            os.remove(path)
            deleted += 1
    if deleted:
        log.info(f"[DB] pruned {deleted} backups older than {days}d")
    return deleted


def nightly_maintenance() -> dict:
    """Run all nightly DB tasks. Called from cron / scheduler."""
    result = {}
    try:
        result["backup"] = backup_db()
        result["pruned"] = prune_old_backups()
        result["logs_deleted"] = cleanup_old_logs()
    except Exception as e:
        log.error(f"[DB] nightly maintenance failed: {e}")
        result["error"] = str(e)
    return result


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

    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "cleanup":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        cleanup_old_logs(days)
        print_db_stats()
    elif cmd == "wal":
        mode = enable_wal_mode()
        print(f"journal_mode={mode}")
    elif cmd == "backup":
        path = backup_db()
        pruned = prune_old_backups()
        print(f"backup: {path}")
        print(f"pruned: {pruned}")
    elif cmd == "nightly":
        print(nightly_maintenance())
    else:
        print_db_stats()
