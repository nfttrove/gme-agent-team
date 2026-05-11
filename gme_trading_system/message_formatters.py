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
    # Labels — force uppercase
    for label in ("PRICE", "DATA", "NEWS", "PATTERN", "TREND", "PREDICTION",
                  "STRUCTURAL", "CONSENSUS", "SOCIAL", "GATE", "NOW", "NEXT"):
        brief = re.sub(rf"\b{label}\b\s*:", f"{label}:", brief, flags=re.IGNORECASE)
    # Directional words inside CONSENSUS / PREDICTION / TREND — uppercase
    for word in ("BULLISH", "BEARISH", "NEUTRAL", "UP", "DOWN", "SIDEWAYS",
                 "GREEN", "YELLOW", "RED", "BUY", "SELL", "HOLD"):
        brief = re.sub(rf"\b{word}\b", word, brief, flags=re.IGNORECASE)
    return brief
