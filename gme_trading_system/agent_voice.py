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
    structural = _grab(r"STRUCTURAL:\s*(GREEN|CAUTION|YELLOW|RED)")

    consensus_dir = consensus.group(1).upper() if consensus else None
    consensus_pct = int(consensus.group(2)) if consensus else None
    signal_action = signal.group(1).upper() if signal else None
    trend_dir = trend.group(1).upper() if trend else None
    struct_state = structural.group(1).upper() if structural else None

    cons_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(consensus_dir, "⚪")
    sig_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "WAIT": "⏳"}.get(signal_action, "⏳")
    trend_emoji = {"UP": "📈", "DOWN": "📉", "SIDEWAYS": "↔️"}.get(trend_dir, "↔️")
    struct_emoji = {"GREEN": "🟢", "CAUTION": "🟡", "YELLOW": "🟡", "RED": "🔴"}.get(struct_state, "⚪")

    lines = [f"🧠 SYNTHESIS | {_ny_hhmm(ts)}", ""]
    if consensus_dir and consensus_pct is not None:
        lines.append(f"Consensus: {cons_emoji} {consensus_dir} ({consensus_pct}%)")
    if signal_action:
        lines.append(f"Signal: {sig_emoji} {signal_action}")
    if trend_dir:
        lines.append(f"Trend: {trend_emoji} {trend_dir}")
    if struct_state:
        lines.append(f"Structure: {struct_emoji} {struct_state}")

    # Body must have at least one field beyond the header to be worth emitting
    if len(lines) <= 2:
        return None
    return "\n".join(lines)


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
    lines = [f"📈 TRENDY | {_ny_hhmm(ts)}", "", header_line]
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
    lines = [f"🔮 FUTURIST | {_ny_hhmm(ts)}", "", header_line]
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

    Parses field-by-field so a partial match still produces a degraded burst.
    Returns None only if no recognizable pattern/direction can be extracted.
    """
    import re as _re
    text = content.strip()

    timeframe_m = _re.search(r"\((\d+[mh])\)", text, flags=_re.IGNORECASE)
    timeframe = timeframe_m.group(1) if timeframe_m else None

    # Pattern name: the first word, typically lowercase ("breakdown", "wedge", etc.)
    name_m = _re.match(r"(\w+)", text)
    pattern_name = name_m.group(1) if name_m else None

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

    lines = [f"⚡ INTRADAY | {_ny_hhmm(ts)}", ""]
    if timeframe and pattern_name:
        lines.append(f"{timeframe} | {pattern_name}")
    elif pattern_name:
        lines.append(pattern_name)
    elif timeframe:
        lines.append(timeframe)

    if direction or level is not None:
        dir_emoji = {"UP": "📈", "DOWN": "📉", "FLAT": "↔️"}.get(direction, "")
        sig_line = f"{dir_emoji} {direction}" if direction else ""
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
        lines = [f"🎯 PATTERN | {_ny_hhmm(ts)}", "", "No clean pattern (30d)"]
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

    lines = [f"📰 NEWSIE | {_ny_hhmm(ts)}", ""]

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
    """CTO Trove emits multi-line: 'GME Trove Score: 65.4/100 ★★★★☆ = unchanged ...'"""
    import re as _re
    m = _re.search(
        r"GME Trove Score:\s*([\d.]+)/100\s*([★☆]+)\s*=\s*(\w+)",
        content, flags=_re.IGNORECASE
    )
    if not m:
        return None
    score = float(m.group(1))
    stars = m.group(2)
    delta = m.group(3)
    # Optional immunity line: 'Immunity 4/5: ...'
    imm_m = _re.search(r"Immunity\s+(\d+)/(\d+):\s*(.+?)(?:\n|$)", content, flags=_re.IGNORECASE)
    immunity_line = None
    if imm_m:
        passed, total = imm_m.group(1), imm_m.group(2)
        failing = [seg.strip() for seg in imm_m.group(3).split("·")
                   if seg.strip().startswith("✗")]
        immunity_line = f"Immunity: {passed}/{total}"
        if failing:
            immunity_line += f" ({', '.join(failing)})"
    lines = [f"🛡️ CTO | {_ny_hhmm(ts)}", "",
             f"Trove Score: {score}/100 {stars} ({delta})"]
    if immunity_line:
        lines.append(immunity_line)
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
                return burst
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
