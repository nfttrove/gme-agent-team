"""
GME Trading System Orchestrator — 10-agent, BackgroundScheduler edition.

Agent schedule (cost-optimized):
  Valerie  (data validator)    — every 5 minutes
  Chatty   (stream commentator) — every 5 minutes
  Newsie   (news sentiment)    — every 30 minutes
  Pattern  (multi-day pattern) — every 2 hours
  Trendy   (daily trend)       — every 4 hours + 8 PM ET
  Futurist (price predictor)   — every 2 hours
  GeoRisk  (geopolitical)      — every 1 hour
  Briefing (strategy)          — daily at 9:32 AM ET
  CTO      (structural intel)  — daily at 9:05 AM ET
  Weekly review                — Fridays at 5 PM ET
"""
import os
import sqlite3
import logging
import time
import subprocess
import sys
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from crewai import Crew, Process
from dotenv import load_dotenv

from metrics_logger import MetricsLogger
from safety_gate import run_gate_check
from market_hours import is_market_open, market_hours_required, is_active_window, active_window_required
from learner import AgentLearner
from telegram_bot import start_bot_thread
from supabase_sync import start_sync_thread
from safe_kickoff import safe_kickoff, safe_kickoff_with_fallback, CrewTimeout
from db_maintenance import enable_wal_mode
from signal_manager import SignalManager
from notifier import notify_signal_alert
from models.agent_outputs import FuturistPrediction

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
metrics = MetricsLogger()
learner = AgentLearner()


# ── Learning helpers ──────────────────────────────────────────────────────────

def recall_lessons(intent: str) -> str:
    """Surface graduated lessons relevant to `intent`, formatted for direct
    injection into a Task description. Returns "" if no lessons match.

    Was a subprocess call to .agent/tools/recall.py — that script filtered by
    a `status` field that the seeded lessons don't carry, so it always
    returned nothing. Now reads lessons.jsonl directly via learning.py.
    """
    try:
        from learning import recall_relevant_lessons
        return recall_relevant_lessons(intent)
    except Exception as e:
        log.warning(f"[recall] Failed to surface lessons: {e}")
        return ""


# ── DB helpers ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(open(os.path.join(os.path.dirname(__file__), "db_schema.sql")).read())
    conn.commit()
    conn.close()
    enable_wal_mode(DB_PATH)


def check_ollama_ready() -> bool:
    """Verify Ollama is running and has required models.

    Requires: gemma2:9b (minimum)
    Recommended: deepseek-r1:8b (for complex reasoning agents)

    Logs failure and returns False if Ollama is unreachable or missing Gemma.
    """
    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        response = requests.get(f"{ollama_host}/api/tags", timeout=2)
        models = [m["name"] for m in response.json().get("models", [])]

        if "gemma2:9b" not in models:
            log.error(f"[check_ollama_ready] REQUIRED: gemma2:9b not found. Available: {models}")
            return False

        has_deepseek = "deepseek-r1:8b" in models
        log.info(f"[check_ollama_ready] Ollama ready ({len(models)} models) - DeepSeek: {'YES' if has_deepseek else 'NO (fallback to Gemma for complex agents)'}")
        return True
    except requests.exceptions.ConnectionError:
        log.error(f"[check_ollama_ready] Ollama unreachable at {ollama_host}")
        return False
    except Exception as e:
        log.error(f"[check_ollama_ready] Error checking Ollama: {e}")
        return False


def write_log(agent: str, content: str, task_type: str, status: str = "ok"):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO agent_logs (agent_name, timestamp, task_type, content, status) VALUES (?,?,?,?,?)",
            (agent, datetime.now(ET).isoformat(), task_type, content, status),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"write_log failed: {e}")


# ── Individual cycle functions ─────────────────────────────────────────────────

@active_window_required
def run_validation():
    """Valerie — data feed health check. No LLM; pure SQL measurement.

    The old crew-based version handed the LLM placeholder values ('tick_count:
    0, latest_ts: unknown, max_gap: 999s') because validate_data_task was
    created at import time and never re-populated, so 1000+ Valerie logs are
    either timeouts or LLM reformats of those zero/unknown placeholders.

    Since this is a pure data-quality measurement (count ticks, measure gaps,
    spot outliers), there's no reason to involve an LLM at all. Compute the
    values, write a single-line summary. Sub-100ms instead of multi-second.
    """
    write_log("Valerie", "Starting validation cycle", "validation", "running")
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT timestamp, close FROM price_ticks WHERE symbol='GME' "
            "AND timestamp > datetime('now','-5 minutes') "
            "ORDER BY timestamp ASC"
        ).fetchall()
        conn.close()

        tick_count = len(rows)
        latest_ts = rows[-1][0] if rows else "none"
        max_gap_s = 0.0
        outliers = 0
        if tick_count >= 2:
            from datetime import datetime as _dt
            prev_t = None
            prev_p = None
            for ts, close in rows:
                try:
                    t = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if prev_t is not None:
                    gap = (t - prev_t).total_seconds()
                    if gap > max_gap_s:
                        max_gap_s = gap
                    if prev_p and close and abs(close - prev_p) / prev_p > 0.20:
                        outliers += 1
                prev_t, prev_p = t, float(close or 0)

        status = "ok" if tick_count > 0 and max_gap_s < 120 and outliers == 0 else "degraded"
        brief = (
            f"{status.upper()} · ticks(5m)={tick_count} · "
            f"latest={latest_ts[:19] if isinstance(latest_ts, str) else latest_ts} · "
            f"max_gap={max_gap_s:.0f}s · outliers={outliers}"
        )
        log.info(f"[Valerie] {brief}")
        write_log("Valerie", brief, "validation",
                  "ok" if status == "ok" else "degraded")
    except Exception as e:
        log.error(f"[Valerie] {e}")
        write_log("Valerie", str(e), "validation", "error")


@active_window_required
def _volume_regime(conn: sqlite3.Connection, symbol: str = "GME") -> dict:
    """Session-relative volume regime, computed deterministically.

    Ratio = today's cumulative volume / (20-day ADV pro-rated to minutes elapsed
    in the 09:30-16:00 ET session). Labels: quiet (<0.5x), normal (0.5-1.3x),
    elevated (1.3-2.0x), heavy (2.0-3.5x), spike (>=3.5x).

    Outside session hours (pre/post/weekend/holiday), compares cumulative-so-far
    against full ADV with no pro-rating. Returns label='unknown' if ADV or
    today's volume isn't available yet.
    """
    from datetime import time as dtime
    now_et    = datetime.now(ET)
    today_str = now_et.date().isoformat()

    # Today's cumulative volume — daily_candles is kept fresh by the aggregator;
    # fall back to summing raw price_ticks if not yet populated.
    row = conn.execute(
        "SELECT volume FROM daily_candles WHERE symbol=? AND date=?",
        (symbol, today_str),
    ).fetchone()
    today_vol = float(row[0]) if row and row[0] else 0.0
    if today_vol == 0:
        row = conn.execute(
            "SELECT COALESCE(SUM(volume),0) FROM price_ticks "
            "WHERE symbol=? AND date(timestamp)=?",
            (symbol, today_str),
        ).fetchone()
        today_vol = float(row[0]) if row and row[0] else 0.0

    # 20-day ADV excluding today
    rows = conn.execute(
        "SELECT volume FROM daily_candles WHERE symbol=? AND date<? "
        "ORDER BY date DESC LIMIT 20",
        (symbol, today_str),
    ).fetchall()
    vols = [float(r[0]) for r in rows if r[0]]
    adv  = sum(vols) / len(vols) if vols else 0.0

    if adv <= 0 or today_vol <= 0:
        return {"label": "unknown", "ratio": 0.0,
                "today_volume": int(today_vol), "baseline_adv": int(adv)}

    MARKET_OPEN  = dtime(9, 30)
    MARKET_CLOSE = dtime(16, 0)
    t = now_et.time().replace(tzinfo=None)
    if MARKET_OPEN <= t < MARKET_CLOSE:
        minutes_elapsed = max(1, (t.hour - 9) * 60 + (t.minute - 30))
        expected = adv * (minutes_elapsed / 390.0)
    else:
        expected = adv

    ratio = today_vol / expected if expected > 0 else 0.0
    if   ratio < 0.5: label = "quiet"
    elif ratio < 1.3: label = "normal"
    elif ratio < 2.0: label = "elevated"
    elif ratio < 3.5: label = "heavy"
    else:             label = "spike"

    return {"label": label, "ratio": round(ratio, 2),
            "today_volume": int(today_vol), "baseline_adv": int(adv)}


def run_commentary():
    """Chatty — one-shot pithy commentary via direct Ollama call.

    Bypasses CrewAI because Gemma + CrewAI's prompt templating was returning
    empty/garbled responses, and CrewAI then fell back to echoing the agent's
    backstory as str(result) — which is what was getting logged for months
    instead of actual commentary. Direct Ollama also avoids the 180s crew
    timeout since there's no orchestration layer.

    Dedup: skip send if state bucket (price-tick, vol-regime, consensus)
    hasn't materially shifted since last comment. Prevents the 6-in-a-row
    "bullish, volume mixed" paraphrase spam.
    """
    write_log("Chatty", "Composing commentary", "commentary", "running")
    try:
        from market_state import get_market_fact
        fact = get_market_fact("GME", DB_PATH)

        conn = sqlite3.connect(DB_PATH)
        synthesis = conn.execute(
            "SELECT content FROM agent_logs WHERE agent_name='Synthesis' "
            "AND status='ok' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

        synthesis_text = synthesis[0][:200] if synthesis else "No consensus yet"
        price = fact['price'] or 0.0

        regime = _volume_regime(conn, "GME")
        vol_label = regime["label"]
        vol_ratio = regime["ratio"]

        # Dedup gate — include direction so up/down flips trigger fresh comment
        price_bucket = round(float(price), 1) if price else 0.0
        consensus_bucket = (synthesis_text or "")[:60].lower()
        state_key = f"{price_bucket}|{fact['direction']}|{vol_label}|{consensus_bucket}"
        last_state = conn.execute(
            "SELECT content FROM agent_logs WHERE agent_name='Chatty' "
            "AND task_type='commentary_state' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last_state and last_state[0] == state_key:
            log.info(f"[Chatty] state unchanged ({state_key}) — skipping")
            conn.close()
            write_log("Chatty", "state unchanged; no new comment", "commentary", "skipped")
            return

        prompt = (
            "You are GME's live-stream commentator. Produce ONE punchy insight (max 120 chars) "
            "grounded in the data below. No preamble, no quotes, no markdown — just the comment. "
            "Use the volume label as given — do NOT invent your own descriptor. "
            "Your commentary MUST be consistent with the MARKET FACT direction (don't say "
            "'bullish price action' when price is FALLING).\n\n"
            f"{fact['prompt_line']}\n"
            f"Volume regime: {vol_label} ({vol_ratio:.2f}x session-pro-rated 20d ADV)\n"
            f"Team consensus: {synthesis_text}\n"
        )

        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        r = requests.post(
            f"{ollama_host}/api/generate",
            json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                  "options": {"num_predict": 80, "temperature": 0.7}},
            timeout=30,
        )
        r.raise_for_status()
        comment = r.json().get("response", "").strip().strip('"').strip("'")
        # Collapse to first line — Gemma sometimes adds a second "explanation" line
        comment = comment.split("\n")[0].strip()[:240]

        if not comment:
            write_log("Chatty", "empty LLM response", "commentary", "error")
            return

        conn.execute(
            "INSERT INTO stream_comments (timestamp, comment, displayed) VALUES (?, ?, 0)",
            (datetime.now(ET).isoformat(), comment),
        )
        conn.commit()
        conn.close()

        log.info(f"[Chatty] {comment}")
        write_log("Chatty", comment, "commentary")
        # Record the state key so the next run can detect "nothing changed"
        write_log("Chatty", state_key, "commentary_state")
    except requests.Timeout:
        log.error("[Chatty] Ollama timeout")
        write_log("Chatty", "Ollama timeout after 30s", "commentary", "timeout")
    except Exception as e:
        log.error(f"[Chatty] {e}")
        write_log("Chatty", str(e), "commentary", "error")


@market_hours_required
def run_news():
    """Newsie — fetch real headlines, score them deterministically, then ask
    Gemma for a one-line narrative.

    Bypasses CrewAI for the same reason as Chatty: Gemma can't call tools, so
    the task prompt's "Fetch the latest 10 GME news headlines using the News
    API tool" instruction caused Gemma to hallucinate placeholder headlines
    ("Headline 1 - Sentiment Analysis Needed") for months.
    """
    log.info("[Newsie] Running news sentiment cycle")
    write_log("Newsie", "Scanning news sentiment", "news", "running")
    with metrics.cycle("news"):
        try:
            from tools import NewsAPITool
            articles = NewsAPITool()._run("GME")
            articles = [a for a in articles if a.get("headline") and "error" not in a]
            if not articles:
                write_log("Newsie", "no articles returned from news sources", "news", "error")
                return

            score_map = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}
            scored = [(a, score_map.get((a.get("sentiment") or "neutral").lower(), 0.0)) for a in articles]
            composite = round(sum(s for _, s in scored) / len(scored), 3)
            label = "bullish" if composite > 0.15 else "bearish" if composite < -0.15 else "neutral"

            # Write each headline row to news_analysis
            conn = sqlite3.connect(DB_PATH)
            now_iso = datetime.now(ET).isoformat()
            for a, s in scored:
                conn.execute(
                    "INSERT INTO news_analysis (timestamp, headline, source, "
                    "sentiment_score, sentiment_label, summary) VALUES (?, ?, ?, ?, ?, ?)",
                    (now_iso, a.get("headline", "")[:300], a.get("source", "")[:80],
                     s, (a.get("sentiment") or "neutral").lower(), (a.get("summary") or "")[:500])
                )
            conn.commit()

            # Ask Gemma for a pithy narrative over the real data
            top_lines = "\n".join(
                f"- [{(a.get('sentiment') or 'neutral')[:4]}] {a.get('source','?')}: {a.get('headline','')[:120]}"
                for a, _ in scored[:8]
            )
            prompt = (
                "You are GME's news desk analyst. In ONE sentence (max 200 chars), "
                "summarise today's news narrative for the trading team. No preamble, no quotes.\n\n"
                f"Composite sentiment: {composite:+.2f} ({label}) across {len(scored)} headlines.\n"
                f"Top headlines:\n{top_lines}\n"
            )
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            narrative = ""
            try:
                r = requests.post(
                    f"{ollama_host}/api/generate",
                    json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                          "options": {"num_predict": 120, "temperature": 0.5}},
                    timeout=30,
                )
                r.raise_for_status()
                narrative = r.json().get("response", "").strip().strip('"').strip("'")
                narrative = narrative.split("\n")[0].strip()[:400]
            except Exception as e:
                log.warning(f"[Newsie] narrative LLM failed: {e}")

            summary = (
                f"composite={composite:+.2f} ({label}) · {len(scored)} articles · "
                f"{narrative or 'narrative unavailable'}"
            )
            conn.close()

            log.info(f"[Newsie] {summary}")
            write_log("Newsie", summary, "news")
        except Exception as e:
            log.error(f"[Newsie] {e}")
            write_log("Newsie", str(e), "news", "error")


def _compute_pattern_signal():
    """Deterministic pattern detection — no LLM in the decision loop.

    The old version handed raw OHLCV to Gemma and asked "what pattern is this?"
    That was theatre: Gemma copied its previous logs and produced the same
    "ascending_triangle @ $26.40 (conf=68%)" every 2 hours, unchanged.

    Now: `pattern_detector.detect_patterns()` does the math (RSI, MACD,
    Bollinger, ATR, swing highs/lows, linear regression for triangles).
    Confidence = number of independent confirming signals, capped at 0.85.
    Gemma is asked *only* to turn the structured output into a plain-English
    sentence — something it's actually good at — and the narration is
    cosmetic. If Gemma fails, we still have the honest structured verdict.
    """
    from tools import PriceDataTool
    from models.agent_outputs import PatternSignal
    from pattern_detector import detect_patterns

    candles = PriceDataTool()._run(lookback_days=30)
    if not candles or len(candles) < 15:
        return None, "insufficient candles (<15)"

    report = detect_patterns(candles)
    if report is None:
        return None, "pattern_detector returned no report (data quality)"

    # Narration: let Gemma write a plain-English sentence FROM the structured
    # output. Fails-open — if Gemma is down or slow, we use a default sentence
    # built from the cues so the signal still flows.
    #
    # NOTE: if the detector returned pattern_type="none", we skip Gemma entirely.
    # Asking it to narrate a non-pattern was producing sentences like "could
    # potentially break out above $26.40" — which contradicts the detector's
    # verdict. When there's no pattern, say exactly that.
    default_sentence = _default_pattern_sentence(report)
    sentence = default_sentence
    if report.pattern_type != "none":
        try:
            prompt = (
                "You are a chart-pattern narrator. Write ONE short plain-English "
                "sentence (max 160 chars, no markdown, no quotes) describing the "
                "verified pattern below. Cite specific numbers from the data. "
                "Do NOT invent patterns, levels, or direction — use only what's "
                "stated.\n\n"
                f"Pattern: {report.pattern_type}\n"
                f"Direction: {report.breakout_direction}\n"
                f"Breakout level: ${report.breakout_level:.2f}\n"
                f"Current price: ${report.indicators.get('price', 0):.2f}\n"
                f"Confidence: {report.confidence:.0%} ({report.severity.lower()} severity)\n"
                f"RSI14: {report.indicators.get('rsi14','n/a')}\n"
                f"MACD hist: {report.indicators.get('macd_hist','n/a')}\n"
                f"Supporting cues: {report.reasoning}\n"
            )
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            r = requests.post(
                f"{ollama_host}/api/generate",
                json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                      "options": {"num_predict": 120, "temperature": 0.2}},
                timeout=30,
            )
            r.raise_for_status()
            candidate = r.json().get("response", "").strip().strip('"').strip("'")
            candidate = candidate.split("\n")[0].strip()
            if 20 < len(candidate) < 300:
                sentence = candidate[:220]
        except Exception as e:
            log.warning(f"[Pattern] narration fallback — Gemma error: {e}")

    # Build PatternSignal from the detector output (authoritative) plus the
    # narration (cosmetic). PatternSignal validators will reject anything
    # inconsistent.
    try:
        signal = PatternSignal(
            pattern_type=report.pattern_type,
            confidence=report.confidence,
            breakout_level=report.breakout_level,
            breakout_direction=report.breakout_direction,
            reasoning=sentence[:220],
            severity=report.severity,
        )
    except Exception as e:
        return None, f"validation error: {e} | report={report.as_dict()}"

    # Build the log-line header. When pattern=none, don't print a spurious
    # "UP break @ $X" — there is no break to watch. Use the prose sentence alone.
    if signal.pattern_type == "none":
        narrative = signal.reasoning[:220]
    else:
        narrative = (
            f"{signal.pattern_type} · {signal.breakout_direction} break @ ${signal.breakout_level:.2f} "
            f"(conf={signal.confidence:.0%}) · {signal.reasoning[:220]}"
        )
    return signal, narrative


def _default_pattern_sentence(report) -> str:
    """Fallback narration if Gemma is unavailable — no invention, just cues.
    Guards against NaN/None indicators (the `ta` library emits NaN before it
    has enough bars, and the detector honestly reports that as None)."""
    import math
    ind = report.indicators or {}
    rsi = ind.get("rsi14")
    macd_hist = ind.get("macd_hist")
    price = ind.get("price", 0)

    def _finite(x):
        return x is not None and isinstance(x, (int, float)) and not math.isnan(x)

    rsi_str = f" RSI {rsi:.0f}" if _finite(rsi) else ""
    macd_str = (
        f", MACD {'+' if macd_hist >= 0 else ''}{macd_hist:.2f}"
        if _finite(macd_hist) else ""
    )
    if report.pattern_type == "none":
        return f"No clean pattern on 30d chart — price ${price:.2f}{rsi_str}{macd_str}."
    return (
        f"{report.pattern_type.replace('_',' ')} detected; watching "
        f"${report.breakout_level:.2f} for {report.breakout_direction.lower()} break"
        f"{rsi_str}{macd_str}."
    )


@market_hours_required
def run_pattern():
    """Every 2h — multi-day chart pattern analysis. CrewAI-bypassed."""
    log.info("[Pattern] Running multi-day pattern analysis")
    write_log("Pattern", "Analysing multi-day chart patterns", "pattern", "running")
    with metrics.cycle("pattern"):
        try:
            signal, narrative = _compute_pattern_signal()
            if signal is None:
                write_log("Pattern", narrative, "pattern", "error")
                return
            write_log("Pattern", narrative, "pattern")
            log.info(f"[Pattern] {narrative}")
        except requests.Timeout:
            write_log("Pattern", "Ollama timeout after 60s", "pattern", "timeout")
        except Exception as e:
            log.error(f"[Pattern] {e}")
            write_log("Pattern", str(e), "pattern", "error")


def _compute_trendy_signal():
    """Shared helper: fetch indicators + lookback, ask Gemma for a TrendySignal JSON.

    Returns (signal: TrendySignal, narrative: str) or (None, reason: str) on failure.
    Used by both run_daily_trend (reports to agent_logs) and run_trendy_signal
    (also writes to signal_alerts + Telegram).
    """
    from tools import IndicatorTool, PriceDataTool
    from models.agent_outputs import TrendySignal

    ind = IndicatorTool()._run(lookback_days=30)
    if not ind or not ind.get("price"):
        return None, "no indicator data available"
    candles = PriceDataTool()._run(lookback_days=20)
    closes = [float(c.get("close", 0) or 0) for c in candles if c.get("close")]
    highs = [float(c.get("high", 0) or 0) for c in candles if c.get("high")]
    lows = [float(c.get("low", 0) or 0) for c in candles if c.get("low")]
    if not closes or not highs or not lows:
        return None, "no lookback candles available"

    support_hint = round(min(lows), 2)
    resistance_hint = round(max(highs), 2)
    price = float(ind["price"])

    prompt = (
        "You are the Trendy agent — daily trend analyst for GME.\n"
        "Respond with ONE JSON object only (no markdown, no preamble).\n\n"
        "LIVE DATA (use these — do not invent):\n"
        f"  price={price:.2f}  vwap={ind.get('vwap',0):.2f}  "
        f"ema8={ind.get('ema8',0):.2f}  ema21={ind.get('ema21',0):.2f}  "
        f"ema50={ind.get('ema50',0):.2f}  rsi14={ind.get('rsi14',0):.1f}  "
        f"pct_from_vwap={ind.get('pct_from_vwap',0):+.2f}%\n"
        f"  above_vwap={ind.get('above_vwap')}  above_ema21={ind.get('above_ema21')}  "
        f"above_ema50={ind.get('above_ema50')}\n"
        f"  20d lookback: low={support_hint:.2f}  high={resistance_hint:.2f}  "
        f"latest_close={closes[-1]:.2f}\n\n"
        "Schema (all fields required):\n"
        '{"trend_direction": "UP"|"DOWN"|"SIDEWAYS", "confidence": <0.0-1.0>, '
        '"support_level": <float>, "resistance_level": <float>, '
        '"reasoning": "<one sentence citing specific indicator values, max 220 chars>", '
        '"severity": "HIGH"|"MEDIUM"|"LOW"}\n\n'
        "Rules: UP requires price > VWAP AND price > EMA21. DOWN requires price < VWAP AND price < EMA21. "
        "Otherwise SIDEWAYS. Confidence ≤ 0.55 if EMAs disagree or RSI in 45-55. "
        f"Support should be near {support_hint}, resistance near {resistance_hint}. severity=HIGH if confidence>=0.75."
    )

    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    r = requests.post(
        f"{ollama_host}/api/generate",
        json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
              "options": {"num_predict": 300, "temperature": 0.2}},
        timeout=60,
    )
    r.raise_for_status()
    raw = r.json().get("response", "")
    data = _extract_json(raw)
    if not data:
        return None, f"parse error: {raw[:300]}"
    try:
        signal = TrendySignal(**data)
    except Exception as e:
        return None, f"validation error: {e} | raw={raw[:300]}"

    narrative = (
        f"{signal.trend_direction} (conf={signal.confidence:.0%}) · "
        f"S=${signal.support_level:.2f} R=${signal.resistance_level:.2f} · {signal.reasoning[:220]}"
    )
    return signal, narrative


@active_window_required
def run_daily_trend():
    """Every 4h + 8 PM ET — daily trend analysis. Bypasses CrewAI (see feedback memory)."""
    log.info("[Trendy] Running daily trend analysis")
    write_log("Trendy", "Running daily trend analysis", "daily_trend", "running")
    with metrics.cycle("daily_trend"):
        try:
            signal, narrative = _compute_trendy_signal()
            if signal is None:
                write_log("Trendy", narrative, "daily_trend", "error")
                return
            write_log("Trendy", narrative, "daily_trend")
            log.info(f"[Trendy] {narrative}")
        except requests.Timeout:
            write_log("Trendy", "Ollama timeout after 60s", "daily_trend", "timeout")
        except Exception as e:
            log.error(f"[Trendy] {e}")
            write_log("Trendy", str(e), "daily_trend", "error")


@market_hours_required
@active_window_required
def run_futurist_prediction_signal():
    """Futurist — direct LLM call with pre-fetched indicators + news + synthesis.

    Bypasses CrewAI for the same reason as Chatty/Newsie: Gemma/DeepSeek can't
    call tools, so CrewAI hands them stale/placeholder context and they either
    hallucinate or time out (prior `predictions` table: 0 rows; last
    prediction_signal log: timeout on 2026-04-22).

    Flow: fetch live indicators → ask Gemma for structured JSON prediction →
    validate with FuturistPrediction → insert into `predictions` → log signal
    via SignalManager → Telegram alert (freshness-gated in notifier).
    """
    import json

    log.info("[Futurist] Starting prediction signal cycle")
    write_log("Futurist", "Running price prediction signal cycle", "prediction_signal", "running")

    with metrics.cycle("futurist_prediction"):
        try:
            from tools import IndicatorTool
            ind = IndicatorTool()._run(lookback_days=30)
            if not ind or "price" not in ind or not ind.get("price"):
                write_log("Futurist", "no indicator data available", "prediction_signal", "error")
                return
            price = float(ind["price"])

            conn = sqlite3.connect(DB_PATH)
            synthesis_row = conn.execute(
                "SELECT content FROM agent_logs WHERE agent_name='Synthesis' "
                "AND status='ok' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            news_row = conn.execute(
                "SELECT content FROM agent_logs WHERE agent_name='Newsie' "
                "AND status='ok' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            conn.close()

            synthesis_text = (synthesis_row[0][:250] if synthesis_row else "No synthesis yet")
            news_text = (news_row[0][:250] if news_row else "No news summary yet")

            prompt = (
                "You are the Futurist — GME price predictor for a 1-hour horizon.\n"
                "Respond with ONE JSON object only (no markdown, no preamble, no explanation outside the JSON).\n\n"
                f"LIVE DATA (use these — do not invent):\n"
                f"  price={price:.2f}  vwap={ind.get('vwap',0):.2f}  "
                f"ema8={ind.get('ema8',0):.2f}  ema21={ind.get('ema21',0):.2f}  "
                f"rsi14={ind.get('rsi14',0):.1f}  rsi3={ind.get('rsi3',0):.1f}  "
                f"atr14={ind.get('atr14',0):.3f}  pct_from_vwap={ind.get('pct_from_vwap',0):+.2f}%\n"
                f"  above_vwap={ind.get('above_vwap')}  above_ema21={ind.get('above_ema21')}\n"
                f"  Latest synthesis: {synthesis_text}\n"
                f"  Latest news:      {news_text}\n\n"
                "Schema (all fields required):\n"
                '{"predicted_price": <float>, "confidence": <0.0-1.0>, '
                '"horizon": "1h", "bias": "BULLISH"|"BEARISH"|"NEUTRAL"|"HOLD", '
                '"reasoning": "<one sentence, max 200 chars>", '
                '"stop_loss": <float>, "take_profit": <float>}\n\n'
                "Rules: stop_loss MUST be below predicted_price for bullish, above for bearish. "
                "Use ATR(14) to size stops (≈1.5×ATR from entry). Confidence ≤ 0.60 if signals conflict."
            )

            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            r = requests.post(
                f"{ollama_host}/api/generate",
                json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                      "options": {"num_predict": 300, "temperature": 0.3}},
                timeout=60,
            )
            r.raise_for_status()
            raw = r.json().get("response", "")
            prediction_data = _extract_json(raw)
            if not prediction_data:
                log.warning("[Futurist] Could not parse prediction from output")
                write_log("Futurist", f"Failed to parse prediction:\n{raw[:500]}", "prediction_signal", "parse_error")
                return

            try:
                prediction = FuturistPrediction(**prediction_data)
            except Exception as e:
                log.error(f"[Futurist] Pydantic validation failed: {e}")
                write_log("Futurist", f"Validation error: {e} | raw={raw[:400]}", "prediction_signal", "validation_error")
                return

            # Persist prediction for accuracy tracking
            now_iso = datetime.now(ET).isoformat()
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO predictions (timestamp, horizon, predicted_price, confidence, reasoning) "
                "VALUES (?, ?, ?, ?, ?)",
                (now_iso, prediction.horizon, prediction.predicted_price,
                 prediction.confidence, prediction.reasoning[:500]),
            )
            conn.commit()
            conn.close()

            summary = (
                f"{prediction.bias} {prediction.horizon} → ${prediction.predicted_price:.2f} "
                f"(conf={prediction.confidence:.0%}) · {prediction.reasoning[:200]}"
            )
            write_log("Futurist", summary, "prediction_signal", "ok")
            log.info(f"[Futurist] {summary}")

            # Log signal and notify (only if confidence is actionable)
            if prediction.confidence >= 0.60 and prediction.stop_loss and prediction.take_profit:
                manager = SignalManager(DB_PATH)
                alert_id = manager.log_alert(
                    agent_name="Futurist",
                    signal_type=prediction.signal_type,
                    confidence=prediction.confidence,
                    severity="HIGH" if prediction.confidence >= 0.80 else ("MEDIUM" if prediction.confidence >= 0.65 else "LOW"),
                    entry_price=prediction.predicted_price * 0.99,
                    stop_loss=prediction.stop_loss,
                    take_profit=prediction.take_profit,
                    reasoning=prediction.reasoning[:500],
                )
                try:
                    notify_signal_alert(
                        agent_name="Futurist",
                        signal_type=prediction.signal_type,
                        confidence=prediction.confidence,
                        entry_price=prediction.predicted_price * 0.99,
                        stop_loss=prediction.stop_loss,
                        take_profit=prediction.take_profit,
                        reasoning=prediction.reasoning,
                        alert_id=alert_id,
                    )
                except Exception as e:
                    log.warning(f"[Futurist] Telegram alert failed (non-critical): {e}")

            metrics.snapshot()

        except requests.Timeout:
            log.error("[Futurist] Ollama timeout")
            write_log("Futurist", "Ollama timeout after 60s", "prediction_signal", "timeout")
        except Exception as e:
            log.error(f"[Futurist] {e}")
            write_log("Futurist", str(e), "prediction_signal", "error")


def _extract_json(raw_output: str) -> dict:
    """Extract JSON from agent output (unified for all agents).

    Handles:
    - Plain JSON (Gemma output)
    - JSON after <thought> block (DeepSeek-r1 output)
    - Markdown code blocks ```json ... ```
    """
    import re
    import json

    # Try 1: Extract JSON from <thought>...</thought> block (DeepSeek-r1)
    thought_match = re.search(r'<thought>.*?</thought>\s*(.*)', raw_output, re.DOTALL)
    if thought_match:
        raw_output = thought_match.group(1)

    # Try 2: Extract from markdown code block
    code_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_output, re.DOTALL)
    if code_match:
        json_str = code_match.group(1)
    else:
        # Try 3: Extract first JSON object
        json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
        json_str = json_match.group(0) if json_match else None

    if not json_str:
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def _extract_futurist_prediction(raw_output: str) -> dict:
    """Extract FuturistPrediction JSON from agent output (legacy, uses _extract_json)."""
    return _extract_json(raw_output)


@active_window_required
def run_trendy_signal():
    """Trendy trend signal + Telegram alert (CrewAI-bypassed — see feedback memory)."""
    log.info("[Trendy] Starting trend signal cycle")
    write_log("Trendy", "Running trend signal cycle", "trend_signal", "running")

    with metrics.cycle("trendy_signal"):
        try:
            signal, narrative = _compute_trendy_signal()
            if signal is None:
                log.warning(f"[Trendy] {narrative}")
                write_log("Trendy", narrative, "trend_signal",
                          "parse_error" if "parse" in narrative else "validation_error" if "validation" in narrative else "error")
                return

            write_log("Trendy", narrative, "trend_signal", "ok")
            log.info(f"[Trendy] {narrative}")

            # For UP trend: entry near support (pullback buy), stop below, target at resistance.
            # For DOWN trend: entry near resistance (bounce short), stop above, target at support.
            # SIDEWAYS: no actionable signal.
            if signal.trend_direction == "SIDEWAYS" or signal.confidence < 0.55:
                metrics.snapshot()
                return

            if signal.trend_direction == "UP":
                entry_price = signal.support_level
                stop_loss = round(signal.support_level * 0.97, 2)
                take_profit = signal.resistance_level
            else:  # DOWN
                entry_price = signal.resistance_level
                stop_loss = round(signal.resistance_level * 1.03, 2)
                take_profit = signal.support_level

            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Trendy",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reasoning=signal.reasoning[:500],
            )
            try:
                notify_signal_alert(
                    agent_name="Trendy",
                    signal_type=signal.signal_type,
                    confidence=signal.confidence,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=f"{signal.trend_direction} trend | S=${signal.support_level:.2f}, R=${signal.resistance_level:.2f} — {signal.reasoning[:160]}",
                    alert_id=alert_id,
                )
            except Exception as e:
                log.warning(f"[Trendy] Telegram alert failed (non-critical): {e}")

            metrics.snapshot()

        except requests.Timeout:
            write_log("Trendy", "Ollama timeout after 60s", "trend_signal", "timeout")
        except Exception as e:
            log.error(f"[Trendy] {e}")
            write_log("Trendy", str(e), "trend_signal", "error")


@active_window_required
def run_synthesis_signal():
    """Synthesis agent consensus brief with signal confidence logging."""
    from agents import synthesis_agent
    from tasks import synthesis_task
    from models.agent_outputs import SynthesisBrief

    log.info("[Synthesis] Starting consensus signal cycle")
    write_log("Synthesis", "Running consensus signal cycle", "consensus_signal", "running")

    with metrics.cycle("synthesis_signal"):
        try:
            crew = Crew(
                agents=[synthesis_agent],
                tasks=[synthesis_task],
                process=Process.sequential,
                verbose=True,
            )
            result = safe_kickoff_with_fallback(crew, agent_name="Synthesis", timeout_seconds=300, label="synthesis_signal")

            signal_data = _extract_json(str(result))
            if not signal_data:
                log.warning("[Synthesis] Could not parse signal")
                return

            try:
                signal = SynthesisBrief(**signal_data)
            except Exception as e:
                log.error(f"[Synthesis] Validation failed: {e}")
                return

            write_log("Synthesis", str(result)[:1000], "consensus_signal", "ok")

            # Log signal
            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Synthesis",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity="HIGH" if signal.confidence >= 0.80 else "MEDIUM" if signal.confidence >= 0.65 else "LOW",
                entry_price=signal.price,
                stop_loss=signal.price * 0.97,
                take_profit=signal.price * 1.03,
                reasoning=f"{signal.consensus} | Strength: {signal.trend_strength:.0%}",
            )

            try:
                notify_signal_alert(
                    agent_name="Synthesis",
                    signal_type=signal.signal_type,
                    confidence=signal.confidence,
                    entry_price=signal.price,
                    stop_loss=signal.price * 0.97,
                    take_profit=signal.price * 1.03,
                    reasoning=f"Consensus: {signal.consensus} | Sentiment: {signal.news_sentiment:+.1f} | Trend: {signal.trend_direction}",
                    alert_id=alert_id,
                )
            except Exception as e:
                log.warning(f"[Synthesis] Alert failed: {e}")

            metrics.snapshot()

        except CrewTimeout as e:
            log.error(f"[Synthesis] TIMEOUT: {e}")
            write_log("Synthesis", f"TIMEOUT: {e}", "consensus_signal", "timeout")
        except Exception as e:
            log.error(f"[Synthesis] {e}")
            write_log("Synthesis", str(e), "consensus_signal", "error")


@active_window_required
def run_pattern_signal():
    """Pattern signal + Telegram alert (CrewAI-bypassed)."""
    log.info("[Pattern] Starting pattern signal cycle")
    write_log("Pattern", "Running pattern signal cycle", "pattern_signal", "running")

    with metrics.cycle("pattern_signal"):
        try:
            signal, narrative = _compute_pattern_signal()
            if signal is None:
                log.warning(f"[Pattern] {narrative}")
                write_log("Pattern", narrative, "pattern_signal",
                          "parse_error" if "parse" in narrative else "validation_error" if "validation" in narrative else "error")
                return

            write_log("Pattern", narrative, "pattern_signal", "ok")
            log.info(f"[Pattern] {narrative}")

            if signal.pattern_type == "none" or signal.confidence < 0.60:
                metrics.snapshot()
                return

            entry_price = signal.breakout_level
            if signal.breakout_direction == "UP":
                stop_loss = round(signal.breakout_level * 0.96, 2)
                take_profit = round(signal.breakout_level * 1.06, 2)
            else:
                stop_loss = round(signal.breakout_level * 1.04, 2)
                take_profit = round(signal.breakout_level * 0.94, 2)

            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Pattern",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reasoning=signal.reasoning[:500],
            )
            try:
                notify_signal_alert(
                    agent_name="Pattern",
                    signal_type=signal.signal_type,
                    confidence=signal.confidence,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=f"{signal.pattern_type} | {signal.breakout_direction} break @ ${signal.breakout_level:.2f} — {signal.reasoning[:160]}",
                    alert_id=alert_id,
                )
            except Exception as e:
                log.warning(f"[Pattern] Alert failed: {e}")

            metrics.snapshot()

        except requests.Timeout:
            write_log("Pattern", "Ollama timeout after 60s", "pattern_signal", "timeout")
        except Exception as e:
            log.error(f"[Pattern] {e}")
            write_log("Pattern", str(e), "pattern_signal", "error")


@active_window_required
def run_newsie_signal():
    """Newsie agent sentiment signal with confidence logging."""
    from agents import news_analyst_agent
    from tasks import news_task
    from models.agent_outputs import NewsSignal

    log.info("[Newsie] Starting sentiment signal cycle")
    write_log("Newsie", "Running sentiment signal cycle", "sentiment_signal", "running")

    with metrics.cycle("newsie_signal"):
        try:
            crew = Crew(
                agents=[news_analyst_agent],
                tasks=[news_task],
                process=Process.sequential,
                verbose=True,
            )
            result = safe_kickoff_with_fallback(crew, agent_name="Newsie", timeout_seconds=300, label="newsie_signal")

            signal_data = _extract_json(str(result))
            if not signal_data:
                log.warning("[Newsie] Could not parse signal")
                return

            try:
                signal = NewsSignal(**signal_data)
            except Exception as e:
                log.error(f"[Newsie] Validation failed: {e}")
                return

            write_log("Newsie", str(result)[:1000], "sentiment_signal", "ok")

            # Log signal
            manager = SignalManager(DB_PATH)
            confidence = abs(signal.sentiment_score)  # Higher |sentiment| = more confident
            alert_id = manager.log_alert(
                agent_name="Newsie",
                signal_type=signal.signal_type,
                confidence=confidence,
                severity="HIGH" if confidence >= 0.80 else "MEDIUM" if confidence >= 0.65 else "LOW",
                entry_price=None,  # Sentiment doesn't have price target
                stop_loss=None,
                take_profit=None,
                reasoning=signal.headline,
            )

            try:
                notify_signal_alert(
                    agent_name="Newsie",
                    signal_type=signal.signal_type,
                    confidence=confidence,
                    entry_price=None,
                    stop_loss=None,
                    take_profit=None,
                    reasoning=f"Sentiment: {signal.sentiment_label} ({signal.sentiment_score:+.1f}) | {signal.headline[:100]}",
                    alert_id=alert_id,
                )
            except Exception as e:
                log.warning(f"[Newsie] Alert failed: {e}")

            metrics.snapshot()

        except CrewTimeout as e:
            log.error(f"[Newsie] TIMEOUT: {e}")
            write_log("Newsie", f"TIMEOUT: {e}", "sentiment_signal", "timeout")
        except Exception as e:
            log.error(f"[Newsie] {e}")
            write_log("Newsie", str(e), "sentiment_signal", "error")


def run_futurist_cycle():
    """Full strategic cycle: gate check → Futurist → Boss → emit signal to team."""
    log.info("[Futurist] Starting full strategic cycle")
    write_log("Futurist", "Starting strategic cycle — gate check", "full_cycle", "running")

    # Recall relevant lessons and INJECT them into the futurist_task prompt.
    # Previously this was logged-and-discarded — agents never saw the lessons.
    # We mutate the imported task's description in place; original is captured
    # on first cycle (_orig_description) so re-injection doesn't accumulate.
    lessons = recall_lessons("GME trading strategy, market conditions, IV management, risk rules")
    try:
        from tasks import futurist_task as _ft
        if not hasattr(_ft, "_orig_description"):
            _ft._orig_description = _ft.description
        _ft.description = (f"{lessons}\n\n{_ft._orig_description}"
                           if lessons else _ft._orig_description)
        if lessons:
            log.info(f"[Futurist] Injected lessons into task ({len(lessons)} chars)")
            write_log("Futurist", f"Recalled & injected lessons:\n{lessons[:500]}", "recall")
        else:
            log.info("[Futurist] No matching lessons for this cycle")
    except Exception as e:
        log.warning(f"[Futurist] lesson injection failed (non-fatal): {e}")

    gate = run_gate_check()
    log.info(gate.report())

    if not gate.allowed:
        log.info("[gate] No signal — skipping strategic cycle.")
        write_log("Futurist", f"Gate blocked: {gate.blocker}", "gate_check", "ok")
        write_log("SafetyGate", gate.report(), "gate_check", "blocked")
        return

    log.info(f"[gate] Signal={gate.signal} Bias={gate.bias} — launching agents.")

    with metrics.cycle("futurist_cycle"):
        try:
            from agents import (
                daily_trend_agent, multiday_trend_agent, news_analyst_agent,
                futurist_agent, project_manager_agent,
            )
            from tasks import (
                daily_trend_task, multiday_trend_task, news_task,
                futurist_task, manager_task,
            )

            crew = Crew(
                agents=[daily_trend_agent, multiday_trend_agent, news_analyst_agent,
                        futurist_agent, project_manager_agent],
                tasks=[daily_trend_task, multiday_trend_task, news_task,
                       futurist_task, manager_task],
                process=Process.sequential,
                verbose=True,
            )
            result = safe_kickoff_with_fallback(crew, agent_name="Futurist", timeout_seconds=600, label="futurist_full_cycle")
            write_log("Futurist", str(result)[:1000], "full_cycle")
            metrics.snapshot()
        except CrewTimeout as e:
            log.error(f"[Futurist] TIMEOUT: {e}")
            write_log("Futurist", f"TIMEOUT: {e}", "full_cycle", "timeout")
        except Exception as e:
            log.error(f"[Futurist] {e}")
            write_log("Futurist", str(e), "full_cycle", "error")


def run_daily_aggregation():
    try:
        import daily_aggregator
        daily_aggregator.aggregate_day()
    except Exception as e:
        log.error(f"[Aggregator] {e}")


@active_window_required
def run_intraday_aggregation():
    """Re-aggregate today's ticks into daily_candles so mid-day readers
    (Trendy, Pattern, Futurist) see current-day data instead of yesterday's."""
    try:
        import daily_aggregator
        daily_aggregator.aggregate_day()
    except Exception as e:
        log.error(f"[Aggregator-intraday] {e}")


@active_window_required
def run_voice_forwarder():
    """Forward new per-agent narrative outputs to Telegram in each agent's voice."""
    try:
        import agent_voice
        sent = agent_voice.forward_pending()
        total = sum(sent.values())
        if total:
            log.info(f"[Voice] forwarded {sent}")
    except Exception as e:
        log.error(f"[Voice] {e}")


def run_sunday_support_message():
    """Sunday 10:00 AM ET — friendly weekly support/coffee nudge."""
    try:
        from notifier import notify
        paypal_url = "https://www.paypal.com/paypalme/2r0v3"
        notify(
            "☕ <b>Weekly check-in</b>\n\n"
            "If the signals, briefs, and voices have earned their keep this week, "
            "a coffee helps keep the team running:\n"
            f"👉 <a href=\"{paypal_url}\">{paypal_url}</a>\n\n"
            "<i>Send /supportme anytime. Happy Sunday.</i>"
        )
        log.info("[Support] Sunday support message sent")
    except Exception as e:
        log.error(f"[Support] Failed: {e}")


def run_learning_debrief():
    """4:30 PM ET — score predictions vs actuals and compute agent metrics."""
    log.info("[Learner] === Post-market debrief ===")
    try:
        learner.post_market_debrief()
    except Exception as e:
        log.error(f"[Learner] Debrief failed: {e}")
        write_log("Learner", str(e), "daily_debrief", "error")


def run_lesson_producer():
    """4:35 PM ET — mine signal_scores for lesson candidates. Auto-graduates
    strong patterns into lessons.jsonl, stages weaker ones for /candidates
    review. Runs 5 min after the debrief so today's scores have settled."""
    log.info("[LessonProducer] === Daily lesson generation ===")
    try:
        from lesson_producer import produce_lessons
        summary = produce_lessons()
        msg = (f"generated={summary['candidates_generated']} "
               f"auto_graduated={summary['auto_graduated']} "
               f"staged={summary['staged']} "
               f"total_lessons={summary['total_lessons']} "
               f"total_staged={summary['total_staged']}")
        log.info(f"[LessonProducer] {msg}")
        write_log("LessonProducer", msg, "lesson_production")
    except Exception as e:
        log.error(f"[LessonProducer] Failed: {e}")
        write_log("LessonProducer", str(e), "lesson_production", "error")


def run_weekly_review():
    """Fridays 5:00 PM ET — Boss reviews trailing performance and adapts strategy."""
    log.info("[Learner] === Weekly strategy review ===")
    try:
        learner.weekly_strategy_review()
    except Exception as e:
        log.error(f"[Learner] Weekly review failed: {e}")
        write_log("Learner", str(e), "weekly_review", "error")


def run_options_update():
    """Monday 8:30 AM ET — fetch options chain and compute max pain for the week."""
    log.info("[Options] Computing weekly max pain...")
    try:
        from options_feed import OptionsFeed, ensure_options_table
        ensure_options_table()
        feed = OptionsFeed()
        feed.update_db(send_telegram=True)
    except Exception as e:
        log.error(f"[Options] Max pain update failed: {e}")
        write_log("Options", str(e), "max_pain", "error")


@market_hours_required
def run_social_scan():
    """Every 15 min during market hours — scan Twitter/X for key account posts."""
    write_log("Social", "Scanning tracked accounts", "social", "running")
    try:
        from twitter_monitor import TwitterMonitor
        monitor = TwitterMonitor()
        results = monitor.scan_all()
        if results:
            log.info(f"[Social] {len(results)} new posts found")
            for r in results:
                write_log("Social", f"@{r['username']} [{r['signal_type']}]: {r['text'][:200]}", "social")
        else:
            from twitter_monitor import TRACKED_ACCOUNTS
            write_log("Social", f"Scanned {len(TRACKED_ACCOUNTS)} accounts — no new posts", "social")
    except Exception as e:
        log.error(f"[Social] Scan failed: {e}")
        write_log("Social", str(e)[:300], "social", "error")


def run_cto_trove_score():
    """CTO — Trove deep-value score for GME with delta vs previous run.

    Writes a formatted score card to agent_logs (task_type='trove_score', agent='CTO')
    which the voice forwarder picks up for Telegram. LLM is used only for the
    one-paragraph interpretation at the end — the numerical scoring is
    deterministic (trove.score()), so it can't hallucinate points.
    """
    log.info("[CTO] Running Trove score")
    write_log("CTO", "Computing Trove deep-value score for GME", "trove_score", "running")
    try:
        from trove import fetch, score as trove_score_fn

        inp = fetch("GME")
        if inp is None:
            write_log("CTO", "trove.fetch returned None (data source down?)", "trove_score", "error")
            return
        r = trove_score_fn(inp)
        total = r["total"]
        rating = r["rating"]
        A = r["pillars"]["A"]
        B = r["pillars"]["B"]
        C = r["pillars"]["C"]
        imm = r["immunity"]
        imm_count = r["immunity_count"]
        net_cash_pct = (inp.cash_mm - inp.total_debt_mm) / inp.market_cap_mm if inp.market_cap_mm else 0

        # Look up previous run for delta
        conn = sqlite3.connect(DB_PATH)
        prev_row = conn.execute(
            "SELECT content FROM agent_logs WHERE agent_name='CTO' "
            "AND task_type='trove_score' AND status='ok' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()

        prev_total = None
        if prev_row and prev_row[0]:
            import re
            m = re.search(r"Score:\s*([\d.]+)/100", prev_row[0])
            if m:
                try:
                    prev_total = float(m.group(1))
                except ValueError:
                    pass

        if prev_total is None:
            delta_str = "(first score)"
        else:
            diff = total - prev_total
            if diff > 0.05:
                delta_str = f"↑ {diff:+.1f} vs prior {prev_total:.1f}"
            elif diff < -0.05:
                delta_str = f"↓ {diff:+.1f} vs prior {prev_total:.1f}"
            else:
                delta_str = f"= unchanged (prior {prev_total:.1f})"

        # Rating-tier hint so the LLM uses the right adjective
        if total >= 80:
            tier_hint = "exceptional deep value — top of the rubric"
        elif total >= 65:
            tier_hint = "strong deep value — well above the investment-grade cutoff"
        elif total >= 50:
            tier_hint = "investment-grade deep value — solid, not weak"
        elif total >= 35:
            tier_hint = "speculative — some pillars working, others not"
        else:
            tier_hint = "below the bar — avoid"

        # Ask Gemma for the interpretation paragraph only — numbers are locked above.
        # Primed with the GME turnaround thesis so it doesn't default to generic
        # "stretched valuation" framing that contradicts the actual setup.
        prompt = (
            "You are the CTO — a deep-value analyst covering GME. Write ONE paragraph "
            "(max 350 chars, no preamble, no markdown, no quotes) interpreting today's "
            "Trove score through the turnaround lens below.\n\n"
            "SCORING RUBRIC (so you use the right language):\n"
            "  ≥80 exceptional · ≥65 strong · ≥50 investment-grade deep value · "
            "≥35 speculative · <35 avoid. Do NOT call a 50+ score 'low' or 'weak'.\n\n"
            "GME CONTEXT (use this framing, don't contradict it):\n"
            "  • Pre-2021: private equity overleveraged the company and stripped it, "
            "Blockbuster/Toys-R-Us playbook. Weak historical P&L and depressed sales "
            "are LEGACY PE DAMAGE being worked off — not current mismanagement.\n"
            "  • Ryan Cohen took the board in 2021, cleared house, executed a turnaround.\n"
            "  • 4-year swing: ~$400M loss → ~$400M profit (~$800M). Raised ~$9B, "
            "now debt-light with a large cash pile.\n"
            "  • Heavily shorted; thesis analogy is early Tesla.\n"
            "  • If Valuation pillar is weak but Capital Structure + Quality are strong, "
            "that IS the thesis shape — call it out, don't flag it as a contradiction.\n\n"
            f"TODAY'S SCORE — {tier_hint}:\n"
            f"GME | Score {total:.1f}/100 ({rating}) | Immunity {imm_count}/5\n"
            f"Pillars — Valuation: {A:.1f}/30 · Capital: {B:.1f}/45 · Quality: {C:.1f}/25\n"
            f"Inputs: EV/FCF {inp.ev_fcf:.1f} · EV/EBITDA {inp.ev_ebitda:.1f} · P/B {inp.pb:.2f} · "
            f"Altman Z {inp.altman_z:.1f} · D/E {inp.debt_equity:.2f} · "
            f"Net Cash {net_cash_pct*100:.1f}% of MCap · Op Margin {inp.operating_margin*100:.1f}% · "
            f"ROE {inp.roe*100:.1f}% · Net Margin {inp.net_margin*100:.1f}%"
        )
        interpretation = ""
        try:
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            resp = requests.post(
                f"{ollama_host}/api/generate",
                json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                      "options": {"num_predict": 200, "temperature": 0.4}},
                timeout=30,
            )
            resp.raise_for_status()
            interpretation = resp.json().get("response", "").strip().strip('"').strip("'")
            interpretation = " ".join(interpretation.split())[:600]
        except Exception as e:
            log.warning(f"[CTO] Trove interpretation LLM failed: {e}")

        imm_line = (
            f"{'✓' if imm['debt_free'] else '✗'} Debt-free · "
            f"{'✓' if imm['cash_over_1b'] else '✗'} Cash>$1B · "
            f"{'✓' if imm['net_cash_positive'] else '✗'} Net Cash+ · "
            f"{'✓' if imm['profitable'] else '✗'} Profitable · "
            f"{'✓' if imm['altman_safe'] else '✗'} Altman Safe"
        )
        brief = (
            f"GME Trove Score: {total:.1f}/100 {rating} {delta_str}\n"
            f"Pillars — Valuation {A:.1f}/30 · Capital {B:.1f}/45 · Quality {C:.1f}/25\n"
            f"Immunity {imm_count}/5: {imm_line}\n"
            f"Inputs — EV/FCF {inp.ev_fcf:.1f} · EV/EBITDA {inp.ev_ebitda:.1f} · P/B {inp.pb:.2f} · "
            f"Altman Z {inp.altman_z:.1f} · D/E {inp.debt_equity:.2f} · Net Cash {net_cash_pct*100:.1f}% · "
            f"OpMgn {inp.operating_margin*100:.1f}% · ROE {inp.roe*100:.1f}% · NetMgn {inp.net_margin*100:.1f}%"
        )
        if interpretation:
            brief += f"\n— {interpretation}"

        log.info(f"[CTO] Trove: {total:.1f}/100 {delta_str}")
        write_log("CTO", brief, "trove_score", "ok")
    except Exception as e:
        log.error(f"[CTO] Trove score failed: {e}")
        write_log("CTO", str(e), "trove_score", "error")


def run_cto_daily_brief():
    """9:05 AM ET — CTO structural intelligence brief, just after morning huddle.

    Bypass pattern: pulls structural_signals, investor_intel, and news_analysis
    rows deterministically. Gemma only writes a short commentary on the
    short-watchlist and anti-pattern flags. GME immunity math is handled
    separately by run_cto_trove_score at 9:10.
    """
    log.info("[CTO] === Daily structural intelligence brief ===")
    write_log("CTO", "Running daily structural brief", "structural_brief", "running")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Top non-GME short candidates from structural_signals (last 30d)
        shorts = conn.execute(
            "SELECT ticker, signal_name, confidence, action, timeline_months, headline "
            "FROM structural_signals WHERE ticker != 'GME' "
            "AND filing_date >= date('now', '-30 days') "
            "ORDER BY confidence DESC LIMIT 5"
        ).fetchall()

        # Most recent investor intelligence snapshot
        inv_row = conn.execute(
            "SELECT content FROM agent_logs WHERE task_type='investor_intel' "
            "AND status='ok' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        inv_line = inv_row["content"][:240] if inv_row else "No investor intel logged."

        # Recent news sentiment distribution (last 24h)
        news_rows = conn.execute(
            "SELECT headline, sentiment_score FROM news_analysis "
            "WHERE datetime(timestamp) > datetime('now', '-1 day') "
            "ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        conn.close()

        if news_rows:
            avg_sent = sum(r["sentiment_score"] or 0 for r in news_rows) / len(news_rows)
            if avg_sent > 0.15:
                news_bias = f"bullish ({avg_sent:+.2f})"
            elif avg_sent < -0.15:
                news_bias = f"bearish ({avg_sent:+.2f})"
            else:
                news_bias = f"neutral ({avg_sent:+.2f})"
        else:
            news_bias = "no headlines in last 24h"

        # Format short watchlist
        if shorts:
            short_lines = [
                f"  {i+1}. {s['ticker']} — {s['signal_name']} "
                f"(conf {int((s['confidence'] or 0)*100)}%, {s['action'] or 'MONITOR'}, "
                f"{s['timeline_months'] or '?'}mo)"
                for i, s in enumerate(shorts[:3])
            ]
            shorts_block = "\n".join(short_lines)
        else:
            shorts_block = "  (no structural signals in last 30d)"

        # Gemma commentary — short paragraph on watchlist + anti-patterns
        prompt = (
            "You are the CTO — structural-intelligence analyst for a GME trading team. "
            "In ONE short paragraph (max 280 chars, plain English, no preamble, no markdown, "
            "no quotes), comment on today's structural picture. Call out the top short "
            "candidate if any, and flag anti-pattern risk if news bias conflicts with fundamentals.\n\n"
            "FACTS (do not contradict):\n"
            f"- Short watchlist (top 3 by confidence):\n{shorts_block}\n"
            f"- Investor intel: {inv_line}\n"
            f"- GME news bias last 24h: {news_bias}\n"
        )
        commentary = "No new structural signals to act on today."
        try:
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            resp = requests.post(
                f"{ollama_host}/api/generate",
                json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                      "options": {"num_predict": 160, "temperature": 0.3}},
                timeout=30,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip().strip('"').strip("'")
            raw = " ".join(raw.split())
            if raw:
                commentary = raw[:500]
        except Exception as e:
            log.warning(f"[CTO] Brief LLM failed ({e}), using fallback")

        brief = (
            "🛡️ CTO STRUCTURAL INTEL BRIEF\n"
            f"Short watchlist:\n{shorts_block}\n"
            f"Investor intel: {inv_line}\n"
            f"News bias (24h): {news_bias}\n"
            f"— {commentary}"
        )
        write_log("CTO", brief[:2000], "structural_brief")
        log.info(f"[CTO] Brief complete")
    except Exception as e:
        log.error(f"[CTO] Brief failed: {e}")
        write_log("CTO", str(e), "structural_brief", "error")


def run_investor_intel_scan():
    """Daily 8:00 AM ET — Fetch latest Scion 13F and check RC Ventures for new filings."""
    log.info("[Investor] === Key Investor Intelligence Scan ===")
    write_log("CTO", "Running investor intelligence scan", "investor_intel", "running")
    try:
        from sec_scanner import SECScanner
        scanner = SECScanner()
        report = scanner.key_investor_intelligence_report()
        rc_alert = report.get("rc_ventures", {}).get("alert", "")
        scion = report.get("scion", {})
        holdings = scion.get("holdings") or [{}]
        top = holdings[0]
        summary = (
            f"RC Ventures: {rc_alert} | "
            f"Scion {scion.get('filing_date','?')}: "
            f"{top.get('name','?')} {top.get('pct_portfolio','?')}% (${top.get('value_usd',0)/1e6:.0f}M)"
        )
        write_log("CTO", summary, "investor_intel")
        log.info(f"[Investor] {summary}")
        if report.get("rc_ventures", {}).get("recent_filings"):
            from notifier import notify
            notify(f"⚠️ <b>RC VENTURES NEW FILING</b>\n{rc_alert}\nCheck SEC EDGAR immediately.")
    except Exception as e:
        log.error(f"[Investor] Intel scan failed: {e}")
        write_log("CTO", str(e), "investor_intel", "error")


def run_cto_structural_scan():
    """Sundays 8:00 AM ET — Weekly deep structural scan and short watchlist update."""
    from agents import cto_agent
    from tasks import cto_structural_scan_task
    log.info("[CTO] === Weekly structural scan ===")
    try:
        # Run live EDGAR scan before the agent brief
        from sec_scanner import SECScanner
        scanner = SECScanner()
        scanner.scan_watchlist(days_back=7)
        log.info("[CTO] EDGAR scan complete — launching CTO analysis agent")

        crew = Crew(agents=[cto_agent], tasks=[cto_structural_scan_task],
                    process=Process.sequential, verbose=True)
        result = crew.kickoff()
        write_log("CTO", str(result)[:2000], "structural_scan")
    except Exception as e:
        log.error(f"[CTO] Structural scan failed: {e}")
        write_log("CTO", str(e), "structural_scan", "error")


@active_window_required
def run_synthesis():
    """Every 5 min — cross-agent intelligence synthesis so all agents share a common picture.

    Bypasses CrewAI (Gemma tool-calling issue — see feedback memory). The old
    `synthesis_task` was created at import time with placeholder strings
    ('unknown', 'No agent logs available') that never got populated, so
    Synthesis was literally logging its own prompt as output for months.

    Now: fetch the actual recent agent logs + live price + indicators, hand
    them to Gemma directly, ask for a one-line structured consensus brief.
    """
    write_log("Synthesis", "Composing consensus brief", "synthesis", "running")
    try:
        from market_state import get_market_fact
        fact = get_market_fact("GME", DB_PATH)
        if fact['price'] is None:
            write_log("Synthesis", "no price data available", "synthesis", "error")
            return

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Pull most-recent ok log per agent so we get *the current view*, not
        # a scroll of stale repeats.
        agents_of_interest = ("Valerie", "Newsie", "Pattern", "Trendy", "Futurist",
                              "CTO", "GeoRisk", "SafetyGate")
        per_agent: dict[str, str] = {}
        for name in agents_of_interest:
            row = conn.execute(
                "SELECT content FROM agent_logs WHERE agent_name=? AND status='ok' "
                "AND timestamp > datetime('now','-4 hours') "
                "ORDER BY timestamp DESC LIMIT 1",
                (name,),
            ).fetchone()
            if row and row[0]:
                per_agent[name] = row[0][:250].replace("\n", " ").strip()
        conn.close()

        if not per_agent:
            write_log("Synthesis", "no recent agent logs in last 4h", "synthesis", "error")
            return

        logs_block = "\n".join(f"  {name}: {content}" for name, content in per_agent.items())
        # PRICE token is pre-formatted from market fact — LLM must use it verbatim
        price_token = f"${fact['price']:.2f} {fact['direction'].lower()}"
        prompt = (
            "You are the team's Synthesis agent. Produce ONE line summarising the current consensus, "
            "in this EXACT format (keep labels, replace bracketed values):\n"
            f"PRICE: {price_token} | DATA: [clean/degraded] | NEWS: [bullish/bearish/neutral, score] | "
            "TREND: [up/down/sideways] | PREDICTION: [bias, confidence%] | "
            "STRUCTURAL: [GREEN/YELLOW/RED] | CONSENSUS: [BULLISH/BEARISH/NEUTRAL] [XX]%\n\n"
            "Rules: use ONLY the data below, do not invent. If an agent is missing, write 'n/a'. "
            f"Use the PRICE token EXACTLY as given: '{price_token}' — do NOT change the direction. "
            "Consensus = majority of the non-n/a directional signals. No preamble, no markdown, no quotes.\n\n"
            f"{fact['prompt_line']}\n"
            f"Recent per-agent outputs (last 4h):\n{logs_block}\n"
        )

        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        r = requests.post(
            f"{ollama_host}/api/generate",
            json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                  "options": {"num_predict": 160, "temperature": 0.2}},
            timeout=45,
        )
        r.raise_for_status()
        brief = r.json().get("response", "").strip().strip('"').strip("'")
        brief = brief.split("\n")[0].strip()[:500]

        if not brief or "PRICE" not in brief.upper():
            write_log("Synthesis", f"malformed brief: {brief[:200]}", "synthesis", "error")
            return

        # Safety net: force correct PRICE token even if LLM drifted
        import re
        brief = re.sub(
            r'PRICE:\s*\$[\d.]+\s*\w*',
            f'PRICE: {price_token}',
            brief,
            count=1,
            flags=re.IGNORECASE,
        )

        log.info(f"[Synthesis] {brief}")
        write_log("Synthesis", brief, "synthesis")
    except requests.Timeout:
        log.error("[Synthesis] Ollama timeout")
        write_log("Synthesis", "Ollama timeout after 45s", "synthesis", "timeout")
    except Exception as e:
        log.error(f"[Synthesis] {e}")
        write_log("Synthesis", str(e), "synthesis", "error")


_GEORISK_KEYWORDS = (
    "tariff", "tariffs", "sanction", "sanctions", "shipping", "port",
    "strike", "strikes", "disruption", "war", "conflict", "attack",
    "cable", "pipeline", "outage", "blackout", "suez", "panama", "red sea",
    "baltic", "taiwan", "iran", "russia", "ukraine", "china", "hurricane",
    "typhoon", "flood", "blockade", "embargo", "chip shortage",
)


def _compute_georisk():
    """Fetch recent news, filter for geopolitical/supply-chain headlines, ask
    Gemma for a risk rating. Returns (level, narrative). CrewAI-bypass.

    Honest fallback: if no keyword-matched headlines, emit
    'LOW - No geopolitical signals detected in recent news' rather than
    hallucinating 'Baltic stable, Suez open' from the task template.
    """
    from tools import NewsAPITool
    articles = NewsAPITool()._run("GME")
    articles = [a for a in articles if a.get("headline") and "error" not in a]

    def is_geo(text: str) -> bool:
        t = (text or "").lower()
        return any(kw in t for kw in _GEORISK_KEYWORDS)

    geo_hits = [
        a for a in articles
        if is_geo(a.get("headline", "")) or is_geo(a.get("summary", ""))
    ]
    if not geo_hits:
        return "LOW", (
            f"LOW - Scanned {len(articles)} recent headlines, 0 tagged geopolitical/supply-chain. "
            f"No disruption signals."
        )

    lines = "\n".join(
        f"  - [{(a.get('sentiment') or 'neutral')[:4]}] {a.get('source','?')}: "
        f"{a.get('headline','')[:140]}"
        for a in geo_hits[:8]
    )
    prompt = (
        "You are the GeoRisk agent — geopolitical + supply-chain risk monitor for GME.\n"
        "Rate current risk based ONLY on the headlines below. One line, format:\n"
        "  '[LEVEL] - [short assessment citing specific events, max 240 chars]'\n"
        "LEVEL is LOW / MEDIUM / HIGH. No preamble, no markdown, no quotes.\n\n"
        "Thresholds: LOW = indirect/minor relevance; MEDIUM = active disruption with "
        "plausible retail impact; HIGH = immediate supply-chain or consumer-demand shock.\n\n"
        f"RECENT GEO-TAGGED HEADLINES ({len(geo_hits)} hits):\n{lines}\n"
    )
    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    r = requests.post(
        f"{ollama_host}/api/generate",
        json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
              "options": {"num_predict": 120, "temperature": 0.3}},
        timeout=45,
    )
    r.raise_for_status()
    brief = r.json().get("response", "").strip().strip('"').strip("'")
    brief = brief.split("\n")[0].strip()[:400]
    level = "LOW"
    for candidate in ("HIGH", "MEDIUM", "LOW"):
        if brief.upper().startswith(candidate):
            level = candidate
            break
    if not brief:
        brief = f"{level} - GeoRisk LLM returned empty despite {len(geo_hits)} geo headlines."
    return level, brief


@active_window_required
def run_georisk():
    """Hourly GeoRisk scan — news-keyword-filtered geopolitical + supply-chain risk."""
    write_log("GeoRisk", "Scanning geopolitical signals", "georisk", "running")
    try:
        level, brief = _compute_georisk()
        log.info(f"[GeoRisk] {brief}")
        write_log("GeoRisk", brief, "georisk")
        return level, brief
    except requests.Timeout:
        write_log("GeoRisk", "Ollama timeout after 45s", "georisk", "timeout")
    except Exception as e:
        log.error(f"[GeoRisk] {e}")
        write_log("GeoRisk", str(e), "georisk", "error")


def run_calibration():
    """Score due predictions against actual price at (made_at + horizon).

    Not active-window-gated — we want to score EOD and overnight predictions
    as soon as their target time passes, regardless of when that falls.
    """
    try:
        from calibration import run_calibration_cycle
        summary = run_calibration_cycle(DB_PATH)
        if summary.get("scored") or summary.get("abandoned"):
            write_log(
                "Calibrator",
                f"scored={summary['scored']} "
                f"abandoned={summary['abandoned']} "
                f"metrics_written={summary.get('metrics_rows_written', 0)}",
                "calibration",
                "ok",
            )
    except Exception as e:
        log.error(f"[calibration] {e}")
        write_log("Calibrator", str(e), "calibration", "error")


@active_window_required
def run_periodic_brief():
    """Every 4 hours — send human-readable intelligence digest to Telegram."""
    import sqlite3
    from notifier import notify_periodic_brief

    try:
        from market_state import get_market_fact
        fact = get_market_fact("GME", DB_PATH)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        price = fact['price'] or 0
        pct_change = fact['pct_change']

        # Get latest synthesis for consensus
        synth = conn.execute(
            "SELECT content FROM agent_logs WHERE task_type='synthesis' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        consensus = synth['content'][:80] if synth else "No consensus yet"

        # Get latest prediction
        pred = conn.execute(
            "SELECT predicted_price, confidence FROM predictions ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        prediction = f"${pred['predicted_price']:.2f} ({pred['confidence']:.0%})" if pred else "No prediction"

        # Get latest signal
        signal = conn.execute(
            "SELECT signal_name, confidence FROM structural_signals ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        top_signal = f"{signal['signal_name']} ({signal['confidence']:.0%})" if signal else "No signals"

        # Get latest georisk
        georisk = conn.execute(
            "SELECT content FROM agent_logs WHERE task_type='georisk' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        geo_risk = georisk['content'][:80] if georisk else "No geopolitical alerts"

        conn.close()

        notify_periodic_brief(
            price=price,
            pct_change=pct_change,
            consensus=consensus,
            top_signal=top_signal,
            geo_risk=geo_risk,
            prediction=prediction
        )
        write_log("Briefer", "4-hour digest sent", "periodic_brief")
        log.info("[Briefer] 4-hour digest sent to Telegram")
    except Exception as e:
        log.error(f"[Briefer] {e}")
        write_log("Briefer", str(e), "periodic_brief", "error")


def run_daily_briefing():
    """10:00 AM ET — ELI5 strategy briefing sent to Telegram after market opens.

    Bypass pattern: numbers and direction are computed deterministically from
    daily_candles + signal_alerts. Gemma only fills the narrative fragments
    (pattern description, waiting-for, risk). Prevents the 'sideways on an
    up day' hallucination the previous CrewAI version produced.
    """
    from datetime import datetime
    log.info("[Briefing] === Daily Strategy Brief ===")
    write_log("Briefing", "Composing daily strategy brief", "daily_brief", "running")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # ---------- 1. Price facts (deterministic) ----------
        tick = conn.execute(
            "SELECT close, timestamp FROM price_ticks WHERE symbol='GME' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        candles = conn.execute(
            "SELECT date, open, high, low, close FROM daily_candles "
            "WHERE symbol='GME' ORDER BY date DESC LIMIT 2"
        ).fetchall()

        if not tick or not candles:
            write_log("Briefing", "No price data", "daily_brief", "error")
            conn.close()
            return

        current = float(tick["close"])
        today = candles[0]
        prev_close = float(candles[1]["close"]) if len(candles) > 1 else float(today["open"])
        day_open = float(today["open"])
        day_high = max(float(today["high"]), current)
        day_low  = min(float(today["low"]),  current)

        pct_vs_open = ((current - day_open) / day_open) * 100 if day_open else 0
        pct_vs_prev = ((current - prev_close) / prev_close) * 100 if prev_close else 0

        # Direction thresholded at ±0.5% (matches common intraday "flat" band)
        if pct_vs_prev > 0.5:
            direction = "rising"
        elif pct_vs_prev < -0.5:
            direction = "falling"
        else:
            direction = "sideways"

        # ---------- 2. Latest agent signals (from signal_alerts, not vague logs) ----------
        def latest_signal(agent: str) -> dict | None:
            row = conn.execute(
                "SELECT signal_type, confidence, entry_price, stop_loss, take_profit, "
                "reasoning, timestamp FROM signal_alerts WHERE agent_name=? "
                "AND datetime(timestamp) > datetime('now', '-1 day') "
                "ORDER BY timestamp DESC LIMIT 1",
                (agent,)
            ).fetchone()
            return dict(row) if row else None

        trendy   = latest_signal("Trendy")
        pattern  = latest_signal("Pattern")
        futurist = latest_signal("Futurist")
        conn.close()

        # Support/resistance: prefer Trendy (explicit S/R fields), fall back to today's range
        if trendy:
            support    = float(trendy["stop_loss"])
            resistance = float(trendy["take_profit"])
        elif pattern:
            support    = float(pattern["stop_loss"])
            resistance = float(pattern["take_profit"])
        else:
            support    = day_low
            resistance = day_high

        # Team confidence = weighted average of bullish agents
        confidences = [s["confidence"] for s in (trendy, pattern, futurist) if s]
        team_conf = int(round(sum(confidences) / len(confidences) * 100)) if confidences else 50

        # ---------- 3. Gemma fills narrative only (numbers are locked) ----------
        signal_lines = []
        if trendy:
            signal_lines.append(f"Trendy (trend): conf {int(trendy['confidence']*100)}% — {trendy['reasoning'][:120]}")
        if pattern:
            signal_lines.append(f"Pattern (chart): conf {int(pattern['confidence']*100)}% — {pattern['reasoning'][:120]}")
        if futurist:
            signal_lines.append(f"Futurist (forecast): conf {int(futurist['confidence']*100)}% — {futurist['reasoning'][:120]}")
        signals_blob = "\n".join(signal_lines) if signal_lines else "(no fresh signals in last 24h)"

        prompt = (
            "You are writing a plain-English trading brief for a non-technical CEO. "
            "Output EXACTLY three short labelled sections, no preamble, no markdown, "
            "no quotes, no emoji. Keep each section to 1-2 short sentences.\n\n"
            "FACTS (use these — do NOT contradict):\n"
            f"- GME is currently ${current:.2f}, {direction} ({pct_vs_prev:+.1f}% vs prior close).\n"
            f"- Today's intraday range: ${day_low:.2f} to ${day_high:.2f}.\n"
            f"- Support ${support:.2f}, resistance ${resistance:.2f}.\n"
            f"- Team confidence: {team_conf}%.\n\n"
            f"AGENT SIGNALS (last 24h):\n{signals_blob}\n\n"
            "WRITE THESE THREE SECTIONS (fill brackets only, keep labels exact):\n"
            "PATTERN: [one sentence describing the chart pattern or price behavior, "
            "plain English, no jargon — if Pattern agent called a named formation use it]\n"
            "WAITING_FOR: [one sentence on what trigger (price level, indicator condition) "
            "would turn this into a trade]\n"
            "RISK: [one sentence on what single thing would invalidate today's plan]"
        )

        pattern_txt = "Price consolidating; no clear formation right now."
        waiting_txt = f"Break above ${resistance:.2f} with follow-through volume."
        risk_txt    = f"A close below ${support:.2f} cancels the setup."
        try:
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            resp = requests.post(
                f"{ollama_host}/api/generate",
                json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                      "options": {"num_predict": 250, "temperature": 0.3}},
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json().get("response", "").strip()
            # Parse labelled sections
            for line in text.splitlines():
                s = line.strip()
                if s.upper().startswith("PATTERN:"):
                    pattern_txt = s.split(":", 1)[1].strip()[:250] or pattern_txt
                elif s.upper().startswith("WAITING_FOR:") or s.upper().startswith("WAITING FOR:"):
                    waiting_txt = s.split(":", 1)[1].strip()[:250] or waiting_txt
                elif s.upper().startswith("RISK:"):
                    risk_txt = s.split(":", 1)[1].strip()[:250] or risk_txt
        except Exception as e:
            log.warning(f"[Briefing] LLM narrative failed ({e}), using fallback text")

        conf_tone = "team sees a clear setup" if team_conf >= 70 else \
                    "team is leaning in but wants confirmation" if team_conf >= 55 else \
                    "team is cautious, no strong edge"

        brief = (
            f"📍 MARKET: GME is at ${current:.2f}. It is {direction} today "
            f"({pct_vs_prev:+.1f}% vs prior close).\n\n"
            f"📐 PATTERN: {pattern_txt}\n\n"
            f"🎯 KEY LEVELS: Support at ${support:.2f}. Resistance at ${resistance:.2f}. "
            f"Today's range: ${day_low:.2f} to ${day_high:.2f}.\n\n"
            f"⏳ WAITING FOR: {waiting_txt}\n\n"
            f"⚠️ RISK: {risk_txt}\n\n"
            f"🔮 CONFIDENCE: {team_conf}% — {conf_tone}."
        )

        write_log("Briefing", brief[:2000], "daily_brief")
        from notifier import notify
        notify(f"<b>📋 DAILY STRATEGY BRIEF</b>\n\n{brief}")
        log.info(f"[Briefing] Brief sent — {direction} {pct_vs_prev:+.1f}% conf={team_conf}%")
    except Exception as e:
        log.error(f"[Briefing] {e}")
        write_log("Briefing", str(e), "daily_brief", "error")


def run_daily_huddle():
    """9:00 AM ET — Boss recaps yesterday + frames today's focus.

    Bypass pattern: yesterday's trades/predictions and today's active signals
    are pulled deterministically. Gemma only writes the 1-line focus. The
    previous CrewAI version was handed hardcoded 'No trades today' via the
    default make_daily_huddle_task() — Boss never saw the real data.
    """
    log.info("[Huddle] === DAILY TEAM BRIEFING ===")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Yesterday's trades (last 24h)
        trades = conn.execute(
            "SELECT action, symbol, entry_price, exit_price, pnl, status "
            "FROM trade_decisions WHERE datetime(timestamp) > datetime('now', '-1 day') "
            "ORDER BY timestamp DESC"
        ).fetchall()

        # Yesterday's scored predictions (have actual_price filled in)
        preds = conn.execute(
            "SELECT horizon, predicted_price, actual_price, error_pct FROM predictions "
            "WHERE datetime(timestamp) > datetime('now', '-1 day') "
            "AND actual_price IS NOT NULL ORDER BY timestamp DESC"
        ).fetchall()

        # Today's fresh agent signals (last 8h)
        signals = conn.execute(
            "SELECT agent_name, signal_type, confidence FROM signal_alerts "
            "WHERE datetime(timestamp) > datetime('now', '-8 hours') "
            "ORDER BY timestamp DESC"
        ).fetchall()
        conn.close()

        trades_line = (
            f"{len(trades)} trades: " + ", ".join(
                f"{t['action']} {t['symbol']} @ ${t['entry_price']:.2f}"
                + (f" → pnl ${t['pnl']:.2f}" if t['pnl'] is not None else "")
                for t in trades[:3]
            )
        ) if trades else "No trades yesterday."

        if preds:
            avg_err = sum(abs(p['error_pct']) for p in preds if p['error_pct'] is not None) / max(1, len(preds))
            preds_line = f"{len(preds)} predictions scored, avg abs error {avg_err:.1f}%."
        else:
            preds_line = "No scored predictions yet."

        # Team bias today (deterministic)
        confs = [s['confidence'] for s in signals] if signals else []
        team_conf = int(round(sum(confs) / len(confs) * 100)) if confs else 50
        n_bullish = len([s for s in signals if s['signal_type'] in
                         ('trend_signal', 'pattern_signal', 'price_prediction')])
        on_track = team_conf >= 60 and n_bullish >= 2
        on_track_line = "YES" if on_track else "NO" if signals else "UNKNOWN"

        # Gemma only writes the focus line — everything above is locked
        prompt = (
            "You are the Boss running a daily team huddle for a GME trading crew. "
            "Given these facts, write ONE short sentence stating the focus for today. "
            "No preamble, no markdown, no quotes, no label. Plain English, under 140 chars.\n\n"
            f"- Yesterday: {trades_line}\n"
            f"- Predictions: {preds_line}\n"
            f"- Active signals this morning: {len(signals)}, team conf {team_conf}%\n"
            f"- On track for profit: {on_track_line}\n"
        )
        focus_txt = (
            "Wait for confluence — only act when Trendy, Pattern, and Futurist all align."
        )
        try:
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            resp = requests.post(
                f"{ollama_host}/api/generate",
                json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                      "options": {"num_predict": 80, "temperature": 0.4}},
                timeout=30,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip().strip('"').strip("'")
            raw = " ".join(raw.split())
            if raw:
                focus_txt = raw[:160]
        except Exception as e:
            log.warning(f"[Huddle] LLM focus line failed ({e}), using fallback")

        brief = (
            "DIRECTIVE: Make money first, do good with it second.\n"
            f"YESTERDAY: {trades_line} {preds_line}\n"
            f"TODAY: {len(signals)} active signals, team conf {team_conf}%.\n"
            f"ON TRACK: {on_track_line}\n"
            f"FOCUS: {focus_txt}"
        )
        log.info(f"[Huddle]\n{brief}")
        write_log("Boss", brief, "daily_huddle")
    except Exception as e:
        log.error(f"[Huddle] {e}")
        write_log("Boss", str(e), "daily_huddle", "error")


def run_standup_report():
    """11 AM & 4 PM ET — Send agent performance standup to Telegram."""
    log.info("[Standup] === AGENT PERFORMANCE REPORT ===")
    try:
        conn = sqlite3.connect(DB_PATH)

        # Get signals from last 24 hours by agent
        signals = conn.execute("""
            SELECT agent_name, COUNT(*) as total, AVG(confidence) as avg_conf
            FROM signal_alerts
            WHERE datetime(timestamp) > datetime('now', '-1 day')
            GROUP BY agent_name
            ORDER BY total DESC
        """).fetchall()

        if not signals:
            log.info("[Standup] No signals in last 24 hours")
            conn.close()
            return

        # Get feedback stats for each agent
        feedback_stats = {}
        for agent_name, _, _ in signals:
            feedback = conn.execute("""
                SELECT
                    COUNT(*) as total_feedback,
                    SUM(CASE WHEN action_taken = 'executed' THEN 1 ELSE 0 END) as executed,
                    SUM(CASE WHEN action_taken = 'executed' AND pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                    AVG(CASE WHEN action_taken = 'executed' THEN pnl_pct END) as avg_pnl_pct
                FROM signal_feedback sf
                JOIN signal_alerts sa ON sf.alert_id = sa.id
                WHERE sa.agent_name = ? AND datetime(sf.execution_timestamp) > datetime('now', '-1 day')
            """, (agent_name,)).fetchone()
            if feedback:
                feedback_stats[agent_name] = feedback

        # Calculate team totals
        total_signals = sum(s[1] for s in signals)
        total_executed = sum(f[1] or 0 for f in feedback_stats.values())
        total_wins = sum(f[2] or 0 for f in feedback_stats.values())
        team_roi = (total_wins / total_executed * 100) if total_executed > 0 else 0

        lines = ["<b>🤖 AGENT DAILY STANDUP</b>\n"]

        for agent_name, total, avg_conf in signals:
            fb = feedback_stats.get(agent_name, (0, 0, 0, None))
            executed = fb[1] or 0
            wins = fb[2] or 0
            win_rate = (wins / executed * 100) if executed > 0 else 0
            conf_pct = int(avg_conf * 100) if avg_conf else 0

            status = "✨" if win_rate == 100 and executed > 0 else "✅" if win_rate >= 67 else "⚠️ " if executed > 0 else "🔹"
            lines.append(f"{status} <b>{agent_name}</b>: {total} signals, {conf_pct}% confidence")
            if executed > 0:
                lines.append(f"   → {executed} executed, {wins} wins ({win_rate:.0f}% win rate)")

        lines.append(f"\n<b>📈 Team ROI: {total_wins}/{total_executed} wins ({team_roi:.0f}% win rate)</b>")

        # Highlight best/worst
        if feedback_stats:
            best = max([(a, f[2]/f[1]*100 if f[1] else 0) for a, f in feedback_stats.items() if f[1]],
                      key=lambda x: x[1], default=("N/A", 0))
            worst = min([(a, f[2]/f[1]*100 if f[1] else 0) for a, f in feedback_stats.items() if f[1]],
                       key=lambda x: x[1], default=("N/A", 0))

            if best[0] != "N/A":
                lines.append(f"🌟 Best: <b>{best[0]}</b> ({best[1]:.0f}%)")
            if worst[0] != "N/A" and worst[1] < 100:
                lines.append(f"📍 Needs tuning: <b>{worst[0]}</b> ({worst[1]:.0f}%)")

        conn.close()

        msg = "\n".join(lines)
        from notifier import notify
        notify(msg)
        log.info("[Standup] Report sent to Telegram")
        write_log("Standup", msg[:2000], "standup_report")
    except Exception as e:
        log.error(f"[Standup] {e}")
        write_log("Standup", str(e), "standup_report", "error")


# ── Orchestrator class ─────────────────────────────────────────────────────────

class TradingSystemOrchestrator:
    def __init__(self):
        init_db()

        # Verify Ollama is ready before scheduling agents
        if not check_ollama_ready():
            log.critical("[TradingSystemOrchestrator] Ollama unavailable; cannot start system")
            sys.exit(1)

        self.scheduler = BackgroundScheduler(timezone="America/New_York")
        with open(os.path.join(os.path.dirname(__file__), "risk_rules.yaml")) as f:
            self.risk_rules = yaml.safe_load(f)

    def configure_schedule(self):
        # Synthesis — runs first so all agents have fresh shared context
        self.scheduler.add_job(run_synthesis,    IntervalTrigger(minutes=5),   id="synthesis")

        # Intraday agents with fallback to Gemma
        self.scheduler.add_job(run_validation,  IntervalTrigger(minutes=5),   id="valerie")
        self.scheduler.add_job(run_commentary,  IntervalTrigger(minutes=5),   id="chatty")

        # Cloud agents — rate-limited
        self.scheduler.add_job(run_news,         IntervalTrigger(minutes=30), id="newsie")
        self.scheduler.add_job(run_pattern,      IntervalTrigger(hours=2),    id="pattern")   # was 6h
        self.scheduler.add_job(run_daily_trend,  IntervalTrigger(hours=4),    id="trendy_interval")  # intraday

        # Signal confidence loop agents — bypass pattern (pre-fetched tools, direct Ollama)
        self.scheduler.add_job(run_trendy_signal,    IntervalTrigger(hours=4),    id="trendy_signal")
        self.scheduler.add_job(run_pattern_signal,   IntervalTrigger(hours=2),    id="pattern_signal")
        self.scheduler.add_job(run_futurist_prediction_signal, IntervalTrigger(hours=2), id="futurist_signal")
        # Dropped: run_synthesis_signal, run_newsie_signal, run_futurist_cycle —
        # CrewAI twins of run_synthesis / run_news / run_futurist_prediction_signal.
        # Gemma hallucinated without tool output; bypass versions already cover these.
        self.scheduler.add_job(run_georisk,      IntervalTrigger(hours=1),    id="georisk")   # hourly geopolitical scan

        # Daily jobs (market-hours aware) — timezone pinned to ET on every trigger
        # since BackgroundScheduler(timezone=...) default isn't being honored on
        # this host (suspected missing tzdata).
        self.scheduler.add_job(run_daily_huddle,      CronTrigger(hour=9,  minute=0,  timezone=ET), id="huddle")
        self.scheduler.add_job(run_daily_briefing,    CronTrigger(hour=10, minute=0,  timezone=ET), id="briefing")
        self.scheduler.add_job(run_standup_report,    CronTrigger(hour=11, minute=0,  timezone=ET), id="standup_midday")
        self.scheduler.add_job(run_daily_trend,       CronTrigger(hour=20, minute=0,  timezone=ET), id="trendy_eod")
        self.scheduler.add_job(run_daily_aggregation, CronTrigger(hour=16, minute=35, timezone=ET), id="aggregator")
        self.scheduler.add_job(run_intraday_aggregation, IntervalTrigger(minutes=5), id="aggregator_intraday")
        self.scheduler.add_job(run_voice_forwarder, IntervalTrigger(minutes=10), id="voice_forwarder")
        self.scheduler.add_job(run_standup_report,    CronTrigger(hour=16, minute=0,  timezone=ET), id="standup_close")

        # Learning sessions — agents review their own performance and adapt
        self.scheduler.add_job(run_learning_debrief, CronTrigger(hour=16, minute=30, timezone=ET), id="debrief")
        # 5-min buffer after debrief — performance_scores has settled before
        # the producer mines signal_scores for new graduated lessons.
        self.scheduler.add_job(run_lesson_producer,  CronTrigger(hour=16, minute=35, timezone=ET), id="lesson_producer")
        self.scheduler.add_job(run_weekly_review,    CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=ET), id="weekly_review")

        # CTO structural intelligence — PE playbook monitoring and short side research
        self.scheduler.add_job(run_cto_daily_brief,    CronTrigger(hour=9,  minute=5,  timezone=ET),                   id="cto_brief")
        self.scheduler.add_job(run_cto_trove_score,    CronTrigger(hour=9,  minute=10, timezone=ET),                   id="cto_trove")
        self.scheduler.add_job(run_cto_structural_scan, CronTrigger(day_of_week="sun", hour=8, minute=0, timezone=ET), id="cto_scan")
        self.scheduler.add_job(run_investor_intel_scan, CronTrigger(hour=8, minute=0, timezone=ET),                    id="investor_intel")

        # Options intelligence — max pain every Monday pre-market
        self.scheduler.add_job(run_options_update, CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=ET), id="options")

        # Weekly coffee nudge — Sundays 10:00 AM ET
        self.scheduler.add_job(run_sunday_support_message, CronTrigger(day_of_week="sun", hour=10, minute=0, timezone=ET), id="sunday_support")

        # Social monitor — scan tracked accounts every 15 minutes during market hours
        self.scheduler.add_job(run_social_scan, IntervalTrigger(minutes=15), id="social")

        # Periodic intelligence digest — every 4 hours to Telegram
        self.scheduler.add_job(run_periodic_brief, IntervalTrigger(hours=4), id="periodic_brief")

        # Prediction calibration — truth-serum for stated confidence numbers.
        # Scores predictions against the price at (made_at + horizon), not EOD
        # close. Runs every 10 min so we catch 1h predictions the moment their
        # window elapses instead of waiting until 4:30 PM debrief.
        self.scheduler.add_job(run_calibration, IntervalTrigger(minutes=10), id="calibration")

        # Nightly DB maintenance: backup + prune old backups + log cleanup (3 AM ET)
        from db_maintenance import nightly_maintenance
        self.scheduler.add_job(nightly_maintenance, CronTrigger(hour=3, minute=0, timezone=ET), id="db_nightly")

    def start(self):
        self.configure_schedule()
        self.scheduler.start()
        start_bot_thread()
        start_sync_thread()
        write_log("Orchestrator", "All 10 agents online", "startup")

        # Sanity-check: print next fire time of the daily briefing so we can
        # confirm cron triggers are honoring ET and not falling back to host tz.
        try:
            j = self.scheduler.get_job("briefing")
            if j and j.next_run_time:
                log.info(f"[tz-check] briefing next_run_time: {j.next_run_time.isoformat()}")
        except Exception:
            pass

        log.info("""
╔══════════════════════════════════════════════════════════════════╗
║      GME Multi-Agent Trading System — ONLINE (Gemma-first)       ║
╠══════════════════════════════════════════════════════════════════╣
║  Synthesis (cross-agent brief) every 5 min — shared context      ║
║  Valerie  (data validator)     every 5 min                       ║
║  Chatty   (commentary)         every 5 min — reads Synthesis     ║
║  Newsie   (news sentiment)     every 30 min                      ║
║  Pattern  (multi-day)          every 2 hours                     ║
║  Trendy   (daily trend)        every 4 hours + 8:00 PM ET EOD    ║
║  Futurist (strategic signal)   every 2 hours (gate-checked)      ║
║  Voice Forwarder               every 10 min — agent→Telegram     ║
║  Boss     (daily huddle)       9:00 AM ET — mission briefing     ║
║  CTO      (structural brief)   9:05 AM ET — PE playbook + shorts ║
║  CTO      (Trove score)        9:10 AM ET — deep-value rating    ║
║  Daily Strategy Brief         10:00 AM ET — team game plan       ║
║  📊 STANDUP (agent perf)       11:00 AM & 4:00 PM ET — ROI check ║
║  Aggregator                    4:35 PM ET                        ║
║  Learner  (daily debrief)      4:30 PM ET — score + adapt        ║
║  LessonProducer                4:35 PM ET — mine new lessons     ║
║  Learner  (weekly review)      Fridays 5:00 PM ET                ║
║  CTO      (structural scan)    Sundays 8:00 AM — EDGAR + shorts  ║
║  ☕ Support message            Sundays 10:00 AM — coffee nudge    ║
╚══════════════════════════════════════════════════════════════════╝
        """)

        # Warm up: build shared context before the first full cycle
        run_investor_intel_scan()
        run_synthesis()
        run_futurist_prediction_signal()

        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.scheduler.shutdown()


if __name__ == "__main__":
    TradingSystemOrchestrator().start()
