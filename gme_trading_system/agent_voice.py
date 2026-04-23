"""Agent voice forwarder.

Forwards each agent's latest output to Telegram in its own voice, so the team
hears from Chatty, Newsie, Futurist (etc.) directly instead of just getting
consolidated summaries.

Watermark strategy: uses a tiny key-value table `voice_watermarks` to track the
last `agent_logs.id` forwarded per agent. Picking up ids > watermark guarantees
no duplicates and no gaps.

Gating: caller is responsible for active-window checks. This module just reads
and forwards. Signal alerts (price predictions with entry/SL/TP) continue to go
through notifier.notify_signal_alert — this is for narrative/commentary only.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass

import notifier

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


@dataclass(frozen=True)
class Voice:
    agent_name: str
    task_type: str   # exact match on agent_logs.task_type
    emoji: str
    label: str       # human-readable persona label
    max_per_run: int # cap to avoid backlog spam after downtime


# Agents currently producing real output (CrewAI-bypass rewrites). Add more
# here as they are fixed. Order matters — forwarded in this order each run.
VOICES: list[Voice] = [
    Voice("CTO",       "trove_score",       "🛡️", "CTO",       max_per_run=1),
    Voice("Synthesis", "synthesis",         "🧠", "Synthesis", max_per_run=1),
    Voice("Trendy",    "trend_signal",      "📈", "Trendy",    max_per_run=1),
    Voice("Pattern",   "pattern_signal",    "🎯", "Pattern",   max_per_run=1),
    Voice("Futurist",  "prediction_signal", "🔮", "Futurist",  max_per_run=2),
    Voice("Newsie",    "news",              "📰", "Newsie",    max_per_run=1),
    Voice("Chatty",    "commentary",        "💬", "Chatty",    max_per_run=2),
]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS voice_watermarks ("
        "  agent_name TEXT NOT NULL,"
        "  task_type  TEXT NOT NULL,"
        "  last_id    INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (agent_name, task_type)"
        ")"
    )


def _get_watermark(conn: sqlite3.Connection, v: Voice) -> int:
    row = conn.execute(
        "SELECT last_id FROM voice_watermarks WHERE agent_name=? AND task_type=?",
        (v.agent_name, v.task_type),
    ).fetchone()
    if row:
        return row[0]
    # First encounter — bootstrap to the current max id so we only forward
    # things that happen *after* the forwarder starts running. No historical
    # backfill (would be years of noise on existing installs).
    bootstrap = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM agent_logs "
        "WHERE agent_name=? AND task_type=? AND status='ok'",
        (v.agent_name, v.task_type),
    ).fetchone()[0]
    _set_watermark(conn, v, bootstrap)
    conn.commit()
    return bootstrap


def _set_watermark(conn: sqlite3.Connection, v: Voice, last_id: int) -> None:
    conn.execute(
        "INSERT INTO voice_watermarks (agent_name, task_type, last_id) VALUES (?, ?, ?) "
        "ON CONFLICT(agent_name, task_type) DO UPDATE SET last_id=excluded.last_id",
        (v.agent_name, v.task_type, last_id),
    )


def _format(v: Voice, content: str, ts: str) -> str:
    # Strip HTML-special chars that break Telegram parse_mode=HTML
    safe = (content or "").replace("<", "&lt;").replace(">", "&gt;").strip()
    # Keep it readable — cap at 500 chars
    if len(safe) > 500:
        safe = safe[:497] + "..."
    time_part = ts[11:16] if len(ts) >= 16 else ts  # HH:MM
    return f"{v.emoji} <b>{v.label}</b> <i>{time_part}</i>\n{safe}"


def forward_pending(db_path: str = DB_PATH) -> dict[str, int]:
    """Forward any unseen agent outputs to Telegram. Returns {agent: count_sent}."""
    sent: dict[str, int] = {}
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        for v in VOICES:
            watermark = _get_watermark(conn, v)
            rows = conn.execute(
                "SELECT id, timestamp, content FROM agent_logs "
                "WHERE agent_name=? AND task_type=? AND status='ok' AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (v.agent_name, v.task_type, watermark, v.max_per_run),
            ).fetchall()

            count = 0
            new_watermark = watermark
            for row_id, ts, content in rows:
                msg = _format(v, content or "", ts or "")
                ok = notifier._send(msg)
                if not ok:
                    log.warning(f"[voice] send failed for {v.agent_name} id={row_id}; "
                                "not advancing watermark")
                    break  # leave remaining unsent so next run retries
                count += 1
                new_watermark = row_id

            # Skip over any rows we couldn't send (above). On success, jump past
            # all rows we did send. If there were rows but none sent, watermark
            # stays put and we'll retry next tick.
            if new_watermark != watermark:
                _set_watermark(conn, v, new_watermark)
                conn.commit()
            sent[v.agent_name] = count
    finally:
        conn.close()
    return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(forward_pending())
