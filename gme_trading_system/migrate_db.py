"""
One-time database migration: adds UNIQUE(symbol, timestamp) to price_ticks.

SQLite cannot ADD CONSTRAINT to an existing table, so we:
  1. Rename price_ticks → price_ticks_old
  2. Create new price_ticks with the UNIQUE constraint
  3. Copy rows in, IGNORE conflicts (keeps first-seen row per timestamp)
  4. Drop the old table
  5. Rebuild indices

Run once:  python migrate_db.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


def migrate():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN")

        # Check if migration already applied
        info = conn.execute("PRAGMA table_info(price_ticks)").fetchall()
        cols = {r[1] for r in info}
        if not cols:
            print("[migrate] price_ticks table does not exist — nothing to do.")
            conn.execute("ROLLBACK")
            return

        # Check current indices for UNIQUE
        indices = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='price_ticks'"
        ).fetchall()
        already_unique = any("UNIQUE" in (sql or "").upper() for _, sql in indices)
        if already_unique:
            print("[migrate] UNIQUE constraint already present — skipping.")
            conn.execute("ROLLBACK")
            return

        print("[migrate] Renaming price_ticks → price_ticks_old ...")
        conn.execute("ALTER TABLE price_ticks RENAME TO price_ticks_old")

        print("[migrate] Creating new price_ticks with UNIQUE(symbol, timestamp) ...")
        conn.execute("""
            CREATE TABLE price_ticks (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol    TEXT    NOT NULL DEFAULT 'GME',
                timestamp TEXT    NOT NULL,
                open      REAL,
                high      REAL,
                low       REAL,
                close     REAL,
                volume    REAL,
                source    TEXT    DEFAULT 'tradingview',
                UNIQUE(symbol, timestamp)
            )
        """)

        print("[migrate] Copying rows (duplicates ignored, tradingview takes priority) ...")
        # Insert tradingview rows first so they win on conflict
        conn.execute("""
            INSERT OR IGNORE INTO price_ticks (symbol, timestamp, open, high, low, close, volume, source)
            SELECT symbol, timestamp, open, high, low, close, volume, source
            FROM price_ticks_old
            WHERE source = 'tradingview'
            ORDER BY timestamp ASC
        """)
        conn.execute("""
            INSERT OR IGNORE INTO price_ticks (symbol, timestamp, open, high, low, close, volume, source)
            SELECT symbol, timestamp, open, high, low, close, volume, source
            FROM price_ticks_old
            WHERE source != 'tradingview'
            ORDER BY timestamp ASC
        """)

        old_count = conn.execute("SELECT COUNT(*) FROM price_ticks_old").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM price_ticks").fetchone()[0]
        removed   = old_count - new_count
        print(f"[migrate] Rows: {old_count} → {new_count} ({removed} duplicates removed)")

        print("[migrate] Dropping old table ...")
        conn.execute("DROP TABLE price_ticks_old")

        print("[migrate] Rebuilding indices ...")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_price_ticks_timestamp ON price_ticks(timestamp)")

        conn.execute("COMMIT")
        print("[migrate] Done.")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[migrate] FAILED — rolled back. Error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
