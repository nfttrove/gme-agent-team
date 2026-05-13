"""Shared message formatters for Telegram / Discord / agent_voice output.

Single source of truth for human-readable presentation of common values:
prices, volume regimes, data quality, consensus rollups.

Design rule: keep canonical tokens intact for the Synthesis parser
(episodic_integration.extract_synthesis_from_output). Human-readable detail
goes in a parenthetical AFTER the canonical token, never replacing it.
"""
from __future__ import annotations

import html
import re


def escape_html(s: str) -> str:
    """Escape <, >, & for Telegram parse_mode=HTML.

    Most call sites embed user-derived or LLM-derived strings, which can
    contain accidental angle brackets that Telegram rejects with a 400.
    """
    return html.escape(s or "", quote=False)


def format_price(price: float, prev_close: float | None) -> str:
    """`$23.21 (-1.2% on day)` or `$23.21` if no baseline."""
    if not prev_close:
        return f"${price:.2f}"
    pct = (price - prev_close) / prev_close * 100
    return f"${price:.2f} ({pct:+.2f}% on day)"


def format_rsi(current: float | None, at_open: float | None = None) -> str:
    """`RSI 44 (was 51 at open)` or `RSI 44` if no anchor."""
    if current is None:
        return "RSI n/a"
    if at_open is None:
        return f"RSI {current:.0f}"
    return f"RSI {current:.0f} (was {at_open:.0f} at open)"


def format_volume(label: str, ratio: float) -> str:
    """`vol quiet (0.43x 20d ADV)`."""
    return f"vol {label} ({ratio:.2f}x 20d ADV)"


def format_data_status(quality: str, gap_s: float = 0, sources_down: int = 0) -> str:
    """`DATA: degraded (1 source down, 180s tick gap)` — canonical token first.

    Parser regex `DATA:\\s*(\\w+)` captures `degraded`; suffix is informational.
    """
    canonical = quality if quality in ("clean", "ok", "degraded") else "degraded"
    bits = []
    if sources_down:
        bits.append(f"{sources_down} source{'s' if sources_down != 1 else ''} down")
    if gap_s and gap_s >= 60:
        bits.append(f"{int(gap_s)}s tick gap")
    suffix = f" ({', '.join(bits)})" if bits else ""
    return f"DATA: {canonical}{suffix}"


def format_consensus(direction: str, pct: int, agreeing: int = 0, total: int = 0,
                     top_agent: str = "", top_conf: float = 0.0) -> str:
    """`CONSENSUS: BULLISH 65% (5/7 agents; Futurist 78%)` — token + suffix.

    Parser captures `BULLISH` and `65`. Suffix is informational only.
    """
    parts = []
    if total:
        parts.append(f"{agreeing}/{total} agents")
    if top_agent and top_conf:
        parts.append(f"{top_agent} {top_conf:.0%}")
    suffix = f" ({'; '.join(parts)})" if parts else ""
    return f"CONSENSUS: {direction.upper()} {pct}%{suffix}"


# Vocabulary tightening for LLM post-processing
_VERBOSE_PHRASES = (
    (re.compile(r"\bindicating (?:a )?(?:lack|absence) of (?:strong )?directional conviction\b",
                re.IGNORECASE), "indecisive"),
    (re.compile(r"\bdirectional conviction\b", re.IGNORECASE), "direction"),
    (re.compile(r"\black of\b", re.IGNORECASE), "no"),
    (re.compile(r"\bin order to\b", re.IGNORECASE), "to"),
    (re.compile(r"\bat this (?:point in )?time\b", re.IGNORECASE), "now"),
)


def tighten_prose(text: str) -> str:
    """Replace verbose stock phrases with terse equivalents.

    Belt-and-braces against LLM verbosity when prompt constraints are
    ignored. Idempotent — safe to apply to already-clean strings.
    """
    if not text:
        return text
    for pat, repl in _VERBOSE_PHRASES:
        text = pat.sub(repl, text)
    return text


def normalize_synthesis_capitalization(brief: str) -> str:
    """Post-LLM normalizer: uppercase canonical labels, Title-case values.

    Labels MUST stay uppercase to satisfy the parser. Values like
    `bearish`, `BEARISH`, `Bearish` get standardized to `BEARISH` for
    the directional terms the parser already requires uppercase.
    """
    if not brief:
        return brief
    # Labels — force uppercase (extended for SIGNAL row labels too)
    for label in ("PRICE", "DATA", "NEWS", "PATTERN", "TREND", "PREDICTION",
                  "STRUCTURAL", "CONSENSUS", "SOCIAL", "GATE", "NOW", "NEXT",
                  "SIGNAL"):
        brief = re.sub(rf"\b{label}\b\s*:", f"{label}:", brief, flags=re.IGNORECASE)
    # Directional words — uppercase. Include SIGNAL actions (BUY/SELL/HOLD/WAIT).
    for word in ("BULLISH", "BEARISH", "NEUTRAL", "UP", "DOWN", "SIDEWAYS",
                 "GREEN", "YELLOW", "RED", "BUY", "SELL", "HOLD", "WAIT"):
        brief = re.sub(rf"\b{word}\b", word, brief, flags=re.IGNORECASE)
    return brief


# Map qualitative trend-strength words to numeric strengths so the parser regex
# `TREND: (\w+) ([\d.]+)` still captures a number even when the LLM disobeys.
_TREND_STRENGTH_WORDS = {
    "strong": "0.8",
    "moderate": "0.6",
    "weak": "0.4",
    "flat": "0.2",
}


def coerce_trend_strength(brief: str) -> str:
    """Replace qualitative trend strengths with numbers.

    `TREND: UP strong` → `TREND: UP 0.8`. Idempotent. No-op when already numeric.
    """
    if not brief:
        return brief

    def _sub(match):
        direction = match.group(1)
        word = match.group(2).lower()
        num = _TREND_STRENGTH_WORDS.get(word, "0.5")
        return f"TREND: {direction} {num}"

    return re.sub(
        rf"TREND:\s*(UP|DOWN|SIDEWAYS)\s+({'|'.join(_TREND_STRENGTH_WORDS)})\b",
        _sub,
        brief,
        flags=re.IGNORECASE,
    )


# Display-layer emoji prefixes for the canonical status words. The text stays
# in place so the parser keeps ingesting (`STRUCTURAL: YELLOW` regex still
# matches); the emoji is purely a visual scannability win for Telegram readers.
_FIELD_EMOJIS = {
    "STRUCTURAL": {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"},
    "CONSENSUS":  {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"},
    "PREDICTION": {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪",
                   "HOLD": "🟡", "BUY": "🟢", "SELL": "🔴"},
    "SIGNAL":     {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "WAIT": "⏳"},
    "TREND":      {"UP": "📈", "DOWN": "📉", "SIDEWAYS": "↔️"},
}

# Detect any emoji we'd prepend, so the helper is idempotent (won't double-prefix
# if called twice on the same text — e.g. forwarder ran the text through once
# already and a later layer calls it again).
_PREPENDED_EMOJIS = "🟢🟡🔴⚪⏳📈📉↔️"


def layout_synthesis_brief(text: str, prev_state: dict | None = None) -> str:
    """Reformat the 3-line NOW/NEXT/SIGNAL brief for mobile readability.

    SIGNAL goes to the top (bold), then NOW and NEXT each get bullet-listed
    fields one per line. Single-line non-synthesis content passes through
    unchanged so this is safe to call on every agent voice.

    If `prev_state` is provided with 'consensus' and/or 'signal' keys and
    they differ from the current brief, prefixes the SIGNAL line with a
    ⚡ FLIP marker plus the prior value — material changes pop out.
    """
    if not text or "NOW:" not in text.upper() or "SIGNAL:" not in text.upper():
        return text

    lines = [ln for ln in text.split("\n") if ln.strip()]
    now_line = next_line = signal_line = None
    other_lines = []
    for ln in lines:
        u = ln.strip().upper()
        if u.startswith("NOW:"):
            now_line = ln.strip()
        elif u.startswith("NEXT:"):
            next_line = ln.strip()
        elif u.startswith("SIGNAL:"):
            signal_line = ln.strip()
        else:
            other_lines.append(ln.strip())

    def _split_fields(body: str) -> list[str]:
        return [f.strip() for f in body.split("|") if f.strip()]

    parts: list[str] = []

    # SIGNAL on top, bold, optional ⚡ FLIP prefix on direction change
    if signal_line:
        flip_prefix = ""
        if prev_state:
            cur_signal = _extract_signal_action(signal_line)
            cur_consensus = _extract_consensus_dir(next_line or "")
            prev_signal = prev_state.get("signal")
            prev_consensus = prev_state.get("consensus")
            flips = []
            if prev_signal and cur_signal and prev_signal != cur_signal:
                flips.append(f"SIGNAL {prev_signal}→{cur_signal}")
            if prev_consensus and cur_consensus and prev_consensus != cur_consensus:
                flips.append(f"CONSENSUS {prev_consensus}→{cur_consensus}")
            if flips:
                flip_prefix = f"⚡ FLIP ({', '.join(flips)})\n"
        parts.append(f"{flip_prefix}<b>{signal_line}</b>")

    if now_line:
        body = now_line[len("NOW:"):].strip()
        parts.append("📊 <b>NOW</b>")
        parts.extend(f"• {f}" for f in _split_fields(body))

    if next_line:
        body = next_line[len("NEXT:"):].strip()
        parts.append("🔮 <b>NEXT</b>")
        parts.extend(f"• {f}" for f in _split_fields(body))

    parts.extend(other_lines)
    return "\n".join(parts)


def _extract_signal_action(signal_line: str) -> str | None:
    m = re.search(r"SIGNAL:\s*(?:[⚡🟢🟡🔴⚪⏳📈📉]+\s*)*(\w+)", signal_line, flags=re.IGNORECASE)
    return m.group(1).upper() if m else None


def _extract_consensus_dir(next_line: str) -> str | None:
    m = re.search(r"CONSENSUS:\s*(?:[🟢🟡🔴⚪]+\s*)*(\w+)", next_line, flags=re.IGNORECASE)
    return m.group(1).upper() if m else None


def decimal_confidence_to_percent(text: str) -> str:
    """Display-layer: rewrite TREND/PREDICTION decimal strengths as percentages.

    Storage stays canonical (`TREND: DOWN 0.55`, parser expects 0.0-1.0).
    Display shows `TREND: DOWN 55%` so it reads consistently with CONSENSUS.
    Only converts decimals < 1.0; integers and already-% values pass through.
    """
    if not text:
        return text

    def _sub(match):
        label = match.group("label")
        word = match.group("word")
        num_str = match.group("num")
        try:
            num = float(num_str)
        except ValueError:
            return match.group(0)
        if num >= 1.0:
            return match.group(0)
        return f"{label}: {word} {int(round(num * 100))}%"

    return re.sub(
        r"(?P<label>TREND|PREDICTION):\s*(?P<word>\w+)\s+(?P<num>0?\.\d+)\b",
        _sub,
        text,
    )


def colorize_status_emojis(text: str) -> str:
    """Prepend a colored emoji before each canonical status word.

    Display-layer transform — the original word stays so the Synthesis parser
    (regex-anchored on the word) keeps working. Idempotent: won't double-prefix.

    Example:
        STRUCTURAL: YELLOW (consolidating) → STRUCTURAL: 🟡 YELLOW (consolidating)
        SIGNAL: BUY @ $22.50 (...) → SIGNAL: 🟢 BUY @ $22.50 (...)
    """
    if not text:
        return text
    for label, mapping in _FIELD_EMOJIS.items():
        for word, emoji in mapping.items():
            # (?<![emoji-chars]\s) — skip if an emoji + space is already in front of WORD
            # Approach instead: require label followed by *only whitespace* then WORD.
            # If an emoji is already prefixed, the pattern won't match (because
            # there'd be non-whitespace between `:` and WORD).
            pattern = rf"({label}:\s*)({word})\b"
            text = re.sub(pattern, rf"\1{emoji} \2", text)
    return text


def coerce_news_score(brief: str) -> str:
    """Convert NEWS scores written as percentages back into the -1.0 to 1.0 range.

    SynthesisBrief.news_sentiment requires a float in [-1.0, 1.0]. If the LLM
    writes `NEWS: BULLISH 75%`, Pydantic rejects 75.0 and the field drops from
    episodic memory. This helper rewrites `NN%` to `0.NN` and `-NN%` to `-0.NN`.
    No-op when the score is already in range.
    """
    if not brief:
        return brief

    def _sub(match):
        label = match.group("label")
        sign = match.group("sign") or ""
        pct = int(match.group("pct"))
        # Clamp to 100% just in case, then scale to decimal.
        pct = min(pct, 100)
        decimal = f"{sign}{pct/100:.2f}"
        return f"NEWS: {label} {decimal}"

    return re.sub(
        r"NEWS:\s*(?P<label>\w+)\s+(?P<sign>-?)(?P<pct>\d+)%",
        _sub,
        brief,
        flags=re.IGNORECASE,
    )


def clamp_consensus_pct(brief: str, ceiling: int = 95) -> str:
    """Cap CONSENSUS percentage at `ceiling` so the LLM cannot publish '100%'.

    100% reads as overclaim — real teams rarely have unanimous conviction, and
    the round number erodes reader trust. Clamping to 95 keeps the message
    honest. The parser captures whatever number remains, so this is safe.
    """
    if not brief:
        return brief

    def _sub(match):
        direction = match.group("dir")
        pct = int(match.group("pct"))
        if pct > ceiling:
            pct = ceiling
        return f"CONSENSUS: {direction} {pct}%"

    return re.sub(
        r"CONSENSUS:\s*(?P<dir>\w+)\s+(?P<pct>\d+)%",
        _sub,
        brief,
        flags=re.IGNORECASE,
    )
