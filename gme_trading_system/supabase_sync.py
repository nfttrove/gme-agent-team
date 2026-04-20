"""
Supabase sync — mirrors 5 key SQLite tables to Supabase every 30 seconds.
Progress is tracked in supabase_sync_state.json (last synced row id per table).
Runs as a daemon thread alongside the orchestrator — if Supabase is unreachable
it logs a warning and retries next cycle; local SQLite is never blocked.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(_DIR, "agent_memory.db")
STATE_PATH = os.path.join(_DIR, "supabase_sync_state.json")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Tables and columns to sync (id must be first — used as upsert key)
TABLES = {
    "agent_logs": [
        "id", "agent_name", "timestamp", "task_type", "content", "status",
    ],
    "trade_decisions": [
        "id", "order_id", "timestamp", "action", "symbol", "quantity",
        "entry_price", "stop_loss", "take_profit", "confidence",
        "approved_by", "status", "paper_trade", "exit_price", "pnl", "notes",
    ],
    "predictions": [
        "id", "timestamp", "horizon", "predicted_price", "confidence",
        "reasoning", "actual_price", "error_pct",
    ],
    "stream_comments": [
        "id", "timestamp", "comment",
    ],
    "structural_signals": [
        "id", "timestamp", "ticker", "signal_name", "filing_type",
        "filing_date", "headline", "url", "confidence", "action", "timeline_months",
    ],
}


def _load_state() -> dict:
    if Path(STATE_PATH).exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {t: 0 for t in TABLES}


def _save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def _get_client():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def sync_once(client, state: dict) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    updated = False

    for table, cols in TABLES.items():
        last_id = state.get(table, 0)
        col_list = ", ".join(cols)
        try:
            rows = conn.execute(
                f"SELECT {col_list} FROM {table} WHERE id > ? ORDER BY id LIMIT 200",
                (last_id,),
            ).fetchall()
        except Exception as e:
            log.warning(f"[Supabase] SQLite read failed for {table}: {e}")
            continue

        if not rows:
            continue

        payload = [dict(row) for row in rows]
        max_id = max(r["id"] for r in payload)

        try:
            client.table(table).upsert(payload).execute()
            state[table] = max_id
            updated = True
            log.info(f"[Supabase] Synced {len(payload)} rows → {table} (up to id={max_id})")
        except Exception as e:
            log.warning(f"[Supabase] Upsert failed for {table}: {e}")

    conn.close()
    if updated:
        _save_state(state)
    return state


def _sync_loop():
    log.info("[Supabase] Sync thread started (interval: 30s)")
    state = _load_state()
    try:
        client = _get_client()
    except Exception as e:
        log.error(f"[Supabase] Failed to create client — sync disabled: {e}")
        return

    while True:
        try:
            state = sync_once(client, state)
        except Exception as e:
            log.error(f"[Supabase] Sync cycle error: {e}")
        time.sleep(30)


def start_sync_thread() -> threading.Thread | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("[Supabase] SUPABASE_URL/KEY not set — sync disabled")
        return None
    t = threading.Thread(target=_sync_loop, daemon=True, name="supabase-sync")
    t.start()
    return t
