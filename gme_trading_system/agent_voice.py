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
    # Per-voice staleness override (minutes). When None, uses
    # AGENT_VOICE_MAX_STALENESS_MIN (default 30). Daily/rare agents need
    # a longer window so a late orchestrator restart doesn't silently drop
    # their once-a-day output (e.g., CTO DV fires once at 09:10 ET).
    staleness_minutes: int | None = None


# Agents currently producing real output (CrewAI-bypass rewrites). Add more
# here as they are fixed. Order matters — forwarded in this order each run.
VOICES: list[Voice] = [
    # CTO DV fires once daily — needs a 24h window so a mid-day restart
    # doesn't silently skip the morning row before it can be forwarded.
    Voice("CTO",       "dv_score",          "🛡️", "CTO",       max_per_run=1,
          staleness_minutes=1440),
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
        "  last_pushed_at TEXT,"
        "  PRIMARY KEY (agent_name, task_type)"
        ")"
    )
    # Backfill column for installs that pre-date last_pushed_at — used by
    # state-diff suppression (Chatty/Synthesis heartbeat). NULL is fine for
    # voices that have never pushed; suppression is bypassed in that case.
    try:
        conn.execute("ALTER TABLE voice_watermarks ADD COLUMN last_pushed_at TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists


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


def _mark_pushed_now(conn: sqlite3.Connection, v: Voice) -> None:
    """Stamp the moment we actually delivered a burst for this voice.
    State-diff suppression uses this to enforce a heartbeat — if too long
    has passed since the last push, the agent fires even when state is
    unchanged so the user knows it's still alive."""
    conn.execute(
        "UPDATE voice_watermarks SET last_pushed_at = datetime('now') "
        "WHERE agent_name=? AND task_type=?",
        (v.agent_name, v.task_type),
    )


def _get_last_pushed_at(conn: sqlite3.Connection, v: Voice) -> datetime | None:
    """Return the UTC datetime of the last successful push, or None if
    this voice has never pushed (or the column is NULL on a fresh row)."""
    row = conn.execute(
        "SELECT last_pushed_at FROM voice_watermarks WHERE agent_name=? AND task_type=?",
        (v.agent_name, v.task_type),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        # SQLite datetime('now') returns 'YYYY-MM-DD HH:MM:SS' (UTC, no tz suffix)
        dt = datetime.fromisoformat(str(row[0]).replace(" ", "T", 1))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# State-diff parsing helpers — used by the Chatty/Synthesis suppression rules.
# Kept module-level so tests can exercise them directly.

import re as _re_module

_PRICE_RE = _re_module.compile(r"\$(\d+\.\d{2})")
_CONSENSUS_RE = _re_module.compile(r"CONSENSUS:\s*(\w+)\s+(\d+)", _re_module.IGNORECASE)

# Words that always pass Chatty through, even when price barely moved —
# they signal something material the user should see (volume spike, alarm,
# regime change). Match on uppercased prose so case doesn't matter.
#
# RISING / FALLING / CAUTION removed 2026-05-18: all three were used
# descriptively in routine Chatty prose ("$21.97 rising on quiet volume",
# "team caution at $21.8") so they bypassed dedup on every cycle. The
# remaining tokens are discrete events, not continuous descriptors.
_CHATTY_ALARM_TOKENS = (
    "SPIKE", "ELEVATED VOLUME",
    "BREAKING", "BREAKOUT", "BREAKDOWN", "FLIP", "REVERSAL", "GAP",
)


def _extract_price(text: str) -> float | None:
    """First $XX.XX in the prose, or None."""
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_consensus(text: str) -> tuple[str | None, int | None]:
    """Returns (direction_upper, percent) from a Synthesis brief, or
    (None, None) if no CONSENSUS line."""
    if not text:
        return None, None
    m = _CONSENSUS_RE.search(text)
    if not m:
        return None, None
    direction = m.group(1).upper()
    try:
        return direction, int(m.group(2))
    except ValueError:
        return direction, None


def _synthesis_low_consensus(content: str, min_pct: int = 60) -> bool:
    """Suppress Synthesis bursts whose consensus confidence is below
    `min_pct`. Below ~60% is the no-information regime — the brief is
    saying 'agents disagree' or 'one agent said neutral', which is mush,
    not signal. Real edge lives at ≥60% conviction (and especially flips).
    """
    _, pct = _extract_consensus(content or "")
    if pct is None:
        return False
    return pct < min_pct


def _synthesis_unchanged_state(
    conn: sqlite3.Connection, row_id: int, content: str,
    last_pushed_at: datetime | None,
    max_silence_min: int = 60,
    price_tol_pct: float = 0.5,
    conf_tol_pp: int = 10,
) -> bool:
    """Suppress Synthesis if price + consensus dir + consensus % + signal
    action are all within tolerance of the last produced brief AND we've
    pushed something within max_silence_min. Heartbeat after silence: if
    it's been longer than max_silence_min since last push, we let it
    through so the user knows the agent is alive."""
    if not content:
        return False
    if last_pushed_at is None:
        return False  # never pushed → fire so user sees current state
    age_min = (datetime.now(timezone.utc) - last_pushed_at).total_seconds() / 60.0
    if age_min > max_silence_min:
        return False  # heartbeat: long enough silence, fire even if unchanged
    prev = conn.execute(
        "SELECT content FROM agent_logs "
        "WHERE agent_name='Synthesis' AND task_type='synthesis' AND status='ok' "
        "AND id < ? ORDER BY id DESC LIMIT 1",
        (row_id,),
    ).fetchone()
    if not prev or not prev[0]:
        return False
    cur_price = _extract_price(content)
    prev_price = _extract_price(prev[0])
    cur_dir, cur_pct = _extract_consensus(content)
    prev_dir, prev_pct = _extract_consensus(prev[0])
    if None in (cur_price, prev_price, cur_dir, prev_dir, cur_pct, prev_pct):
        return False
    if prev_price == 0:
        return False
    if abs(cur_price - prev_price) / prev_price * 100.0 >= price_tol_pct:
        return False
    if cur_dir != prev_dir:
        return False
    if abs(cur_pct - prev_pct) >= conf_tol_pp:
        return False
    # Signal action flip is higher-information than ±conf_tol_pp wobble
    # and should pass even when other dims match — BUT with two caveats:
    #   1) churn within {HOLD, WAIT, NEUTRAL} is noise (all 'do nothing')
    #   2) action↔idle flips inside a short window are LLM oscillation,
    #      not market change. Today's stream had SELL→WAIT→SELL→WAIT every
    #      ~5 min with identical dir/price/conf. Require ≥15 min between
    #      such flips so the agent's first decision stands.
    # Direction flips (BULLISH↔BEARISH) bypass this entirely via the
    # earlier cur_dir != prev_dir check.
    from message_formatters import _extract_signal_action
    cur_signal = _extract_signal_action(content)
    prev_signal = _extract_signal_action(prev[0])
    _IDLE_SIGNALS = {"HOLD", "WAIT", "NEUTRAL"}
    # Only BUY/SELL — Synthesis prompts at orchestrator.py:2730 explicitly
    # constrain to BUY/SELL/HOLD/WAIT. LONG/SHORT belong to other agents'
    # vocabularies (CTO short-rankings) and shouldn't be treated as flip
    # synonyms here.
    _ACTION_SIGNALS = {"BUY", "SELL"}
    _MIN_FLIP_AGE_MIN = 15
    if cur_signal and prev_signal and cur_signal != prev_signal:
        cur_idle = cur_signal in _IDLE_SIGNALS
        prev_idle = prev_signal in _IDLE_SIGNALS
        cur_action = cur_signal in _ACTION_SIGNALS
        prev_action = prev_signal in _ACTION_SIGNALS
        # idle↔idle (HOLD↔WAIT↔NEUTRAL): noise — same equivalence class
        if cur_idle and prev_idle:
            return True
        # action↔action (BUY↔SELL): always pass — real reversal, never
        # suppress regardless of how recently we pushed
        if cur_action and prev_action:
            return False
        # action↔idle: pass only if enough time elapsed since last push
        # (avoids LLM oscillation noise observed today)
        if age_min >= _MIN_FLIP_AGE_MIN:
            return False
    return True


_FUTURIST_DIR_RE = _re_module.compile(r"\b(BULLISH|BEARISH|NEUTRAL)\b", _re_module.IGNORECASE)
_FUTURIST_TARGET_RE = _re_module.compile(r"[→\->]+\s*\$?([\d.]+)")
_FUTURIST_CONF_RE = _re_module.compile(r"conf[=:\s]+(\d+)%?", _re_module.IGNORECASE)


def _extract_futurist_fields(text: str) -> tuple[str | None, float | None, int | None]:
    """Returns (direction_upper, target_price, confidence_pct) from a
    Futurist prediction row, or (None, None, None)-style tuple when fields
    are missing. Mirrors the regexes used by `_try_futurist_burst` to keep
    the dedup logic and the formatter in lockstep."""
    if not text:
        return None, None, None
    dir_m = _FUTURIST_DIR_RE.search(text)
    direction = dir_m.group(1).upper() if dir_m else None
    target_m = _FUTURIST_TARGET_RE.search(text)
    try:
        target = float(target_m.group(1)) if target_m else None
    except ValueError:
        target = None
    conf_m = _FUTURIST_CONF_RE.search(text)
    try:
        conf = int(conf_m.group(1)) if conf_m else None
    except ValueError:
        conf = None
    return direction, target, conf


def _futurist_unchanged_state(
    conn: sqlite3.Connection, row_id: int, content: str,
    last_pushed_at: datetime | None,
    max_silence_min: int = 60,
    target_tol_pct: float = 0.5,
    conf_tol_pp: int = 10,
) -> bool:
    """Suppress Futurist if direction + target ± tolerance + confidence
    are all within tolerance of the last produced prediction AND we've
    pushed within max_silence_min. Heartbeat after silence: long enough
    silence always passes so the user knows the agent is alive."""
    if not content:
        return False
    if last_pushed_at is None:
        return False
    age_min = (datetime.now(timezone.utc) - last_pushed_at).total_seconds() / 60.0
    if age_min > max_silence_min:
        return False
    prev = conn.execute(
        "SELECT content FROM agent_logs "
        "WHERE agent_name='Futurist' AND task_type='prediction_signal' AND status='ok' "
        "AND id < ? ORDER BY id DESC LIMIT 1",
        (row_id,),
    ).fetchone()
    if not prev or not prev[0]:
        return False
    cur_dir, cur_target, cur_conf = _extract_futurist_fields(content)
    prev_dir, prev_target, prev_conf = _extract_futurist_fields(prev[0])
    if None in (cur_dir, prev_dir, cur_target, prev_target, cur_conf, prev_conf):
        return False
    if prev_target == 0:
        return False
    if cur_dir != prev_dir:
        return False
    if abs(cur_target - prev_target) / prev_target * 100.0 >= target_tol_pct:
        return False
    if abs(cur_conf - prev_conf) >= conf_tol_pp:
        return False
    return True


def _chatty_unchanged_state(
    conn: sqlite3.Connection, row_id: int, content: str,
    last_pushed_at: datetime | None,
    max_silence_min: int = 30,
    price_tol_pct: float = 0.5,
) -> bool:
    """Suppress Chatty if price hasn't moved and prose has no alarm tokens
    AND we've pushed within max_silence_min. Same heartbeat rule as
    Synthesis — long silences always pass through."""
    if not content:
        return False
    if last_pushed_at is None:
        return False
    age_min = (datetime.now(timezone.utc) - last_pushed_at).total_seconds() / 60.0
    if age_min > max_silence_min:
        return False
    upper = content.upper()
    if any(tok in upper for tok in _CHATTY_ALARM_TOKENS):
        return False
    prev = conn.execute(
        "SELECT content FROM agent_logs "
        "WHERE agent_name='Chatty' AND task_type='commentary' AND status='ok' "
        "AND id < ? ORDER BY id DESC LIMIT 1",
        (row_id,),
    ).fetchone()
    if not prev or not prev[0]:
        return False
    cur_price = _extract_price(content)
    prev_price = _extract_price(prev[0])
    if cur_price is None or prev_price is None or prev_price == 0:
        return False
    return abs(cur_price - prev_price) / prev_price * 100.0 < price_tol_pct


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


def _ny_hhmm(ts: str) -> str:
    """Return 'HH:MM ET' from an ET ISO timestamp, falling back to current ET."""
    if len(ts) >= 16:
        return f"{ts[11:16]} ET"
    try:
        from message_formatters_v2 import get_ny_time_short
        return get_ny_time_short()
    except Exception:
        return ts


def _try_synthesis_burst(content: str, ts: str) -> str | None:
    """Parse canonical Synthesis NOW/NEXT/SIGNAL output and emit as compact burst.

    Returns None when the content doesn't look like a Synthesis brief, letting
    the caller fall through to the legacy prose formatter.

    Canonical format being parsed:
        NOW: PRICE: $22.09 🔻 -1.49% | DATA: clean | NEWS: NEUTRAL 0.13 | STRUCTURAL: CAUTION
        NEXT: CONSENSUS: BEARISH 65% | TREND: DOWN 0.6 | PREDICTION: BEARISH 0.55
        SIGNAL: WAIT — explanation
    """
    if not content or "NOW:" not in content.upper() or "SIGNAL:" not in content.upper():
        return None
    import re as _re

    def _grab(pattern: str, flags=_re.IGNORECASE) -> _re.Match | None:
        return _re.search(pattern, content, flags=flags)

    consensus = _grab(r"CONSENSUS:\s*(\w+)\s+(\d+)%?")
    signal = _grab(r"SIGNAL:\s*(\w+)")
    trend = _grab(r"TREND:\s*(UP|DOWN|SIDEWAYS)")
    # Capture both the status word AND the parenthetical reason so the
    # rendered line carries WHY the backdrop is cautious, not just THAT
    # it is. e.g. STRUCTURAL: CAUTION (consolidating) → CAUTION + reason.
    structural = _grab(r"STRUCTURAL:\s*(GREEN|CAUTION|YELLOW|RED)(?:\s*\(([^)]+)\))?")

    consensus_dir = consensus.group(1).upper() if consensus else None
    consensus_pct = int(consensus.group(2)) if consensus else None
    signal_action = signal.group(1).upper() if signal else None
    trend_dir = trend.group(1).upper() if trend else None
    struct_state = structural.group(1).upper() if structural else None
    struct_reason = structural.group(2).strip() if structural and structural.group(2) else None

    cons_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(consensus_dir, "⚪")
    sig_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "WAIT": "⏳"}.get(signal_action, "⏳")
    trend_emoji = {"UP": "📈", "DOWN": "📉", "SIDEWAYS": "↔️"}.get(trend_dir, "↔️")
    struct_emoji = {"GREEN": "🟢", "CAUTION": "🟡", "YELLOW": "🟡", "RED": "🔴"}.get(struct_state, "⚪")

    # Header tag the brief so readers know what type of message they're
    # looking at without parsing the body. Verdict block uses emoji-LEFT
    # so the color signals direction at a glance, the label confirms.
    lines = [f"🧠 SIGNAL | {_ny_hhmm(ts)}", ""]
    if consensus_dir and consensus_pct is not None:
        lines.append(f"{cons_emoji} Consensus: {consensus_dir} ({consensus_pct}%)")
    if signal_action:
        lines.append(f"{sig_emoji} Signal: {signal_action}")
    if trend_dir:
        lines.append(f"{trend_emoji} Trend: {trend_dir}")
    if struct_state:
        # Promote the reason word to the value position (e.g. CONSOLIDATING)
        # — the emoji color already carries GREEN/CAUTION/RED so the status
        # word would be redundant. Fall back to status word when reason
        # is missing.
        backdrop_value = (struct_reason or struct_state).upper()
        lines.append(f"{struct_emoji} Backdrop: {backdrop_value}")

    # Body must have at least one field beyond the header to be worth emitting
    if len(lines) <= 2:
        return None

    # Optional enrichment: pull the most-recent Trendy reasoning bullets
    # + a derived risk warning. Both are soft-fail — Synthesis brief
    # stands on its own if Trendy is stale or the heuristic doesn't fire.
    try:
        trendy_bullets = _latest_trendy_reasons(max_items=3, max_age_min=15)
    except Exception:
        trendy_bullets = []
    risk_line = _synthesis_risk_warning(signal_action, trend_dir, struct_state)

    if trendy_bullets or risk_line:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━")
        if trendy_bullets:
            lines.append("")
            lines.extend(f"• {b}" for b in trendy_bullets)
        if risk_line:
            lines.append("")
            lines.append(risk_line)

    return "\n".join(lines)


def _latest_trendy_reasons(max_items: int = 3, max_age_min: int = 15) -> list[str]:
    """Pull the most-recent Trendy trend_signal row (if fresh) and split
    its reasoning into up to `max_items` short bullets. Returns [] when
    Trendy is stale or absent — caller treats empty as 'skip the block'."""
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT timestamp, content FROM agent_logs "
                "WHERE agent_name='Trendy' AND task_type='trend_signal' "
                "AND status='ok' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return []
    if not row or not row[1]:
        return []
    ts_str, content = row
    if _is_stale(ts_str or "", max_age_min):
        return []
    # Trendy format: "SIDEWAYS (conf=40%) · S=$21.48 R=$23.19 · Price below VWAP, EMAs, RSI 38.6. 5d range."
    # Reasoning lives after the second " · "; everything before is metadata.
    # If the format ever drifts (no `·` or fewer segments), fall back to
    # sentence-splitting the whole content so bullets degrade rather than
    # vanishing silently.
    parts = [p.strip() for p in content.split("·")]
    if len(parts) >= 3:
        prose = " ".join(parts[2:]).strip().rstrip(".")
    else:
        prose = content.strip().rstrip(".")
    if not prose:
        return []
    return _split_reasons(prose, max_items)


def _synthesis_risk_warning(signal: str | None, trend: str | None,
                              backdrop: str | None) -> str | None:
    """Derive a one-line risk warning from the verdict combo. Heuristic,
    not LLM — surfaces the obvious dangerous configurations a reader
    should flag, without needing the agent to think about it.

    Returns a formatted '⚠️ ...' line or None when no warning fires."""
    if not signal:
        return None
    sig = signal.upper()
    tr = (trend or "").upper()
    bd = (backdrop or "").upper()
    # Sideline signal + cautious backdrop on a directional trend = fakeout zone
    if sig in {"WAIT", "HOLD"} and bd in {"CAUTION", "YELLOW"} and tr in {"UP", "DOWN"}:
        return "⚠️ High fakeout risk"
    # Action signal against a red backdrop — known structural problem
    if sig in {"BUY", "SELL"} and bd == "RED":
        return "⚠️ Acting against a red backdrop"
    # Counter-trend trade
    if (sig == "BUY" and tr == "DOWN") or (sig == "SELL" and tr == "UP"):
        return "⚠️ Counter-trend setup"
    return None


def _split_reasons(prose: str, max_items: int = 3) -> list[str]:
    """Split agent prose into 2–3 reason bullets, preferring sentence boundaries
    over commas (commas inside a list like 'VWAP, EMAs, RSI neutral' shouldn't
    fragment into single-word bullets). Falls back to comma split if no
    sentences detected. Returns clean, trimmed strings."""
    import re as _re
    # Prefer sentence-boundary split (period + space or end)
    parts = [p.strip().rstrip(".") for p in _re.split(r"\.\s+", prose) if p.strip()]
    # If only one sentence, fall back to semicolon, then comma (only if it
    # would give us multiple reasonable chunks — at least 8 chars each)
    if len(parts) == 1:
        for sep in [";", ","]:
            candidate = [p.strip() for p in parts[0].split(sep) if p.strip()]
            if len(candidate) >= 2 and all(len(c) >= 8 for c in candidate):
                parts = candidate
                break
    return parts[:max_items]


def _try_trendy_burst(content: str, ts: str) -> str | None:
    """Trendy emits something like:
        'SIDEWAYS (conf=45%) · S=$21.71 R=$25.25 · Price below VWAP, EMAs, RSI neutral.'

    Parses field-by-field so a partial match still produces a degraded burst.
    Returns None only if direction can't be identified (the minimum signal).
    """
    import re as _re
    text = content.strip()

    dir_m = _re.search(r"\b(UP|DOWN|SIDEWAYS)\b", text, flags=_re.IGNORECASE)
    if not dir_m:
        return None  # no direction → fall through to legacy
    direction = dir_m.group(1).upper()
    dir_emoji = {"UP": "📈", "DOWN": "📉", "SIDEWAYS": "↔️"}[direction]

    conf_m = _re.search(r"conf[=:\s]+(\d+)%?", text, flags=_re.IGNORECASE)
    conf = int(conf_m.group(1)) if conf_m else None

    sup_m = _re.search(r"S=\$?([\d.]+)", text)
    res_m = _re.search(r"R=\$?([\d.]+)", text)

    # Prose: everything after the last "·"
    prose = text.rsplit("·", 1)[-1].strip().rstrip(".") if "·" in text else ""
    reasons = _split_reasons(prose, 3) if prose else []

    header_line = f"{dir_emoji} {direction}" + (f" ({conf}%)" if conf is not None else "")
    lines = [f"📈 {_ny_hhmm(ts)}", "", header_line]
    if sup_m and res_m:
        lines.append(f"Support: ${float(sup_m.group(1)):.2f} | Resistance: ${float(res_m.group(1)):.2f}")
    elif sup_m:
        lines.append(f"Support: ${float(sup_m.group(1)):.2f}")
    elif res_m:
        lines.append(f"Resistance: ${float(res_m.group(1)):.2f}")
    if reasons:
        lines.append("")
        lines.extend(f"• {r}" for r in reasons)
    return "\n".join(lines)


def _try_futurist_burst(content: str, ts: str) -> str | None:
    """Futurist emits something like:
        'BEARISH 1h → $21.95 (conf=55%) · Price below VWAP and EMAs...'

    Parses field-by-field so a partial match still produces a degraded burst.
    Returns None only if direction can't be identified.
    """
    import re as _re
    text = content.strip()

    dir_m = _re.search(r"\b(BULLISH|BEARISH|NEUTRAL)\b", text, flags=_re.IGNORECASE)
    if not dir_m:
        return None
    direction = dir_m.group(1).upper()
    dir_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}[direction]

    # Horizon: usually 1h, 4h, 1d adjacent to direction; optional
    horizon_m = _re.search(r"\b(\d+[mhd])\b", text[dir_m.end():dir_m.end()+10], flags=_re.IGNORECASE)
    horizon = horizon_m.group(1) if horizon_m else None

    target_m = _re.search(r"[→\->]+\s*\$?([\d.]+)", text)
    target = float(target_m.group(1)) if target_m else None

    conf_m = _re.search(r"conf[=:\s]+(\d+)%?", text, flags=_re.IGNORECASE)
    conf = int(conf_m.group(1)) if conf_m else None

    prose = text.rsplit("·", 1)[-1].strip().rstrip(".") if "·" in text else ""
    reasons = _split_reasons(prose, 3) if prose else []

    header_line = f"{dir_emoji} {direction}" + (f" ({conf}%)" if conf is not None else "")
    lines = [f"🔮 {_ny_hhmm(ts)}", "", header_line]
    if target is not None:
        target_line = f"Target: ${target:.2f}"
        if horizon:
            target_line += f" ({horizon})"
        lines.append(target_line)
    if reasons:
        lines.append("")
        lines.extend(f"• {r}" for r in reasons)
    return "\n".join(lines)


def _try_pattern_intraday_burst(content: str, ts: str) -> str | None:
    """Intraday emits something like:
        'breakdown (5m) · DOWN break @ $22.10 (conf=85%) · breakdown detected ...'
    Also sometimes 'No clean pattern on 30d chart — price $22.08 RSI 38'.

    Parses field-by-field so a partial match still produces a degraded burst.
    Returns None only if no recognizable pattern/direction can be extracted.
    """
    import re as _re
    text = content.strip()

    # No-signal report (also emitted by intraday sometimes)
    if text.lower().startswith("no clean pattern") or text.lower().startswith("no pattern"):
        price_m = _re.search(r"price\s+\$?([\d.]+)", text, flags=_re.IGNORECASE)
        rsi_m = _re.search(r"RSI\s+(\d+)", text, flags=_re.IGNORECASE)
        lines = [f"⚡ {_ny_hhmm(ts)}", "", "No clean intraday pattern"]
        if price_m and rsi_m:
            lines.append(f"Price: ${float(price_m.group(1)):.2f} | RSI: {rsi_m.group(1)}")
        return "\n".join(lines)

    timeframe_m = _re.search(r"\((\d+[mh])\)", text, flags=_re.IGNORECASE)
    timeframe = timeframe_m.group(1) if timeframe_m else None

    # Pattern name: the first word, typically lowercase ("breakdown", "wedge", etc.)
    # but skip if it's "no" (handled above) or another non-pattern word
    name_m = _re.match(r"([a-z]+)", text, flags=_re.IGNORECASE)
    pattern_name = name_m.group(1) if name_m else None
    if pattern_name and pattern_name.lower() in ("no", "none", "the", "a"):
        pattern_name = None

    dir_m = _re.search(r"\b(UP|DOWN|FLAT)\b", text, flags=_re.IGNORECASE)
    direction = dir_m.group(1).upper() if dir_m else None

    level_m = _re.search(r"@\s*\$?([\d.]+)", text)
    level = float(level_m.group(1)) if level_m else None

    conf_m = _re.search(r"conf[=:\s]+(\d+)%?", text, flags=_re.IGNORECASE)
    conf = int(conf_m.group(1)) if conf_m else None

    # Need at minimum a direction or a pattern name to make a meaningful burst
    if not direction and not pattern_name:
        return None

    prose = text.rsplit("·", 1)[-1].strip().rstrip(".") if text.count("·") >= 2 else ""
    reasons = _split_reasons(prose, 3) if prose else []

    lines = [f"⚡ {_ny_hhmm(ts)}", ""]
    if timeframe and pattern_name:
        lines.append(f"{timeframe} | {pattern_name}")
    elif pattern_name:
        lines.append(pattern_name)
    elif timeframe:
        lines.append(timeframe)

    if direction or level is not None:
        dir_word = {"UP": "Breakout", "DOWN": "Breakdown", "FLAT": "Flat"}.get(direction, direction)
        dir_emoji = {"UP": "📈", "DOWN": "📉", "FLAT": "↔️"}.get(direction, "")
        sig_line = f"{dir_emoji} {dir_word}" if direction else ""
        if level is not None:
            sig_line = (sig_line + f" @ ${level:.2f}") if sig_line else f"@ ${level:.2f}"
        if conf is not None:
            sig_line += f" ({conf}%)"
        lines.append(sig_line.strip())

    if reasons:
        lines.append("")
        lines.extend(f"• {r}" for r in reasons)
    return "\n".join(lines)


def _try_pattern_burst(content: str, ts: str) -> str | None:
    """Daily Pattern emits 'No clean pattern on 30d chart — price $22.32 RSI 40.'
    or pattern-detection lines similar to intraday. Keep terse."""
    stripped = content.strip()
    if stripped.lower().startswith("no clean pattern"):
        import re as _re
        price_m = _re.search(r"price\s+\$?([\d.]+)", stripped, flags=_re.IGNORECASE)
        rsi_m = _re.search(r"RSI\s+(\d+)", stripped, flags=_re.IGNORECASE)
        lines = [f"🎯 {_ny_hhmm(ts)}", "", "No clean pattern (30d)"]
        if price_m and rsi_m:
            lines.append(f"Price: ${float(price_m.group(1)):.2f} | RSI: {rsi_m.group(1)}")
        return "\n".join(lines)
    # Fall back to intraday-style parser for detected patterns
    return _try_pattern_intraday_burst(content, ts)


def _try_newsie_burst(content: str, ts: str) -> str | None:
    """Newsie emits something like:
        'composite=+0.13 (neutral) · 15 articles · <headline>...'

    Parses field-by-field so a partial match still produces a degraded burst.
    Returns None only if neither score nor headline can be extracted (the two
    minimum signals — without either there's nothing meaningful to send).
    """
    import re as _re
    text = content.strip()

    score_m = _re.search(r"composite[=:\s]+([+-]?\d+\.\d+)", text, flags=_re.IGNORECASE)
    score = float(score_m.group(1)) if score_m else None

    label_m = _re.search(r"\(\s*(bullish|bearish|neutral|positive|negative)\s*\)",
                        text, flags=_re.IGNORECASE)
    # Fallback: infer label from score sign if explicit label missing
    label = label_m.group(1).upper() if label_m else None
    if label is None and score is not None:
        label = "BULLISH" if score > 0.1 else ("BEARISH" if score < -0.1 else "NEUTRAL")

    articles_m = _re.search(r"(\d+)\s+articles?", text, flags=_re.IGNORECASE)
    n_articles = int(articles_m.group(1)) if articles_m else None

    # Headline: prefer the last "· "-delimited segment; otherwise the last sentence
    headline = ""
    if "·" in text:
        headline = text.rsplit("·", 1)[-1].strip()
    if not headline and score_m:
        headline = text[score_m.end():].strip(" ·")

    if score is None and not headline:
        return None  # nothing to say

    label_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪",
                   "POSITIVE": "🟢", "NEGATIVE": "🔴"}.get(label, "⚪")

    lines = [f"📰 {_ny_hhmm(ts)}", ""]

    # Sentiment summary line — emit whichever fields survived
    summary_parts = []
    if label:
        summary_parts.append(f"{label_emoji} {label}")
    if score is not None:
        summary_parts.append(f"({score:+.2f})")
    if n_articles is not None:
        summary_parts.append(f"· {n_articles} articles")
    if summary_parts:
        lines.append(" ".join(summary_parts))

    if headline:
        headline_short = headline.split(".")[0].strip()
        if len(headline_short) > 120:
            cut = headline_short.rfind(" ", 0, 120)
            headline_short = headline_short[:cut if cut > 0 else 120].rstrip() + "…"
        lines.append(f'"{headline_short}"')

    return "\n".join(lines)


def _try_cto_burst(content: str, ts: str) -> str | None:
    """CTO DV emits multi-line thesis intelligence. Extract any of:
        - numeric score + star rating + change vs prior
        - immunity check (passing/failing)
        - net cash %
        - Altman Z (bankruptcy distance)
        - insider 3y buys ($)
    Emits whatever survived (resilient/partial). Returns None only if no
    DV score line at all — that's the minimum signal for a CTO burst.

    Each parser pulls fields independently so prose phrasing can drift
    without breaking the burst (mirrors the resilient-parsing approach
    used elsewhere in this module).
    """
    import re as _re

    # Optional ticker prefix is captured so multi-ticker bursts can label
    # each headline. Falls back to "GME" when absent — the GME-only era
    # didn't write the ticker into the brief.
    score_m = _re.search(
        r"(?:([A-Z]{1,6})\s+)?DV\s*Score:\s*([\d.]+)/100\s*([★☆]+)?\s*=?\s*(\w+)?",
        content, flags=_re.IGNORECASE
    )
    if not score_m:
        return None  # minimum signal missing
    ticker = (score_m.group(1) or "GME").upper()
    score = float(score_m.group(2))
    stars = score_m.group(3) or ""
    delta = score_m.group(4) or ""

    # Immunity: 'Immunity 4/5: ✗ Debt-free · ✓ Cash>$1B · ...'
    imm_m = _re.search(r"Immunity\s+(\d+)/(\d+):\s*(.+?)(?:\n|$)", content, flags=_re.IGNORECASE)
    immunity_passed = imm_m.group(1) if imm_m else None
    immunity_total = imm_m.group(2) if imm_m else None
    immunity_failing = []
    if imm_m:
        immunity_failing = [seg.strip().lstrip("✗").strip()
                            for seg in imm_m.group(3).split("·")
                            if seg.strip().startswith("✗")]

    # Net Cash: 'Net Cash 46.4%' from the Inputs line
    net_cash_m = _re.search(r"Net Cash\s+([\d.]+)%", content, flags=_re.IGNORECASE)
    net_cash = float(net_cash_m.group(1)) if net_cash_m else None

    # Altman Z: 'Altman Z 8.3' — bankruptcy distance (>3 = safe, <1.8 = distress)
    altman_m = _re.search(r"Altman\s*Z\s+([\d.]+)", content, flags=_re.IGNORECASE)
    altman_z = float(altman_m.group(1)) if altman_m else None

    # Insider 3y dollars: 'Insider 3y buys: 21 purchases / $44.2M'
    insider_m = _re.search(
        r"Insider\s+3y\s+buys?:\s*(\d+)\s*purchases?\s*/\s*\$?([\d.]+[MK]?)",
        content, flags=_re.IGNORECASE
    )
    insider_count = int(insider_m.group(1)) if insider_m else None
    insider_dollars = insider_m.group(2) if insider_m else None

    # Short Vol: 'Short Vol: 58% (30d avg 61%, as of 2026-05-13)' from FINRA
    # Reg SHO. Tolerates an optional ticker prefix ("GME Short Vol: ...")
    # added when multi-ticker bursts are stacked.
    sv_m = _re.search(
        r"(?:[A-Z]{1,6}\s+)?Short\s*Vol:\s*([\d.]+)%\s*\(30d\s*avg\s+([\d.]+)%",
        content, flags=_re.IGNORECASE
    )
    short_vol_today = float(sv_m.group(1)) if sv_m else None
    short_vol_avg = float(sv_m.group(2)) if sv_m else None

    lines = [f"🛡️ {_ny_hhmm(ts)}", ""]

    # Headline: ticker + score + delta. Ticker prefix is mandatory so
    # multi-ticker bursts stay unambiguous when stacked one after another.
    headline = f"{ticker} DV: {score:.1f}/100"
    if stars:
        headline += f" {stars}"
    if delta:
        headline += f" ({delta})"
    lines.append(headline)

    # Capital health: net cash + altman z on one line if both present
    cap_parts = []
    if net_cash is not None:
        cap_parts.append(f"Net Cash: {net_cash:.0f}%")
    if altman_z is not None:
        cap_parts.append(f"Altman Z: {altman_z:.1f}")
    if cap_parts:
        lines.append(" | ".join(cap_parts))

    # Insider conviction
    if insider_count is not None and insider_dollars:
        lines.append(f"Insider 3y: {insider_count} buys / ${insider_dollars}")

    # Short volume (FINRA Reg SHO consolidated feed). Arrow shows today vs.
    # 30-day baseline — ↑ = pressure building, ↓ = pressure easing. 2pp
    # threshold filters daily noise; only material moves get an arrow.
    if short_vol_today is not None and short_vol_avg is not None:
        delta = short_vol_today - short_vol_avg
        arrow = "↑" if delta > 2 else "↓" if delta < -2 else "→"
        lines.append(f"Short Vol: {short_vol_today:.0f}% {arrow} (30d {short_vol_avg:.0f}%)")

    # Immunity (shown last so failing items pop). Status emoji leads:
    # 🟢 all checks pass (5/5), 🟡 any check fails. Color signals
    # whether the thesis still has structural immunity at a glance.
    if immunity_passed and immunity_total:
        try:
            all_passing = int(immunity_passed) >= int(immunity_total)
        except ValueError:
            all_passing = True
        imm_emoji = "🟢" if all_passing else "🟡"
        imm_line = f"{imm_emoji} Immunity: {immunity_passed}/{immunity_total}"
        if immunity_failing:
            imm_line += f" (✗ {', '.join(immunity_failing[:2])})"
        lines.append(imm_line)

    # Plain-English read of the numbers (LLM-generated thesis interpretation
    # written by orchestrator.run_cto_dv_score). Pulled out to its own labelled
    # footer so a non-quant reader gets a one-line synthesis under the raw
    # scorecard. Skipped silently when absent (deterministic non-GME tickers).
    read_m = _re.search(r"READ:\s*(.+?)(?:\n|$)", content, flags=_re.IGNORECASE)
    if read_m:
        lines.append("")
        lines.append(f"📝 READ: {read_m.group(1).strip()}")

    return "\n".join(lines)


_BURST_DISPATCH = {
    "Synthesis": _try_synthesis_burst,
    "Trendy": _try_trendy_burst,
    "Futurist": _try_futurist_burst,
    "Pattern Intraday": _try_pattern_intraday_burst,
    "Pattern": _try_pattern_burst,
    "Newsie": _try_newsie_burst,
    "CTO": _try_cto_burst,
    # Chatty deliberately omitted — keeps free prose voice
}


def _format(v: Voice, content: str, ts: str, prev_state: dict | None = None) -> str:
    # Strip HTML-special chars that break Telegram parse_mode=HTML
    safe = (content or "").replace("<", "&lt;").replace(">", "&gt;").strip()

    # Burst-format path for structured-output agents. Returns the full message
    # (with its own header + timestamp) so callers should NOT re-wrap. Falls
    # through to the legacy narrative formatter when None is returned.
    burst_fn = _BURST_DISPATCH.get(v.agent_name)
    if burst_fn:
        try:
            burst = burst_fn(safe, ts)
            if burst:
                # Append plain-English glosses for trading jargon on burst-formatted
                # agents too — same machinery the legacy path uses below. Empty
                # footer when no glossary terms detected.
                try:
                    from trading_glossary import glossary_footer
                    burst_footer = glossary_footer(safe)
                except Exception:
                    burst_footer = ""
                return f"{burst}\n\n<i>{burst_footer}</i>" if burst_footer else burst
        except Exception as e:
            log.warning(f"[voice] burst format failed for {v.agent_name}: {e} — falling through")

    # Legacy narrative path (Chatty's prose, Newsie's commentary, etc.)
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
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
        today_et = datetime.now(_ET).date().isoformat()
    except Exception:
        today_et = ""
    # Match the burst-formatter time format ("HH:MM ET") so legacy-path
    # voices (Chatty) carry the same tz suffix as Synthesis/Trendy/etc.
    # Compare against today-in-ET (not server-local date) because the
    # host is BST — near ET midnight, server date is already the next
    # day and would mislabel "today" in ET timestamps.
    if len(ts) >= 16:
        ts_date = ts[:10]
        hhmm = ts[11:16]
        time_part = f"{hhmm} ET" if ts_date == today_et else f"{ts_date[5:]} {hhmm} ET"
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
    default_stale_cutoff = int(os.getenv("AGENT_VOICE_MAX_STALENESS_MIN", "30"))
    # Big enough to plow through multi-day backlogs in a few ticks; bounded
    # so a single tick can't stall on huge result sets.
    QUERY_LIMIT = 500

    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        for v in VOICES:
            # Per-voice staleness override; daily/rare agents need a wider window
            stale_cutoff_min = v.staleness_minutes if v.staleness_minutes is not None else default_stale_cutoff
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
            # Capture once per voice — used by state-diff suppression below.
            last_pushed_at = _get_last_pushed_at(conn, v)
            for row_id, ts, content in rows:
                if _is_stale(ts or "", stale_cutoff_min):
                    new_watermark = row_id
                    skipped_count += 1
                    continue
                if sent_count >= v.max_per_run:
                    break
                # Synthesis: low-consensus floor. Below 60% conviction is the
                # no-information regime ("CONSENSUS: NEUTRAL 50%" = coin flip).
                if v.agent_name == "Synthesis":
                    if _synthesis_low_consensus(content or "", min_pct=60):
                        log.info(f"[voice] Synthesis low consensus (<60%) — suppressed")
                        new_watermark = row_id
                        continue
                    # Synthesis: state-diff. Same price/dir/conf within tolerance
                    # AND we've pushed recently → suppress. Heartbeat at most every
                    # 60 min if state stays unchanged.
                    if _synthesis_unchanged_state(conn, row_id, content or "",
                                                    last_pushed_at):
                        log.info(f"[voice] Synthesis unchanged state — suppressed")
                        new_watermark = row_id
                        continue
                # Futurist: state-diff. Same dir/target/conf within tolerance
                # AND we've pushed recently → suppress. Heartbeat at most every
                # 60 min if state stays unchanged.
                if v.agent_name == "Futurist":
                    if _futurist_unchanged_state(conn, row_id, content or "",
                                                   last_pushed_at):
                        log.info(f"[voice] Futurist unchanged state — suppressed")
                        new_watermark = row_id
                        continue
                # Chatty echo suppression: if this Chatty row's bias matches
                # the most recent Synthesis (within 60s), don't re-notify.
                if v.agent_name == "Chatty":
                    echo_dir = _chatty_echoes_synthesis(conn, row_id, content or "", ts or "")
                    if echo_dir:
                        log.info(f"[voice] Chatty echo of Synthesis ({echo_dir}) — suppressed")
                        new_watermark = row_id
                        continue
                    # Chatty: state-diff. Same price within tolerance, no alarm
                    # words, recent push → suppress.
                    if _chatty_unchanged_state(conn, row_id, content or "",
                                                 last_pushed_at):
                        log.info(f"[voice] Chatty unchanged state — suppressed")
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
                # Stamp the push time so state-diff suppression can enforce
                # the heartbeat window on subsequent ticks. Update locally
                # too so the loop's next iteration sees the fresh timestamp.
                _set_watermark(conn, v, new_watermark)
                _mark_pushed_now(conn, v)
                conn.commit()
                last_pushed_at = datetime.now(timezone.utc)

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
