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
from datetime import datetime, timedelta, timezone

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


def _chatty_echoes_synthesis(conn: sqlite3.Connection, row_id: int, content: str,
                              ts: str, window_seconds: int = 60) -> str | None:
    """If a Synthesis brief landed within `window_seconds` before this Chatty
    row AND the Chatty content's directional bias matches Synthesis's
    consensus direction, return the Synthesis direction so the caller can log
    + suppress. Returns None when there's no echo to suppress.
    """
    if not content or not ts:
        return None
    # Find a Synthesis brief in the window (ts arithmetic uses SQLite's
    # datetime; the row_id<? guard avoids race conditions with the current row).
    # Normalize both sides with datetime() so the ISO-with-TZ stored format
    # compares correctly against the windowed cutoff (otherwise the `T` and
    # `+00:00` suffix make string comparison unreliable).
    syn_row = conn.execute(
        "SELECT content FROM agent_logs "
        "WHERE agent_name='Synthesis' AND task_type='synthesis' AND status='ok' "
        "AND length(content) > 50 AND id < ? "
        "AND datetime(timestamp) > datetime(?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (row_id, ts, f"-{window_seconds} seconds"),
    ).fetchone()
    if not syn_row or not syn_row[0]:
        return None
    from message_formatters import _extract_consensus_dir
    syn_dir = _extract_consensus_dir(syn_row[0])
    if not syn_dir:
        return None
    # Extract Chatty's bias from prose. Map rising/falling/quiet/etc. to
    # the canonical BULLISH/BEARISH/NEUTRAL bucket.
    import re as _re
    text = content.upper()
    # Check explicit state words first
    for word, canonical in [
        ("BULLISH", "BULLISH"), ("BEARISH", "BEARISH"), ("NEUTRAL", "NEUTRAL"),
        ("RISING", "BULLISH"), ("FALLING", "BEARISH"),
    ]:
        if _re.search(rf"\b{word}\b", text):
            if canonical == syn_dir:
                return syn_dir
            return None
    return None


def _newsie_zero_score_repeat(conn: sqlite3.Connection, row_id: int, content: str,
                               window_minutes: int = 60) -> bool:
    """Return True if the current Newsie row carries a zero-score sentiment AND
    the previous Newsie within `window_minutes` was also zero-score. Suppress
    in that case — back-to-back 'no news' notifications have zero information."""
    if not content:
        return False
    import re as _re
    # Match composite=+0.00, composite=-0.00, composite=0.00, or "neutral 0.0"
    score_match = _re.search(
        r"(?:composite\s*=\s*|score\s+|sentiment[:\s]+|neutral\s+)([+-]?\d+\.\d+)",
        content,
        flags=_re.IGNORECASE,
    )
    if not score_match:
        return False
    try:
        current_score = float(score_match.group(1))
    except ValueError:
        return False
    if abs(current_score) > 0.01:
        return False  # current is non-zero, let it through
    # Check previous Newsie
    prev = conn.execute(
        "SELECT content FROM agent_logs "
        "WHERE agent_name='Newsie' AND task_type='news' AND status='ok' "
        "AND id < ? "
        "AND datetime(timestamp) > datetime('now', ?) "
        "ORDER BY id DESC LIMIT 1",
        (row_id, f"-{window_minutes} minutes"),
    ).fetchone()
    if not prev or not prev[0]:
        return False
    prev_match = _re.search(
        r"(?:composite\s*=\s*|score\s+|sentiment[:\s]+|neutral\s+)([+-]?\d+\.\d+)",
        prev[0],
        flags=_re.IGNORECASE,
    )
    if not prev_match:
        return False
    try:
        prev_score = float(prev_match.group(1))
    except ValueError:
        return False
    return abs(prev_score) <= 0.01  # both zero → suppress


def _is_stale(ts: str, max_age_minutes: int) -> bool:
    """True if ts is older than max_age_minutes. Returns True on parse
    failure to err on the side of skipping (better to drop a row than
    forward stale content as if current).

    `ts` is whatever write_log persisted — currently ET ISO 8601 with
    offset (e.g. '2026-05-01T10:00:06-04:00'). datetime.fromisoformat
    handles tz-aware strings; SQLite default 'YYYY-MM-DD HH:MM:SS' (no tz)
    is also accepted, treated as UTC.
    """
    if not ts:
        return True
    try:
        normalized = ts.replace(" ", "T", 1) if (len(ts) > 10 and ts[10] == " ") else ts
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) > timedelta(minutes=max_age_minutes)
    except Exception:
        return True


def _format(v: Voice, content: str, ts: str, prev_state: dict | None = None) -> str:
    # Strip HTML-special chars that break Telegram parse_mode=HTML
    safe = (content or "").replace("<", "&lt;").replace(">", "&gt;").strip()
    # Display-layer transforms — canonical content stays in agent_logs.
    # ORDER MATTERS: decimal→% must run BEFORE colorize, because colorize
    # injects an emoji between `TREND:` and the word, which would otherwise
    # block the decimal regex from matching.
    #   1) decimal_confidence_to_percent: TREND DOWN 0.55 → TREND DOWN 55%
    #   2) colorize_status_emojis: prepend 🟢🟡🔴 etc. before status words
    #   3) layout_synthesis_brief: SIGNAL on top (bold), NOW/NEXT as bullets,
    #      optional ⚡ FLIP marker when consensus/signal direction changed
    try:
        from message_formatters import (
            colorize_status_emojis,
            decimal_confidence_to_percent,
            layout_synthesis_brief,
        )
        safe = decimal_confidence_to_percent(safe)
        safe = colorize_status_emojis(safe)
        safe = layout_synthesis_brief(safe, prev_state=prev_state)
    except Exception:
        pass
    # Keep it readable — emoji prepends + bullet layout add bytes, raise the cap
    if len(safe) > 800:
        safe = safe[:797] + "..."
    from datetime import date
    if len(ts) >= 16:
        ts_date = ts[:10]
        hhmm = ts[11:16]
        time_part = hhmm if ts_date == str(date.today()) else f"{ts_date[5:]} {hhmm}"
    else:
        time_part = ts
    # Append plain-English glosses for any trading jargon (RSI/EMA/VWAP/MACD/...)
    # so a non-quant reader can act on the signal. Empty footer when no jargon.
    try:
        from trading_glossary import glossary_footer
        footer = glossary_footer(safe)
    except Exception:
        footer = ""
    body = f"{safe}\n\n<i>{footer}</i>" if footer else safe
    return f"{v.emoji} <i>{time_part}</i>\n{body}"


def forward_pending(db_path: str = DB_PATH) -> dict[str, int]:
    """Forward any unseen agent outputs to Telegram. Returns {agent: count_sent}.

    Stale-row defense: if a row's timestamp is older than
    AGENT_VOICE_MAX_STALENESS_MIN (default 30) minutes, it is silently
    skipped and the watermark is advanced past it. This keeps the team
    from being shown 2-day-old "current" briefs after the forwarder gets
    behind a backlog (the watermark drains at max_per_run/tick, which
    matches the agent's own write rate, so a backlog never catches up
    on its own).
    """
    sent: dict[str, int] = {}
    stale_cutoff_min = int(os.getenv("AGENT_VOICE_MAX_STALENESS_MIN", "30"))
    # Big enough to plow through multi-day backlogs in a few ticks; bounded
    # so a single tick can't stall on huge result sets.
    QUERY_LIMIT = 500

    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        for v in VOICES:
            watermark = _get_watermark(conn, v)
            rows = conn.execute(
                "SELECT id, timestamp, content FROM agent_logs "
                "WHERE agent_name=? AND task_type=? AND status='ok' AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (v.agent_name, v.task_type, watermark, QUERY_LIMIT),
            ).fetchall()

            sent_count = 0
            skipped_count = 0
            new_watermark = watermark
            for row_id, ts, content in rows:
                if _is_stale(ts or "", stale_cutoff_min):
                    new_watermark = row_id
                    skipped_count += 1
                    continue
                if sent_count >= v.max_per_run:
                    break
                # Chatty echo suppression: if this Chatty row's bias matches
                # the most recent Synthesis (within 60s), don't re-notify.
                if v.agent_name == "Chatty":
                    echo_dir = _chatty_echoes_synthesis(conn, row_id, content or "", ts or "")
                    if echo_dir:
                        log.info(f"[voice] Chatty echo of Synthesis ({echo_dir}) — suppressed")
                        new_watermark = row_id
                        continue
                # Newsie zero-score repeat suppression
                if v.agent_name == "Newsie":
                    if _newsie_zero_score_repeat(conn, row_id, content or ""):
                        log.info(f"[voice] Newsie zero-score repeat — suppressed")
                        new_watermark = row_id
                        continue
                # Flip detection: for Synthesis, pull the previous brief's
                # consensus + signal action so the formatter can flag direction
                # changes. Other voices don't have a flip concept.
                prev_state = None
                if v.agent_name == "Synthesis":
                    prev_row = conn.execute(
                        "SELECT content FROM agent_logs "
                        "WHERE agent_name='Synthesis' AND task_type='synthesis' "
                        "AND status='ok' AND id < ? ORDER BY id DESC LIMIT 1",
                        (row_id,),
                    ).fetchone()
                    if prev_row and prev_row[0]:
                        from message_formatters import _extract_consensus_dir, _extract_signal_action
                        prev_state = {
                            "consensus": _extract_consensus_dir(prev_row[0]),
                            "signal": _extract_signal_action(prev_row[0]),
                        }
                msg = _format(v, content or "", ts or "", prev_state=prev_state)
                ok = notifier._send(msg)
                if not ok:
                    log.warning(f"[voice] send failed for {v.agent_name} id={row_id}; "
                                "not advancing watermark past it")
                    break  # leave this and remaining rows for next tick
                sent_count += 1
                new_watermark = row_id

            if skipped_count:
                log.info(f"[voice] {v.agent_name} skipped {skipped_count} stale row(s) "
                         f"(>{stale_cutoff_min} min old)")

            if new_watermark != watermark:
                _set_watermark(conn, v, new_watermark)
                conn.commit()
            sent[v.agent_name] = sent_count
    finally:
        conn.close()
    return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(forward_pending())
