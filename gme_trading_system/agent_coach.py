"""Diagnose why a muted agent is failing, using Gemini Pro as the analyst.

Collects the agent's recent signals + their actual outcomes from the DB,
groups by signal type (BULL vs BEAR), and asks Pro to find the failure
pattern + suggest a fix. Triggered by the `/coach AGENT` Telegram command.

This is the improvement loop the gate is missing: the gate decides who to
trust, the coach explains why the bad ones are bad so a human can fix them.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

SAMPLE_LIMIT = 50  # most recent N resolved signals
MIN_SCORED_SIGNALS = 5  # need at least this many to bother diagnosing
PRO_MODEL = "gemini-2.5-pro"
# Pro's internal "thinking" budget eats into max_output_tokens. The shared
# llm_generate_gemini doesn't cap it, so we bypass it and call the raw client
# with an explicit budget — caps thinking spend AND guarantees output room.
PRO_THINKING_BUDGET = 2048
PRO_MAX_OUTPUT_TOKENS = 4096


@dataclass
class CoachReport:
    ok: bool
    agent_name: str
    sample_size: int = 0
    overall_hit_rate: float | None = None
    bull_hit_rate: float | None = None
    bear_hit_rate: float | None = None
    diagnosis: str = ""
    suggestion: str = ""
    reason_if_failed: str = ""


def resolve_agent_name(db_path: str, query: str) -> str | None:
    """Match `query` (case-insensitive substring) against agents in signal_scores.

    Returns the canonical agent name if exactly one matches, or None on
    no match or multiple matches.
    """
    needle = query.strip().lower()
    if not needle:
        return None
    with sqlite3.connect(db_path) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT agent_name FROM signal_scores"
        ).fetchall()]
    exact = [n for n in names if n.lower() == needle]
    if exact:
        return exact[0]
    contains = [n for n in names if needle in n.lower()]
    if len(contains) == 1:
        return contains[0]
    return None


def _collect_signals(db_path: str, agent_name: str) -> list[dict]:
    """Pull the last N scored signals for this agent.

    Direction (BULL/BEAR) is derived from the TP/SL geometry — most agents
    write signal_type as the task name (e.g. 'intraday_pattern_signal'),
    not the direction, so we infer: TP>entry & SL<entry = BULL, opposite
    = BEAR. Confidence lives on signal_alerts; left-join to pick it up.
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT s.signal_type AS raw_type, a.confidence,
                      s.directional_hit, s.tp_hit,
                      s.baseline_price, s.end_price, s.validated_at,
                      a.entry_price, a.stop_loss, a.take_profit
                 FROM signal_scores s
                 LEFT JOIN signal_alerts a ON a.id = s.signal_id
                WHERE s.agent_name = ?
                  AND s.baseline_price IS NOT NULL
                  AND s.baseline_price != s.end_price
                ORDER BY s.validated_at DESC LIMIT ?""",
            (agent_name, SAMPLE_LIMIT),
        ).fetchall()
    enriched = []
    for r in rows:
        d = dict(r)
        d["signal_type"] = _derive_direction(d)
        enriched.append(d)
    return enriched


def _derive_direction(row: dict) -> str:
    """Map raw signal type + TP/SL geometry to BULL/BEAR/UNKNOWN."""
    raw = (row.get("raw_type") or "").upper()
    if raw in ("BULL", "BULLISH"):
        return "BULL"
    if raw in ("BEAR", "BEARISH"):
        return "BEAR"
    # Fall back to TP/SL geometry
    entry = row.get("entry_price")
    tp = row.get("take_profit")
    sl = row.get("stop_loss")
    if entry and tp and sl:
        if tp > entry and sl < entry:
            return "BULL"
        if tp < entry and sl > entry:
            return "BEAR"
    return "UNKNOWN"


def _bucket_stats(signals: list[dict]) -> dict:
    bull = [s for s in signals if (s.get("signal_type") or "").upper() in ("BULL", "BULLISH")]
    bear = [s for s in signals if (s.get("signal_type") or "").upper() in ("BEAR", "BEARISH")]
    def rate(rows: list[dict]) -> float | None:
        scored = [r for r in rows if r.get("directional_hit") is not None]
        if not scored:
            return None
        return sum(int(r["directional_hit"]) for r in scored) / len(scored)
    return {
        "overall": rate(signals),
        "bull": rate(bull),
        "bear": rate(bear),
        "n_bull": len(bull),
        "n_bear": len(bear),
    }


def _format_sample_for_prompt(signals: list[dict], max_rows: int = 30) -> str:
    """Compact one-line-per-signal summary for the prompt."""
    lines = []
    for s in signals[:max_rows]:
        direction = (s.get("signal_type") or "?").upper()[:4]
        hit = "✓" if s.get("directional_hit") == 1 else "✗" if s.get("directional_hit") == 0 else "?"
        baseline = s.get("baseline_price")
        end = s.get("end_price")
        move_pct = ""
        if baseline and end:
            move_pct = f" ({(end - baseline) / baseline * 100:+.1f}%)"
        conf = f"{int(round((s.get('confidence') or 0) * 100))}%"
        ts = (s.get("validated_at") or "")[:16]
        lines.append(f"  {ts} {direction:4s} conf={conf:>4s} → {hit}{move_pct}")
    return "\n".join(lines)


def _build_prompt(agent_name: str, signals: list[dict], stats: dict) -> str:
    """Build the Pro-tier diagnostic prompt."""
    bull_pct = f"{stats['bull']:.0%}" if stats['bull'] is not None else "n/a"
    bear_pct = f"{stats['bear']:.0%}" if stats['bear'] is not None else "n/a"
    overall_pct = f"{stats['overall']:.0%}" if stats['overall'] is not None else "n/a"
    return (
        f"You are diagnosing a trading agent's signal-generation logic.\n\n"
        f"AGENT: {agent_name}\n"
        f"30-day stats: overall hit rate {overall_pct} on n={len(signals)} resolved signals\n"
        f"  • BULL signals: {bull_pct} hit rate (n={stats['n_bull']})\n"
        f"  • BEAR signals: {bear_pct} hit rate (n={stats['n_bear']})\n\n"
        f"Recent signals (most recent first; ✓=direction correct, ✗=wrong; "
        f"move % is what GME actually did over the window):\n"
        f"{_format_sample_for_prompt(signals)}\n\n"
        f"Analyze this data and respond in exactly this structure (no preamble, "
        f"no markdown, no bullets — three labelled sections only):\n\n"
        f"PATTERN:\n"
        f"<2-3 sentences naming the specific failure pattern you see. Is the "
        f"agent wrong on BULL more than BEAR? Wrong only in certain price "
        f"regimes? Consistently late? Inverted? Be specific to THIS agent's "
        f"data, not generic.>\n\n"
        f"HYPOTHESIS:\n"
        f"<1-2 sentences on the likely cause. E.g. 'breakouts at resistance "
        f"are being called BULL when GME tends to fade them in low-vol' or "
        f"'signals fire too late — by the time we see them the move is over'.>\n\n"
        f"SUGGESTION:\n"
        f"<One concrete actionable fix. A prompt edit, a parameter change, "
        f"or a feature to add. Keep it implementable.>"
    )


def _parse_diagnosis(raw: str) -> tuple[str, str]:
    """Pull (diagnosis, suggestion) from Pro's structured output."""
    if not raw:
        return ("", "")
    text = raw.strip()
    pattern = ""
    hypothesis = ""
    suggestion = ""
    sections = {"PATTERN:": "pattern", "HYPOTHESIS:": "hypothesis", "SUGGESTION:": "suggestion"}
    current = None
    buckets: dict[str, list[str]] = {"pattern": [], "hypothesis": [], "suggestion": []}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                buckets[current].append("")
            continue
        upper = stripped.upper()
        matched = False
        for label, key in sections.items():
            if upper.startswith(label):
                current = key
                rest = stripped[len(label):].strip()
                if rest:
                    buckets[key].append(rest)
                matched = True
                break
        if matched:
            continue
        if current:
            buckets[current].append(stripped)
    pattern = " ".join(buckets["pattern"]).strip()
    hypothesis = " ".join(buckets["hypothesis"]).strip()
    suggestion = " ".join(buckets["suggestion"]).strip()
    diagnosis = pattern
    if hypothesis:
        diagnosis = (diagnosis + ("\n\n" if diagnosis else "") + "Likely cause: " + hypothesis).strip()
    return (diagnosis, suggestion)


def diagnose_agent(db_path: str, agent_query: str, llm_caller=None) -> CoachReport:
    """End-to-end: resolve agent, collect signals, ask Pro, parse, return report.

    `llm_caller` is an injectable function with the signature of
    llm_config.llm_generate_gemini (used so tests can stub the network call).
    """
    canonical = resolve_agent_name(db_path, agent_query)
    if not canonical:
        return CoachReport(
            ok=False, agent_name=agent_query,
            reason_if_failed=f"no agent matches '{agent_query}' (try one of: see /standup)",
        )

    signals = _collect_signals(db_path, canonical)
    if len(signals) < MIN_SCORED_SIGNALS:
        return CoachReport(
            ok=False, agent_name=canonical, sample_size=len(signals),
            reason_if_failed=f"only {len(signals)} scored signals — need ≥{MIN_SCORED_SIGNALS} to diagnose",
        )

    stats = _bucket_stats(signals)
    prompt = _build_prompt(canonical, signals, stats)

    if llm_caller is None:
        llm_caller = _call_pro_with_bounded_thinking
    try:
        raw = llm_caller(prompt, model=PRO_MODEL, num_predict=PRO_MAX_OUTPUT_TOKENS, temperature=0.3)
    except Exception as e:
        return CoachReport(
            ok=False, agent_name=canonical, sample_size=len(signals),
            overall_hit_rate=stats["overall"], bull_hit_rate=stats["bull"], bear_hit_rate=stats["bear"],
            reason_if_failed=f"Gemini Pro call failed: {e}",
        )

    diagnosis, suggestion = _parse_diagnosis(raw)
    return CoachReport(
        ok=True, agent_name=canonical, sample_size=len(signals),
        overall_hit_rate=stats["overall"], bull_hit_rate=stats["bull"], bear_hit_rate=stats["bear"],
        diagnosis=diagnosis or raw.strip(),
        suggestion=suggestion,
    )


def format_coach_report(report: CoachReport) -> str:
    """Telegram-friendly rendering of a CoachReport."""
    if not report.ok:
        return f"🎓 <b>COACH: {_esc(report.agent_name)}</b>\n\n⚠️ {_esc(report.reason_if_failed)}"

    bits = [f"🎓 <b>COACH: {_esc(report.agent_name)}</b>", ""]
    bits.append(f"<b>Sample:</b> {report.sample_size} resolved signals (30d)")
    if report.overall_hit_rate is not None:
        overall = f"{report.overall_hit_rate * 100:.0f}%"
        bull = f"{report.bull_hit_rate * 100:.0f}%" if report.bull_hit_rate is not None else "n/a"
        bear = f"{report.bear_hit_rate * 100:.0f}%" if report.bear_hit_rate is not None else "n/a"
        bits.append(f"<b>Hit rate:</b> {overall} overall · BULL {bull} · BEAR {bear}")
    bits.append("")
    bits.append("<b>📊 Diagnosis</b>")
    bits.append(_esc(report.diagnosis))
    if report.suggestion:
        bits.append("")
        bits.append("<b>🔧 Suggested fix</b>")
        bits.append(_esc(report.suggestion))
    return "\n".join(bits)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _call_pro_with_bounded_thinking(prompt: str, model: str, num_predict: int, temperature: float) -> str:
    """Raw Gemini Pro call with thinking_budget capped so it can't eat the
    entire max_output_tokens before producing visible text."""
    from google import genai as google_genai
    from google.genai import types as genai_types
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY missing for coach Pro call")
    client = google_genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=num_predict,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=PRO_THINKING_BUDGET),
        ),
    )
    return (resp.text or "").strip()
