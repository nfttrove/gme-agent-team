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
from yahoo_finance_feed import start_yahoo_feed
from safe_kickoff import safe_kickoff, safe_kickoff_with_fallback, CrewTimeout
from db_maintenance import enable_wal_mode
from signal_manager import SignalManager
from notifier import notify_signal_alert
import signal_gate
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
    """Verify Ollama is reachable and gemma2:9b is pulled — only when STREAM_MODE=0.

    When STREAM_MODE=1 (production default — Gemini Flash is primary, see
    project_llm_primary_gemini_flash.md), Ollama is unused so this check is
    skipped. Avoids forcing the user to keep gemma2:9b pulled when running
    cloud-primary to free local RAM for OBS streaming.
    """
    if os.getenv("STREAM_MODE", "").strip() in {"1", "true", "yes", "on"}:
        log.info("[check_ollama_ready] STREAM_MODE=on — skipping Ollama check (cloud-primary)")
        return True

    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        response = requests.get(f"{ollama_host}/api/tags", timeout=2)
        models = [m["name"] for m in response.json().get("models", [])]

        if "gemma2:9b" not in models:
            log.error(f"[check_ollama_ready] REQUIRED: gemma2:9b not found. Available: {models}")
            return False

        log.info(f"[check_ollama_ready] Ollama ready ({len(models)} models)")
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


def _live_intraday_volume(symbol: str) -> float | None:
    """Cumulative regular-session volume from yfinance. Returns None on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).fast_info
        v = getattr(info, "last_volume", None) or info.get("lastVolume")
        return float(v) if v else None
    except Exception as e:
        log.warning(f"[_live_intraday_volume] {e}")
        return None


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

    MARKET_OPEN  = dtime(9, 30)
    MARKET_CLOSE = dtime(16, 0)
    t = now_et.time().replace(tzinfo=None)

    # Today's cumulative volume. The TradingView webhook only delivers ~3% of
    # GME minute bars (see commit 594319f), so summing price_ticks intraday
    # under-counts by ~30x. During the regular session, fetch the cumulative
    # session volume live from yfinance; fall back to daily_candles / price_ticks.
    today_vol = 0.0
    if MARKET_OPEN <= t < MARKET_CLOSE:
        live = _live_intraday_volume(symbol)
        if live and live > 0:
            today_vol = live
    if today_vol == 0:
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

        # Tight bypass-pattern prompt: list the *only* numbers Gemma may cite,
        # explicitly separate "today's range" from "5-day range" so they can't
        # be conflated, and refuse to fabricate support/resistance levels.
        # Earlier Chatty output ("Buyers reclaim $24.34, testing resistance
        # near $25.43. Today's range $23.69–$24.34") was Gemma reading the
        # 5-day range line and labelling it as today's — that's the failure
        # mode this prompt structure prevents.
        prev_close_line = (
            f"  prev close (yesterday): ${fact['prev_close']:.2f}"
            if fact.get('prev_close') else "  prev close: unavailable"
        )
        today_range_line = (
            f"  today's range: ${fact['today_low']:.2f}-${fact['today_high']:.2f} ({fact['today_ticks']} ticks)"
            if fact.get('today_low') is not None
            else "  today's range: no ticks yet today"
        )
        prompt = (
            "You are GME's live-stream commentator. Produce ONE punchy insight "
            "(max 120 chars). No preamble, no quotes, no markdown.\n\n"
            "FACTS — these are the ONLY numbers you may cite:\n"
            f"  current price: ${price:.2f}\n"
            f"  direction: {fact['direction']} ({fact['pct_change']:+.2f}% vs prev close)\n"
            f"{prev_close_line}\n"
            f"{today_range_line}\n"
            f"  volume regime: {vol_label} ({vol_ratio:.2f}x 20d ADV)\n"
            f"  team consensus: {synthesis_text}\n\n"
            "RULES:\n"
            "- Cite ONLY the prices above. NEVER invent support/resistance levels.\n"
            "- If you reference 'today's range', use EXACTLY the today's range numbers — "
            "do not substitute 5-day or weekly figures.\n"
            "- Direction must match — never say 'rising'/'rallying' when FALLING.\n"
            "- Use the volume label verbatim — do NOT substitute synonyms.\n"
        )

        from llm_config import llm_generate
        # Lower temperature than free-form narrative — Chatty's job is to
        # describe locked numbers, not to riff.
        comment = llm_generate(prompt, num_predict=80, temperature=0.3, timeout=30)
        comment = comment.strip().strip('"').strip("'")
        # Collapse to first line — model sometimes adds a second "explanation" line
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
        log.error("[Chatty] LLM timeout")
        write_log("Chatty", "LLM timeout after 30s", "commentary", "timeout")
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
            from news_filter import filter_articles
            raw = NewsAPITool()._run("GME")
            raw = [a for a in raw if a.get("headline") and "error" not in a]
            # Disambiguate the GME ticker: Global/Graduate Medical Equipment/Education
            # share the three letters and pollute the sentiment composite.
            articles = filter_articles(raw)
            dropped = len(raw) - len(articles)
            if dropped:
                log.info(f"[Newsie] dropped {dropped} non-GameStop GME articles")
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
            from llm_config import llm_generate_grounded
            narrative = ""
            try:
                narrative = llm_generate_grounded(prompt, num_predict=120, temperature=0.5, timeout=30)
                narrative = narrative.strip().strip('"').strip("'")
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
            from llm_config import llm_generate
            candidate = llm_generate(prompt, num_predict=120, temperature=0.2, timeout=30)
            candidate = candidate.strip().strip('"').strip("'").split("\n")[0].strip()
            if 20 < len(candidate) < 300:
                sentence = candidate[:220]
        except Exception as e:
            log.warning(f"[Pattern] narration fallback — LLM error: {e}")

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

    # 5-day swing levels — 20d min/max drags S/R to stale prints from prior
    # consolidation (e.g. a 3-week-old low that's 10%+ below market once a
    # trend kicks in), producing limit-order entries that never fill.
    swing = min(5, len(lows))
    support_hint = round(min(lows[-swing:]), 2)
    resistance_hint = round(max(highs[-swing:]), 2)
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
        f"  5d swing: low={support_hint:.2f}  high={resistance_hint:.2f}  "
        f"latest_close={closes[-1]:.2f}\n\n"
        "Schema (all fields required):\n"
        '{"trend_direction": "UP"|"DOWN"|"SIDEWAYS", "confidence": <0.0-1.0>, '
        '"support_level": <float>, "resistance_level": <float>, '
        '"reasoning": "<trader-terse, <=15 words, cite specific indicator values. '
        'FORBIDDEN phrases: \'directional conviction\', \'lack of\', \'indicating\'>", '
        '"severity": "HIGH"|"MEDIUM"|"LOW"}\n\n'
        "Rules: UP requires price > VWAP AND price > EMA21. DOWN requires price < VWAP AND price < EMA21. "
        "Otherwise SIDEWAYS. Confidence <= 0.55 if EMAs disagree or RSI in 45-55. "
        f"support_level MUST equal {support_hint}, resistance_level MUST equal {resistance_hint}. severity=HIGH if confidence>=0.75."
    )

    from llm_config import llm_generate
    raw = llm_generate(prompt, num_predict=300, temperature=0.2, timeout=60)
    data = _extract_json(raw)
    if not data:
        return None, f"parse error: {raw[:300]}"
    try:
        signal = TrendySignal(**data)
    except Exception as e:
        return None, f"validation error: {e} | raw={raw[:300]}"

    from message_formatters import tighten_prose
    reasoning_clean = tighten_prose(signal.reasoning)[:220]
    narrative = (
        f"{signal.trend_direction} (conf={signal.confidence:.0%}) · "
        f"S=${signal.support_level:.2f} R=${signal.resistance_level:.2f} · {reasoning_clean}"
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

            from llm_config import llm_generate
            raw = llm_generate(prompt, num_predict=300, temperature=0.3, timeout=60)
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

            # Episodic logging — soft-fail so a log issue never blocks signal
            # emit. Mirrors the synthesis pattern. We bypass episodic_integration's
            # `log_futurist_prediction(text)` wrapper because its
            # extract_prediction_from_output regex expects an older nested JSON
            # shape that no longer matches the orchestrator's `raw` output. We
            # already have a validated Pydantic prediction, so call log_prediction
            # directly — same destination, no parse step.
            try:
                # Import via episodic_integration (which adds .agent/ to
                # sys.path) instead of directly — guarantees the import
                # works under launchd regardless of cwd.
                from episodic_integration import log_prediction
                log_prediction(
                    agent_name="Futurist",
                    predicted_price=prediction.predicted_price,
                    confidence=prediction.confidence,
                    horizon=str(prediction.horizon),
                    bias=str(prediction.bias),
                    reasoning=prediction.reasoning[:500],
                )
            except Exception as e:
                log.warning(f"[Futurist] episodic log failed: {e}")

            # Log signal and notify (only if confidence is actionable)
            if prediction.confidence >= 0.60 and prediction.stop_loss and prediction.take_profit:
                # /coach Futurist diagnosis (2026-05-16): high-conf BULL signals
                # are inversely correlated with accuracy — Pro found the agent
                # calls tops in low-vol chop. Invert BULL>75% conf to BEAR
                # (flip bias + swap SL/TP). Stays gated by signal_gate so the
                # inverted signals get scored before reaching Telegram, and
                # the original prediction is already persisted upstream for
                # the agent's own accuracy tracking.
                emit_bias = prediction.bias
                emit_sl = prediction.stop_loss
                emit_tp = prediction.take_profit
                inversion_tag = ""
                if (prediction.bias or "").upper().startswith("BULL") and prediction.confidence > _FUTURIST_INVERT_BULL_CONF_FLOOR:
                    emit_bias = "BEARISH"
                    emit_sl, emit_tp = prediction.take_profit, prediction.stop_loss
                    inversion_tag = f"[INVERTED: orig BULL conf={prediction.confidence:.0%}] "
                    log.info(f"[Futurist] inverting high-conf BULL → BEAR (orig conf={prediction.confidence:.2f})")
                    write_log("Futurist",
                              f"INVERT high-conf BULL→BEAR (orig conf={prediction.confidence:.2%}) per /coach diagnosis",
                              "prediction_signal", "inverted")

                gate = signal_gate.evaluate("Futurist", DB_PATH)
                gated = gate["decision"] != "EMIT"

                manager = SignalManager(DB_PATH)
                alert_id = manager.log_alert(
                    agent_name="Futurist",
                    signal_type=prediction.signal_type,
                    confidence=prediction.confidence,
                    severity="HIGH" if prediction.confidence >= 0.80 else ("MEDIUM" if prediction.confidence >= 0.65 else "LOW"),
                    entry_price=prediction.predicted_price * 0.99,
                    stop_loss=emit_sl,
                    take_profit=emit_tp,
                    reasoning=inversion_tag + (f"[{gate['decision']}] " if gated else "") + prediction.reasoning[:480],
                    paper_trade=not gated,
                )
                if gated:
                    log.info(f"[Futurist] gate={gate['decision']} · {gate['reason']} — Telegram + paper trade suppressed")
                    write_log("Futurist",
                              f"GATE {gate['decision']} · {gate['reason']} · {summary[:200]}",
                              "prediction_signal", "gated")
                    metrics.snapshot()
                    return
                try:
                    notify_signal_alert(
                        agent_name="Futurist",
                        signal_type=prediction.signal_type,
                        confidence=prediction.confidence,
                        entry_price=prediction.predicted_price * 0.99,
                        stop_loss=emit_sl,
                        take_profit=emit_tp,
                        reasoning=inversion_tag + prediction.reasoning,
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

            gate = signal_gate.evaluate("Trendy", DB_PATH)
            gated = gate["decision"] != "EMIT"

            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Trendy",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reasoning=(f"[{gate['decision']}] " if gated else "") + signal.reasoning[:480],
                paper_trade=not gated,
            )
            if gated:
                log.info(f"[Trendy] gate={gate['decision']} · {gate['reason']} — Telegram + paper trade suppressed")
                write_log("Trendy",
                          f"GATE {gate['decision']} · {gate['reason']} · {narrative[:200]}",
                          "trend_signal", "gated")
                metrics.snapshot()
                return
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

            gate = signal_gate.evaluate("Pattern", DB_PATH)
            gated = gate["decision"] != "EMIT"

            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Pattern",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reasoning=(f"[{gate['decision']}] " if gated else "") + signal.reasoning[:480],
                paper_trade=not gated,
            )
            if gated:
                log.info(f"[Pattern] gate={gate['decision']} · {gate['reason']} — Telegram + paper trade suppressed")
                write_log("Pattern",
                          f"GATE {gate['decision']} · {gate['reason']} · {narrative[:200]}",
                          "pattern_signal", "gated")
                metrics.snapshot()
                return
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


# ── Intraday pattern signal (5-minute bars) ──────────────────────────────────

# Intraday parameters chosen by the task spec — daily detector uses 0.60 conf
# and ±4%/±6% R/R; intraday is noisier so we raise the bar.
# 2026-05-08: bumped floor 0.70 → 0.80 after 7-day audit showed 80–90% conf
# bucket had 16% directional hit rate. Floor alone wasn't enough — the same
# (pattern_type, direction, level) was re-emitting on every 5-min bar, so we
# also dedupe within a 4-hour rolling window (see _intraday_dedupe_recent).
_INTRADAY_PATTERN_CONFIDENCE_FLOOR = 0.80
_INTRADAY_STOP_PCT = 0.015   # ±1.5% stop
_INTRADAY_TARGET_PCT = 0.025  # ±2.5% target
_INTRADAY_LOOKBACK_BARS = 60  # ~5 hours of 5-min bars
# 0.3% of price per 5-min bar = the floor below which moves are noise. Set
# from /coach Pattern Intraday diagnosis (2026-05-16): agent's BULL was at
# 18% hit rate because it fired on 0.0% chop. Tune up if too restrictive.
_INTRADAY_ATR_FLOOR_PCT = 0.003

# /coach Futurist (2026-05-16) found high-conf BULL signals are inversely
# correlated with accuracy — Pro hypothesis: the agent calls tops in low-vol
# chop. Above this floor we flip BULL → BEAR (and swap SL/TP). Experimental;
# revisit after 14d to see whether inverted signals score above the gate's
# 30% suppress floor. The original prediction still lands in `predictions`
# table for agent-accuracy tracking; only the emitted signal is flipped.
_FUTURIST_INVERT_BULL_CONF_FLOOR = 0.75
_INTRADAY_AGG_LOOKBACK_MINUTES = 360  # aggregate the last 6 hours of ticks
_INTRADAY_DEDUPE_WINDOW_HOURS = 4  # don't re-emit same setup within this window
_INTRADAY_DEDUPE_LEVEL_TOL = 0.015  # ±1.5% breakout-level tolerance for "same setup"


def _fetch_intraday_candles(lookback_bars: int = _INTRADAY_LOOKBACK_BARS) -> list[dict]:
    """Read the most recent N 5-minute candles from intraday_candles, oldest
    first. Returns [] if the table is empty or missing."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT bucket_start AS date, open, high, low, close, volume "
                "FROM intraday_candles "
                "WHERE symbol='GME' AND interval='5m' "
                "ORDER BY bucket_start DESC LIMIT ?",
                (lookback_bars,),
            )
        except sqlite3.OperationalError:
            return []  # table doesn't exist yet
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows
    finally:
        conn.close()


def _intraday_dedupe_recent(pattern_type: str, direction: str, level: float) -> dict | None:
    """Return the most recent Pattern Intraday alert within the dedupe window
    that matches (pattern_type, direction) and whose entry_price is within
    ``_INTRADAY_DEDUPE_LEVEL_TOL`` of ``level``. Returns ``None`` if no match,
    meaning this signal is "novel" and should emit.

    The detector re-fires the same setup on every 5-min bar while price stays
    near the breakout level, so without this we get clusters of 3–12 identical
    emits per setup. Match by reasoning prefix (e.g. "breakdown detected") and
    entry_price (== breakout_level on emit), since neither is a column.
    """
    import re
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            f"""
            SELECT created_at, reasoning, entry_price, stop_loss
            FROM signal_alerts
            WHERE agent_name = 'Pattern Intraday'
              AND created_at > datetime('now', '-{_INTRADAY_DEDUPE_WINDOW_HOURS} hours')
            ORDER BY created_at DESC
            """,
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    pat_lower = pattern_type.lower()
    for created_at, reasoning, entry_price, stop_loss in rows:
        if entry_price is None or stop_loss is None:
            continue
        # Strip optional [EMIT]/[SHADOW]/[SUPPRESS] prefix the gate adds.
        text = re.sub(r"^\[\w+\]\s*", "", reasoning or "").lower()
        m = re.match(r"([a-z]+(?:\s+[a-z]+)?)\s+detected", text)
        if not m:
            continue
        if m.group(1) != pat_lower:
            continue
        prior_dir = "DOWN" if stop_loss > entry_price else "UP"
        if prior_dir != direction:
            continue
        if abs(entry_price - level) / max(level, 0.01) > _INTRADAY_DEDUPE_LEVEL_TOL:
            continue
        return {
            "created_at": created_at,
            "entry_price": entry_price,
            "pattern_type": pat_lower,
            "direction": prior_dir,
        }
    return None


def _compute_intraday_pattern_signal():
    """Deterministic intraday pattern detection on 5-minute bars.

    Mirrors `_compute_pattern_signal` but reads from `intraday_candles`
    instead of `daily_candles` and tunes the detector via the new `config`
    arg (higher MIN_CANDLES because 5-min noise drowns short windows).

    Re-aggregates ticks first so a fresh cycle sees fresh bars even if the
    standalone aggregator job hasn't fired.
    """
    from models.agent_outputs import PatternSignal
    from pattern_detector import detect_patterns
    import intraday_aggregator

    try:
        intraday_aggregator.aggregate_5m_bars(_INTRADAY_AGG_LOOKBACK_MINUTES)
    except Exception as e:
        log.warning(f"[Pattern Intraday] aggregator failed (continuing with stale): {e}")

    candles = _fetch_intraday_candles()
    if not candles or len(candles) < 30:
        return None, f"insufficient intraday candles ({len(candles)} < 30)"

    report = detect_patterns(candles, config={"MIN_CANDLES": 30})
    if report is None:
        return None, "intraday detector returned no report (data quality)"

    # ATR chop filter (added 2026-05-16 per /coach diagnosis: BULL 18% on 50
    # signals, agent was firing on low-volatility noise). Block when the 14-bar
    # ATR is below 0.3% of price — that's low enough to call genuine chop on a
    # ~$21 stock without filtering active sessions. Relative threshold so it
    # scales if GME repirces.
    atr14 = report.indicators.get("atr14") if report.indicators else None
    last_close = report.indicators.get("price") if report.indicators else None
    if atr14 and last_close and last_close > 0:
        atr_pct = atr14 / last_close
        if atr_pct < _INTRADAY_ATR_FLOOR_PCT:
            return None, f"ATR chop filter: 5m ATR {atr_pct:.2%} < floor {_INTRADAY_ATR_FLOOR_PCT:.2%}"

    # No LLM narration on the intraday path — the cycle runs every 5 min so
    # a 30s Gemma call would dominate cost for cosmetic prose. Use the
    # deterministic default sentence directly.
    sentence = _default_pattern_sentence(report)

    try:
        signal = PatternSignal(
            pattern_type=report.pattern_type,
            confidence=report.confidence,
            breakout_level=report.breakout_level,
            breakout_direction=report.breakout_direction,
            reasoning=sentence[:220],
            severity=report.severity,
            signal_type="intraday_pattern_signal",
        )
    except Exception as e:
        return None, f"validation error: {e} | report={report.as_dict()}"

    if signal.pattern_type == "none":
        narrative = signal.reasoning[:220]
    else:
        narrative = (
            f"{signal.pattern_type} (5m) · {signal.breakout_direction} break @ "
            f"${signal.breakout_level:.2f} (conf={signal.confidence:.0%}) · "
            f"{signal.reasoning[:220]}"
        )
    return signal, narrative


@active_window_required
def run_intraday_pattern_signal():
    """Every 5 min — intraday pattern detection on 5-minute bars + Telegram alert."""
    log.info("[Pattern Intraday] Starting intraday pattern signal cycle")
    write_log("Pattern Intraday", "Running intraday pattern signal cycle",
              "intraday_pattern_signal", "running")

    with metrics.cycle("intraday_pattern_signal"):
        try:
            signal, narrative = _compute_intraday_pattern_signal()
            if signal is None:
                log.warning(f"[Pattern Intraday] {narrative}")
                status = ("parse_error" if "parse" in narrative
                          else "validation_error" if "validation" in narrative
                          else "error")
                write_log("Pattern Intraday", narrative, "intraday_pattern_signal", status)
                return

            write_log("Pattern Intraday", narrative, "intraday_pattern_signal", "ok")
            log.info(f"[Pattern Intraday] {narrative}")

            if (signal.pattern_type == "none"
                    or signal.confidence < _INTRADAY_PATTERN_CONFIDENCE_FLOOR):
                metrics.snapshot()
                return

            dupe = _intraday_dedupe_recent(
                signal.pattern_type, signal.breakout_direction, signal.breakout_level,
            )
            if dupe is not None:
                msg = (f"dedup · same {dupe['pattern_type']} {dupe['direction']} "
                       f"@ ~${dupe['entry_price']:.2f} emitted at {dupe['created_at']}")
                log.info(f"[Pattern Intraday] {msg}")
                write_log("Pattern Intraday", msg, "intraday_pattern_signal", "deduped")
                metrics.snapshot()
                return

            entry_price = signal.breakout_level
            if signal.breakout_direction == "UP":
                stop_loss = round(signal.breakout_level * (1 - _INTRADAY_STOP_PCT), 2)
                take_profit = round(signal.breakout_level * (1 + _INTRADAY_TARGET_PCT), 2)
            else:
                stop_loss = round(signal.breakout_level * (1 + _INTRADAY_STOP_PCT), 2)
                take_profit = round(signal.breakout_level * (1 - _INTRADAY_TARGET_PCT), 2)

            gate = signal_gate.evaluate("Pattern Intraday", DB_PATH)
            gated = gate["decision"] != "EMIT"

            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Pattern Intraday",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reasoning=(f"[{gate['decision']}] " if gated else "") + signal.reasoning[:480],
                paper_trade=not gated,
            )
            if gated:
                log.info(f"[Pattern Intraday] gate={gate['decision']} · {gate['reason']} — Telegram + paper trade suppressed")
                write_log("Pattern Intraday",
                          f"GATE {gate['decision']} · {gate['reason']} · {narrative[:200]}",
                          "intraday_pattern_signal", "gated")
                metrics.snapshot()
                return
            try:
                notify_signal_alert(
                    agent_name="Pattern Intraday",
                    signal_type=signal.signal_type,
                    confidence=signal.confidence,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=(f"{signal.pattern_type} (5m) | "
                               f"{signal.breakout_direction} break @ "
                               f"${signal.breakout_level:.2f} — "
                               f"{signal.reasoning[:160]}"),
                    alert_id=alert_id,
                )
            except Exception as e:
                log.warning(f"[Pattern Intraday] Alert failed: {e}")

            metrics.snapshot()

        except Exception as e:
            log.error(f"[Pattern Intraday] {e}")
            write_log("Pattern Intraday", str(e), "intraday_pattern_signal", "error")


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


def run_intraday_aggregation():
    """Re-aggregate today's ticks into daily_candles so mid-day readers
    (Trendy, Pattern, Futurist) see current-day data instead of yesterday's.

    Intentionally NOT @active_window_required: pre-market ticks should produce
    a (partial) candle so agents reading daily_candles before 08:30 ET don't
    see yesterday's row. The aggregator is pure SQL — it does DELETE+INSERT
    atomically and rewrites itself on each run as more ticks arrive."""
    try:
        import daily_aggregator
        daily_aggregator.aggregate_day()
    except Exception as e:
        log.error(f"[Aggregator-intraday] {e}")
        write_log("Aggregator", str(e), "intraday_aggregation", "error")


def run_history_overwrite():
    """Nightly — re-fetch the last year of GME daily candles from yfinance and
    overwrite local rows. Tick-derived rows have been chronically partial
    (TradingView webhook delivers only a fraction of minute bars), poisoning
    20-day ADV used by Chatty's volume-regime label. yfinance reports
    exchange-authoritative volume."""
    try:
        from import_history import import_history
        result = import_history(years=1, overwrite=True)
        log.info(f"[HistoryImport] {result}")
        write_log("HistoryImport",
                  f"deleted={result['deleted']} inserted={result['inserted']}",
                  "history_overwrite")
    except Exception as e:
        log.error(f"[HistoryImport] {e}")
        write_log("HistoryImport", str(e), "history_overwrite", "error")


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
            "<i>Happy Sunday.</i>"
        )
        log.info("[Support] Sunday support message sent")
    except Exception as e:
        log.error(f"[Support] Failed: {e}")


def run_sunday_week_ahead():
    """Sunday 13:00 ET (18:00 BST) — calendar setup for the upcoming week.

    One-ping preview: this Friday's expiry, last-seen max pain, upcoming earnings
    if within 30 days, trading-day count to the user's end-of-May deadline.
    Pairs with the morning sunday_support_message; together they're the only
    two weekend pings.
    """
    try:
        from week_ahead import build_week_ahead_snapshot
        from notifier import notify_week_ahead

        snapshot = build_week_ahead_snapshot(DB_PATH)
        notify_week_ahead(snapshot)
        write_log("WeekAhead", f"sent · friday={snapshot.next_friday} earnings_days={snapshot.earnings_days_away}", "week_ahead")
        log.info("[WeekAhead] Sunday preview sent")
    except Exception as e:
        log.error(f"[WeekAhead] Failed: {e}")
        write_log("WeekAhead", str(e), "week_ahead", "error")


def run_promo_broadcast():
    """Broadcast the @mygmebot promo card (mascot/QR + caption) to Telegram."""
    try:
        from notifier import notify_promo
        ok = notify_promo()
        log.info(f"[Promo] broadcast {'sent' if ok else 'failed'}")
    except Exception as e:
        log.error(f"[Promo] Failed: {e}")


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


def _compose_dv_section(conn, top_n: int | None = None) -> str:
    """Build the DEEP VALUE block for the Saturday review.

    Pulls the latest snapshot from dv_score_history, optionally compares
    to the closest snapshot from 5–8 days prior, and renders one line per
    ticker with score, stars, price, and a week-over-week delta tag.

    `top_n=None` (default) shows every ticker in the snapshot. Pass a
    positive int to cap (used only by tests that want a smaller subset).

    Returns "" if the table is empty or unreachable — the brief composer
    will then omit the section entirely (no 'DV: no data' scaffolding).
    """
    try:
        latest_date_row = conn.execute(
            "SELECT MAX(score_date) FROM dv_score_history"
        ).fetchone()
        latest_date = latest_date_row[0] if latest_date_row else None
        if not latest_date:
            return ""

        total_in_snapshot = conn.execute(
            "SELECT COUNT(*) FROM dv_score_history WHERE score_date = ?",
            (latest_date,),
        ).fetchone()[0]

        if top_n is None:
            latest = conn.execute(
                "SELECT ticker, score, rating, price_at_score "
                "FROM dv_score_history WHERE score_date = ? "
                "ORDER BY score DESC",
                (latest_date,),
            ).fetchall()
        else:
            latest = conn.execute(
                "SELECT ticker, score, rating, price_at_score "
                "FROM dv_score_history WHERE score_date = ? "
                "ORDER BY score DESC LIMIT ?",
                (latest_date, top_n),
            ).fetchall()
        if not latest:
            return ""

        # Find the most recent prior snapshot 5–8 days back. 5d window absorbs
        # weekday/weekend cron drift; 8d ceiling keeps us comparing against a
        # genuinely prior week, not last Thursday.
        prior_date_row = conn.execute(
            "SELECT MAX(score_date) FROM dv_score_history "
            "WHERE score_date <= date(?, '-5 days') "
            "AND score_date >= date(?, '-8 days')",
            (latest_date, latest_date),
        ).fetchone()
        prior_date = prior_date_row[0] if prior_date_row else None

        prior_by_ticker: dict[str, float] = {}
        if prior_date:
            for t, s in conn.execute(
                "SELECT ticker, score FROM dv_score_history WHERE score_date = ?",
                (prior_date,),
            ).fetchall():
                prior_by_ticker[t] = float(s)

        lines = ["<b>🔍 DEEP VALUE</b> — top of the watchlist"]
        for ticker, score, rating, price in latest:
            price_str = f"${float(price):.2f}" if price is not None else "—"
            if ticker in prior_by_ticker:
                delta = float(score) - prior_by_ticker[ticker]
                arrow = "↑" if delta > 0.05 else "↓" if delta < -0.05 else "→"
                tag = f"({arrow} {delta:+.1f} vs last week)"
            elif prior_date:
                tag = "(new entry)"
            else:
                tag = "(first weekly snapshot)"
            lines.append(
                f"• {ticker:<5} {float(score):.1f}  {rating}  {price_str}  {tag}"
            )
        if top_n is not None and total_in_snapshot > len(latest):
            lines.append(f"<i>+ {total_in_snapshot - len(latest)} more — use /dv for full list</i>")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"[SatReview] dv section failed: {e}")
        return ""


def _format_recent_lessons(recent_lessons: list) -> str:
    """Format the most recent 2 graduated lessons for the Saturday Review
    LESSONS block. `recent_lessons` is a list of (ts, row_dict) tuples,
    pre-sorted desc by ts. Returns a multi-line string or a clean-state
    fallback when empty.

    HTML-escapes the description because real lessons (e.g. the seeded
    pe_playbook_stage5_critical row) contain raw '<' and '>' chars which
    would break Telegram's HTML parse mode and abort the whole brief."""
    if not recent_lessons:
        return "• No new graduations this week — clean state."
    top = recent_lessons[:2]
    lines = ["• Recent graduations:"]
    for _, row in top:
        desc = (row.get("description") or row.get("outcome") or "").strip()
        if not desc:
            continue
        if len(desc) > 120:
            desc = desc[:117] + "..."
        safe = desc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"  – {safe}")
    return "\n".join(lines) if len(lines) > 1 else "• No new graduations this week — clean state."


def run_saturday_review():
    """Saturday 09:00 ET — week-in-review digest sent to Telegram.

    Bypass pattern: every number below is computed deterministically from the
    DB and the circuit-breaker registry. Gemma only writes the one-line
    next-week focus. Same discipline as run_daily_briefing — facts locked,
    narrative filled.
    """
    import json
    from datetime import date, timedelta, timezone
    from circuit_breaker import list_breakers
    from lesson_producer import list_staged_candidates

    log.info("[SatReview] === Saturday week-in-review ===")
    write_log("SatReview", "Composing weekly digest", "saturday_review", "running")

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # ---------- 1. Week's paper trades ----------
        trade_rows = conn.execute(
            "SELECT pnl FROM trade_decisions "
            "WHERE datetime(timestamp) > datetime('now', '-7 days') "
            "AND status='closed' AND paper_trade=1 AND pnl IS NOT NULL"
        ).fetchall()
        pnls = [float(r["pnl"]) for r in trade_rows]
        n_trades = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = (wins / n_trades * 100) if n_trades else 0
        week_pnl_usd = sum(pnls)

        # ---------- 2. Week's scored predictions ----------
        pred_rows = conn.execute(
            "SELECT error_pct FROM predictions "
            "WHERE datetime(timestamp) > datetime('now', '-7 days') "
            "AND actual_price IS NOT NULL AND error_pct IS NOT NULL"
        ).fetchall()
        n_preds = len(pred_rows)
        avg_err = (
            sum(abs(float(r["error_pct"])) for r in pred_rows) / n_preds
            if n_preds else 0
        )

        # ---------- 3. Signals by agent ----------
        signal_rows = conn.execute(
            "SELECT agent_name, COUNT(*) AS n FROM signal_alerts "
            "WHERE datetime(timestamp) > datetime('now', '-7 days') "
            "GROUP BY agent_name ORDER BY n DESC"
        ).fetchall()
        signal_counts = [(r["agent_name"], int(r["n"])) for r in signal_rows]
        top_agent = signal_counts[0] if signal_counts else None

        # ---------- 3b. Accuracy leader (for next-week focus, NOT volume) ----------
        # Volume-leader → focus recommendation used to surface fade-worthy agents
        # because the worst agent is often the loudest. Pick the highest hit-rate
        # agent (n>=10 to avoid noise; the focus text below acknowledges sample
        # size). Below-coin-flip leaders trigger an honest "stand down" line
        # instead of a fake recommendation.
        try:
            acc_row = conn.execute(
                """SELECT agent_name, AVG(directional_hit) AS hit_rate, COUNT(*) AS n
                     FROM signal_scores
                    WHERE validated_at > datetime('now', '-30 days')
                      AND baseline_price != end_price
                    GROUP BY agent_name HAVING COUNT(*) >= 10
                    ORDER BY hit_rate DESC LIMIT 1"""
            ).fetchone()
        except sqlite3.OperationalError:
            acc_row = None
        accuracy_leader = (acc_row["agent_name"], float(acc_row["hit_rate"]), int(acc_row["n"])) if acc_row else None

        # ---------- 4. Agent freshness (last run in past 24h, active-window only) ----------
        recency_rows = conn.execute(
            "SELECT agent_name, MAX(timestamp) AS last_ts FROM agent_logs "
            "WHERE datetime(timestamp) > datetime('now', '-3 days') "
            "GROUP BY agent_name"
        ).fetchall()
        stale_agents = [
            r["agent_name"] for r in recency_rows
            if r["last_ts"] and (
                datetime.now(ET) - datetime.fromisoformat(r["last_ts"]).replace(tzinfo=ET)
            ).total_seconds() > 24 * 3600
        ]

        # ---------- 5. DV deep-value scores (rendered while conn is open) ----------
        # Cap to top 15 — 45-ticker lists are unreadable in Telegram. Full list
        # remains available via /dv.
        dv_section = _compose_dv_section(conn, top_n=15)

        conn.close()

        # ---------- 5. Lesson candidates ----------
        try:
            candidates = list_staged_candidates()
        except Exception as e:
            log.warning(f"[SatReview] candidate read failed: {e}")
            candidates = []
        n_candidates = len(candidates)

        # ---------- 5b. Recent graduated lessons (last 7 days) ----------
        # Surfaces what lesson_producer actually shipped so the user sees the
        # learning loop's output, not just the candidate count. Soft-fails to
        # empty list — never blocks the brief.
        recent_lessons = []
        try:
            from lesson_producer import LESSONS_PATH
            if os.path.exists(LESSONS_PATH):
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
                with open(LESSONS_PATH) as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                            grad_at = row.get("graduated_at")
                            if not grad_at:
                                continue
                            ts = datetime.fromisoformat(grad_at.replace("Z", "+00:00"))
                            # Existing seeded rows have naive timestamps; treat
                            # those as UTC so the comparison with `cutoff` (aware)
                            # doesn't raise TypeError and abort the whole scan.
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            if ts >= cutoff:
                                recent_lessons.append((ts, row))
                        except (json.JSONDecodeError, ValueError):
                            continue
                recent_lessons.sort(key=lambda x: x[0], reverse=True)
        except Exception as e:
            log.warning(f"[SatReview] lessons read failed: {e}")

        # ---------- 7. System health ----------
        breakers = list_breakers()
        open_breakers = [name for name, b in breakers.items() if b["state"] != "closed"]

        # ---------- 8. Next-week focus, deterministic from accuracy leader ----------
        # Replaced the LLM call: this line is too important to risk Gemma
        # hallucinating a recommendation, and the accuracy data tells us
        # what to say. Honest bands: strong (>=60%), edge (>=50%), or stand
        # down (no agent above coin flip). Confluence fallback when no data.
        if not accuracy_leader:
            focus_txt = "Watch for confluence between Pattern and Futurist on the open — no 30d accuracy data yet."
        else:
            leader_name, leader_hit, leader_n = accuracy_leader
            sample_chip = f"n={leader_n}{' — small sample' if leader_n < 20 else ''}"
            if leader_hit >= 0.60:
                focus_txt = f"Strong lead: {leader_name} at {leader_hit:.0%} 30d hit rate ({sample_chip}). Build setups around it."
            elif leader_hit >= 0.50:
                focus_txt = f"Edge: {leader_name} at {leader_hit:.0%} ({sample_chip}) — small but positive. Look for confluence."
            else:
                focus_txt = (
                    f"No agent above coin flip (best is {leader_name} at {leader_hit:.0%}). "
                    "Stand down on agent signals; lean on price + structure."
                )

        # ---------- 9. Compose the Telegram message ----------
        week_start = (date.today() - timedelta(days=6)).isoformat()
        week_end = date.today().isoformat()

        # Paper-trade open/close stats are private — see _standup for the
        # owner-only view. The Saturday review is signals-focused.
        preds_line = (
            f"• {n_preds} predictions scored, avg error {avg_err:.1f}%"
            if n_preds else "• No predictions scored this week."
        )

        top_line = (
            f"• Top signal generator: {top_agent[0]} ({top_agent[1]} signals)"
            if top_agent else "• No agents emitted signals this week."
        )

        n_signals_total = sum(n for _, n in signal_counts) if signal_counts else 0
        signals_line = (
            f"• {n_signals_total} signals emitted across {len(signal_counts)} agent(s)"
            if signal_counts else "• No signals emitted this week."
        )

        system_line = (
            f"All circuit breakers closed."
            if not open_breakers
            else f"⚠️ Open breakers: {', '.join(open_breakers)}."
        )
        if stale_agents:
            system_line += f" Stale (>24h): {', '.join(stale_agents)}."

        # Render DV section only if we got non-empty content; keeps the
        # brief clean on first-deploy boots before the daily DV cron has
        # populated dv_score_history.
        dv_block = f"{dv_section}\n\n" if dv_section else ""

        brief = (
            f"📅 <b>SATURDAY REVIEW</b> — week of {week_start} to {week_end}\n\n"
            f"📊 <b>THIS WEEK</b>\n{signals_line}\n{top_line}\n{preds_line}\n\n"
            f"{dv_block}"
            f"📚 <b>LESSONS</b>\n"
            f"• {n_candidates} candidate{'' if n_candidates == 1 else 's'} pending review (/candidates)\n"
            f"{_format_recent_lessons(recent_lessons)}\n"
            f"🔧 <b>SYSTEM</b>\n{system_line}\n\n"
            f"🔮 <b>NEXT WEEK</b>\n{focus_txt}"
        )

        write_log("SatReview", brief[:2000], "saturday_review")
        from notifier import notify
        notify(brief)
        # Internal log retains paper-trade stats for the operator's eyes
        # (orchestrator log only — never sent to Telegram).
        log.info(
            f"[SatReview] sent — signals={n_signals_total} preds={n_preds} "
            f"candidates={n_candidates} (private: trades={n_trades} "
            f"win_rate={win_rate:.0f}% pnl=${week_pnl_usd:+.0f})"
        )
    except Exception as e:
        log.error(f"[SatReview] {e}")
        write_log("SatReview", str(e), "saturday_review", "error")


def run_options_update():
    """Monday 8:30 AM ET — fetch options chain and compute max pain for the week."""
    log.info("[Options] Computing weekly max pain...")
    try:
        from options_feed import OptionsFeed, ensure_options_table
        ensure_options_table()
        feed = OptionsFeed()
        feed.update_db(send_telegram=True)

        # Realized-vol context first so the brief can reference the regime.
        vol_forecast = None
        try:
            from volatility_forecast import forecast_next_abs_return

            vol_forecast = forecast_next_abs_return(DB_PATH)
            write_log("Options", vol_forecast.summary(), "realized_vol_forecast", "ok" if vol_forecast.ok else "warn")
        except Exception as vol_err:
            log.warning("[Options] Realized-vol baseline failed: %s", vol_err)
            write_log("Options", str(vol_err), "realized_vol_forecast", "warn")

        try:
            from options_feed import save_watchlist_snapshot, load_previous_watchlist
            from options_brief import (
                persona_label, compute_wow_diff, gone_strikes, shares_translation,
            )
            from notifier import notify_options_brief
            from datetime import date

            call_watchlist = feed.call_contract_candidates(n=5)
            candidates = call_watchlist.get("candidates", []) if call_watchlist else []
            if not candidates:
                write_log("Options", "No liquid call contract candidates passed filters", "call_contract_watchlist")
            else:
                expiry = call_watchlist.get("expiration", "")
                spot = float(call_watchlist.get("current_price", 0.0))
                today_iso = date.today().isoformat()

                previous = load_previous_watchlist(expiry, today_iso)
                wow = compute_wow_diff(candidates, previous)
                gone = gone_strikes(candidates, previous)
                personas = [persona_label(c, candidates) for c in candidates]
                vol_regime = vol_forecast.regime if vol_forecast and vol_forecast.ok else ""
                takeaway = shares_translation(candidates, vol_regime=vol_regime)

                log_lines = [
                    f"{c['contract_symbol'] or 'call'} strike ${c['strike']:.2f}: "
                    f"score {c['score']:.1f}, bid/ask ${c['bid']:.2f}/${c['ask']:.2f}, "
                    f"vol {c['volume']}, OI {c['open_interest']}, IV {c['iv']:.0%}, BE(mid) ${c['breakeven_mid']:.2f}"
                    for c in candidates
                ]
                write_log(
                    "Options",
                    "Call contract watchlist (not an execution recommendation):\n" + "\n".join(log_lines),
                    "call_contract_watchlist",
                )

                notify_options_brief(
                    expiration=expiry,
                    spot_price=spot,
                    candidates=candidates[:3],
                    candidate_personas=personas[:3],
                    wow_diff=wow,
                    gone=gone,
                    vol_predicted_pct=vol_forecast.predicted_abs_move_pct if vol_forecast and vol_forecast.ok else None,
                    vol_long_term_pct=vol_forecast.long_term_abs_move_pct if vol_forecast and vol_forecast.ok else None,
                    vol_regime=vol_regime,
                    shares_takeaway=takeaway,
                )

                save_watchlist_snapshot(call_watchlist, snapshot_date=today_iso)
        except Exception as wl_err:
            log.warning("[Options] Call contract watchlist failed: %s", wl_err)
            write_log("Options", str(wl_err), "call_contract_watchlist", "warn")
    except Exception as e:
        log.error(f"[Options] Max pain update failed: {e}")
        write_log("Options", str(e), "max_pain", "error")


def run_fundamentals_update():
    """Daily 08:35 ET (Mon-Fri) — refresh yfinance fundamentals + earnings date
    feeding the local OBS dashboard panel (/obs/stats)."""
    log.info("[Fundamentals] Refreshing OBS panel snapshot...")
    try:
        from fundamentals_feed import FundamentalsFeed
        ok = FundamentalsFeed().update_db()
        if not ok:
            write_log("Fundamentals", "yfinance returned no fields — skipped write", "fundamentals", "warn")
    except Exception as e:
        log.error(f"[Fundamentals] Update failed: {e}")
        write_log("Fundamentals", str(e), "fundamentals", "error")


def run_monday_weekend_digest():
    """Monday 08:00 ET — pre-open digest of weekend news + gap risk.

    The 09:00 huddle doesn't currently surface weekend news in time — by the
    open the team has had no chance to read it. This brief lands an hour
    before the bell with what broke since Friday's close.

    Bypass pattern: news headlines and GeoRisk logs are pulled raw, the gap
    estimate is arithmetic, Gemma only fills the one-line 'what to watch'
    closer. Falls back to a hardcoded line if Gemma is unavailable.
    """
    log.info("[MondayDigest] === Pre-open weekend digest ===")
    write_log("MondayDigest", "Composing weekend digest", "monday_digest", "running")

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # ---------- 1. News since Friday's close (~3 days window for Mon morning) ----------
        news_rows = conn.execute(
            "SELECT headline, source, sentiment_score, sentiment_label, summary "
            "FROM news_analysis "
            "WHERE datetime(timestamp) > datetime('now', '-3 days') "
            "ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        n_news = len(news_rows)

        # ---------- 2. GeoRisk over the weekend ----------
        geo_rows = conn.execute(
            "SELECT content, status FROM agent_logs "
            "WHERE task_type='georisk' "
            "AND datetime(timestamp) > datetime('now', '-3 days') "
            "ORDER BY timestamp DESC LIMIT 3"
        ).fetchall()
        latest_geo = geo_rows[0]["content"][:200] if geo_rows else None

        # ---------- 3. Gap risk — pre-market tick vs Friday close ----------
        last_tick = conn.execute(
            "SELECT close, timestamp FROM price_ticks WHERE symbol='GME' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        fri_close_row = conn.execute(
            "SELECT close FROM daily_candles WHERE symbol='GME' "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        conn.close()

        if last_tick and fri_close_row and float(fri_close_row["close"]):
            current = float(last_tick["close"])
            fri_close = float(fri_close_row["close"])
            gap_pct = ((current - fri_close) / fri_close) * 100
            if gap_pct > 0.5:
                gap_direction = "gap up"
            elif gap_pct < -0.5:
                gap_direction = "gap down"
            else:
                gap_direction = "flat open"
            gap_line = (
                f"GME ${current:.2f} vs Fri close ${fri_close:.2f} "
                f"({gap_pct:+.1f}%) — {gap_direction}"
            )
        else:
            gap_line = "Gap risk: pre-market quote unavailable."

        # ---------- 4. Top news headlines (deterministic — no Gemma rewriting) ----------
        if news_rows:
            # Color emoji leads the row so direction scans before headline.
            # Replaces the bracketed [neutral]/[bullish]/[bearish] tags
            # that buried the signal behind text the reader had to parse.
            _sentiment_emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
            headlines = []
            for r in news_rows[:5]:
                hl = (r["headline"] or "")[:120]
                label = (r["sentiment_label"] or "neutral").lower()
                emoji = _sentiment_emoji.get(label, "⚪")
                headlines.append(f"• {emoji} {hl}")
            news_block = "\n".join(headlines)
        else:
            news_block = "• No news flagged since Friday close."

        # ---------- 5. Gemma narrative for "what to watch" ----------
        prompt = (
            "You are writing the closing line of a Monday pre-open weekend "
            "digest for a GME trading crew. Output ONE short sentence on what "
            "to watch at the open given these facts. No preamble, no markdown, "
            "no quotes, under 140 chars.\n\n"
            f"- News items since Fri close: {n_news}\n"
            f"- Gap setup: {gap_line}\n"
            f"- GeoRisk note: {'present' if latest_geo else 'none'}\n"
        )
        watch_txt = "Watch the first 30-min range — fade extremes, follow continuation."
        try:
            from llm_config import llm_generate
            raw = llm_generate(prompt, num_predict=80, temperature=0.4, timeout=30)
            raw = " ".join((raw or "").strip().strip('"').strip("'").split())
            if raw:
                watch_txt = raw[:160]
        except Exception as e:
            log.warning(f"[MondayDigest] LLM watch line failed ({e}), using fallback")

        # ---------- 6. Compose ----------
        geo_section = (
            f"\n\n<b>GEORISK</b>\n{latest_geo}" if latest_geo else ""
        )
        brief = (
            f"🌅 <b>MONDAY WEEKEND DIGEST</b>\n\n"
            f"<b>GAP SETUP</b>\n{gap_line}\n\n"
            f"<b>WEEKEND HEADLINES</b> ({n_news} item{'' if n_news == 1 else 's'})\n"
            f"{news_block}"
            f"{geo_section}\n\n"
            f"<b>WATCH</b>\n{watch_txt}"
        )

        write_log("MondayDigest", brief[:2000], "monday_digest")
        from notifier import notify
        notify(brief)
        log.info(f"[MondayDigest] sent — news={n_news} gap={gap_line[:60]}")
    except Exception as e:
        log.error(f"[MondayDigest] {e}")
        write_log("MondayDigest", str(e), "monday_digest", "error")


@market_hours_required
def run_social_scan():
    """Every 15 min during market hours — scan Twitter/X for key account posts.

    Distinguish three outcomes so the log doesn't lie:
      - posts found → log each one
      - 0 posts AND every backend failed → status='error' so future me can spot
        the silent-rot pattern that produced "no new posts" forever
      - 0 posts AND ≥1 backend succeeded → genuine quiet day, status='ok'
    """
    write_log("Social", "Scanning tracked accounts", "social", "running")
    try:
        from twitter_monitor import TwitterMonitor, TRACKED_ACCOUNTS
        monitor = TwitterMonitor()
        results = monitor.scan_all()
        scan_stats = monitor.last_scan_stats  # {'tried': N, 'failed': M, 'posts': K}

        if results:
            log.info(f"[Social] {len(results)} new posts found")
            for r in results:
                write_log("Social", f"@{r['username']} [{r['signal_type']}]: {r['text'][:200]}",
                          "social")
        elif scan_stats.get("failed", 0) >= scan_stats.get("tried", len(TRACKED_ACCOUNTS)):
            # Every single backend call failed — that's not "no news", that's
            # a broken pipe. Flag it so it shows up in /freshness instead of
            # silently rotting (this is exactly how social_posts ended up at
            # 0 rows ever — Supabase Edge timed out and we logged 'no posts').
            write_log("Social",
                      f"All {scan_stats['failed']}/{scan_stats['tried']} backend "
                      "calls failed — Twitter/Nitter unreachable",
                      "social", "error")
        else:
            write_log("Social",
                      f"Scanned {scan_stats.get('tried', len(TRACKED_ACCOUNTS))} accounts — "
                      f"no new posts ({scan_stats.get('failed', 0)} backend failures)",
                      "social")
    except Exception as e:
        log.error(f"[Social] Scan failed: {e}")
        write_log("Social", str(e)[:300], "social", "error")


def run_cto_dv_score(tickers: list[str] | None = None):
    """CTO — DV (deep-value) score with delta vs previous run.

    Writes one formatted score card per ticker to agent_logs
    (task_type='dv_score', agent='CTO'); each row produces its own Telegram
    burst via the voice forwarder. LLM interpretation runs only for GME
    (the GME turnaround thesis is hard-coded into the prompt). Other
    tickers get the deterministic numerical block + short-vol + venue mix.

    Args:
        tickers: list of symbols to score. Defaults to ["GME"] for the
            9:10 ET cron. /dvburst passes user-supplied lists like
            ["GME", "EBAY"]; each row writes sequentially so the
            Telegram bursts stack one after another.
    """
    if tickers is None:
        tickers = ["GME"]
    tickers = [t.upper() for t in tickers if t and t.strip()]
    if not tickers:
        return
    log.info(f"[CTO] Running DV score for {', '.join(tickers)}")
    for ticker in tickers:
        _run_cto_dv_score_for(ticker)


def _run_cto_dv_score_for(ticker: str):
    """Per-ticker DV brief. Extracted so run_cto_dv_score can loop without
    duplicating the ~150 lines of scoring + formatting."""
    write_log("CTO", f"Computing DV deep-value score for {ticker}", "dv_score", "running")
    try:
        from dv_score import fetch, score as dv_score_fn

        inp = fetch(ticker)
        if inp is None:
            write_log("CTO", f"dv_score.fetch({ticker}) returned None (data source down?)",
                      "dv_score", "error")
            return
        r = dv_score_fn(inp)
        total = r["total"]
        rating = r["rating"]
        A = r["pillars"]["A"]
        B = r["pillars"]["B"]
        C = r["pillars"]["C"]
        D = r["pillars"]["D"]
        ins_count = inp.insider_buy_count
        ins_dollars = inp.insider_buy_dollars
        ins_str = (f"${ins_dollars/1e6:.1f}M" if ins_dollars >= 1e6
                   else (f"${ins_dollars/1e3:.0f}K" if ins_dollars > 0 else "$0"))
        imm = r["immunity"]
        imm_count = r["immunity_count"]
        net_cash_pct = (inp.cash_mm - inp.total_debt_mm) / inp.market_cap_mm if inp.market_cap_mm else 0

        # Look up previous run for delta — ticker-keyed so multi-ticker
        # bursts don't bleed prior scores across symbols.
        conn = sqlite3.connect(DB_PATH)
        prev_row = conn.execute(
            "SELECT content FROM agent_logs WHERE agent_name='CTO' "
            "AND task_type='dv_score' AND status='ok' "
            "AND content LIKE ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (f"{ticker} DV Score%",),
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
        # Other tickers skip the LLM (the prompt is GME-specific and the deterministic
        # numerics + venue mix already carry the load).
        interpretation = ""
        if ticker == "GME":
            prompt = (
                "You are the CTO — a deep-value analyst covering GME. Write ONE paragraph "
                "(max 350 chars, no preamble, no markdown, no quotes) interpreting today's "
                "DV (deep-value) score through the turnaround lens below.\n\n"
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
                f"Pillars — Valuation: {A:.1f}/25 · Capital: {B:.1f}/40 · Quality: {C:.1f}/20 · "
                f"Insider Conviction: {D:.1f}/15\n"
                f"Inputs: EV/FCF {inp.ev_fcf:.1f} · EV/EBITDA {inp.ev_ebitda:.1f} · P/B {inp.pb:.2f} · "
                f"Altman Z {inp.altman_z:.1f} · D/E {inp.debt_equity:.2f} · "
                f"Net Cash {net_cash_pct*100:.1f}% of MCap · Op Margin {inp.operating_margin*100:.1f}% · "
                f"ROE {inp.roe*100:.1f}% · Net Margin {inp.net_margin*100:.1f}%\n"
                f"Insider open-market buys (dir/officer, last 3y): {ins_count} purchases / {ins_str} total"
            )
            try:
                from llm_config import llm_generate
                interpretation = llm_generate(prompt, num_predict=200, temperature=0.4, timeout=30)
                interpretation = interpretation.strip().strip('"').strip("'")
                interpretation = " ".join(interpretation.split())[:600]
            except Exception as e:
                log.warning(f"[CTO] DV interpretation LLM failed: {e}")

        imm_line = (
            f"{'✓' if imm['debt_free'] else '✗'} Debt-free · "
            f"{'✓' if imm['cash_over_1b'] else '✗'} Cash>$1B · "
            f"{'✓' if imm['net_cash_positive'] else '✗'} Net Cash+ · "
            f"{'✓' if imm['profitable'] else '✗'} Profitable · "
            f"{'✓' if imm['altman_safe'] else '✗'} Altman Safe"
        )
        # Short-vol intel from FINRA Reg SHO daily feed. Independent of the
        # DV pillar math (rubric stays untouched) — adds a one-line context
        # tag to the brief so the burst surfaces today's short pressure vs.
        # the 30-day baseline. Soft-fails to nothing on FINRA outage.
        short_vol_line = ""
        try:
            from finra_short_vol import get_short_vol_summary, format_brief_line
            sv = get_short_vol_summary(ticker)
            if sv:
                short_vol_line = format_brief_line(sv, ticker=ticker)
        except Exception as e:
            log.warning(f"[CTO] FINRA short-vol fetch failed: {e}")

        # Venue-mix intel from Polygon trades (per-MIC daily aggregation).
        # Shows how much volume cleared off-exchange (DARK POOL = TRF/ADF
        # prints) vs. lit venues. Soft-fails to nothing without
        # POLYGON_API_KEY or on Polygon outage.
        venue_line = ""
        try:
            from exchange_volume import (
                get_exchange_volume_summary,
                format_brief_line as ex_brief,
            )
            ev = get_exchange_volume_summary(ticker)
            if ev:
                venue_line = ex_brief(ev, ticker=ticker)
        except Exception as e:
            log.warning(f"[CTO] Exchange-volume fetch failed: {e}")

        # Fails-to-Deliver intel from SEC bi-weekly publication. ~30-day
        # publication lag is the data, not a bug — context for whether
        # recent settlement cycles cleared cleanly. Soft-fails to
        # nothing on SEC outage or pre-publication windows.
        ftd_line = ""
        try:
            from sec_ftd import get_ftd_summary, format_brief_line as ftd_brief
            ftd = get_ftd_summary(ticker)
            if ftd:
                ftd_line = ftd_brief(ftd, ticker=ticker)
        except Exception as e:
            log.warning(f"[CTO] SEC FTD fetch failed: {e}")

        brief = (
            f"{ticker} DV Score: {total:.1f}/100 {rating} {delta_str}\n"
            f"Pillars — Valuation {A:.1f}/25 · Capital {B:.1f}/40 · Quality {C:.1f}/20 · Insider {D:.1f}/15\n"
            f"Insider 3y buys: {ins_count} purchases / {ins_str}\n"
            f"Immunity {imm_count}/5: {imm_line}\n"
            f"Inputs — EV/FCF {inp.ev_fcf:.1f} · EV/EBITDA {inp.ev_ebitda:.1f} · P/B {inp.pb:.2f} · "
            f"Altman Z {inp.altman_z:.1f} · D/E {inp.debt_equity:.2f} · Net Cash {net_cash_pct*100:.1f}% · "
            f"OpMgn {inp.operating_margin*100:.1f}% · ROE {inp.roe*100:.1f}% · NetMgn {inp.net_margin*100:.1f}%"
        )
        if short_vol_line:
            brief += f"\n{short_vol_line}"
        if venue_line:
            brief += f"\n{venue_line}"
        if ftd_line:
            brief += f"\n{ftd_line}"
        if interpretation:
            brief += f"\nREAD: {interpretation}"

        log.info(f"[CTO] {ticker} DV: {total:.1f}/100 {delta_str}")
        write_log("CTO", brief, "dv_score", "ok")
    except Exception as e:
        log.error(f"[CTO] {ticker} DV score failed: {e}")
        write_log("CTO", f"{ticker}: {e}", "dv_score", "error")


def run_dv_history_log():
    """9:15 AM ET — log EVERY watchlist ticker to dv_score_history.

    Previously gated at score >= 65 (the 'investment-grade deep value' bar).
    Now logs everyone so the Saturday review can show the full ranking,
    not just companies that already cleared the bar. Database growth is
    negligible (~40 rows/day × 365 = ~15k rows/year, UNIQUE-constrained).
    """
    log.info("[dv_history] daily log run")
    try:
        from dv_history import log_daily_scores
        result = log_daily_scores(threshold=0.0)
        write_log("CTO", f"dv_history log: {result}", "dv_history", "ok")
    except Exception as e:
        log.error(f"[dv_history] log failed: {e}")
        write_log("CTO", str(e), "dv_history", "error")


def run_dv_history_resolve():
    """3:30 AM ET — resolve forward returns for any rows whose
    30/90/365-day anniversary has passed."""
    log.info("[dv_history] daily resolve run")
    try:
        from dv_history import resolve_forward_returns
        result = resolve_forward_returns()
        write_log("CTO", f"dv_history resolve: {result}", "dv_history", "ok")
    except Exception as e:
        log.error(f"[dv_history] resolve failed: {e}")
        write_log("CTO", str(e), "dv_history", "error")


def run_cto_daily_brief():
    """9:05 AM ET — CTO structural intelligence brief, just after morning huddle.

    Bypass pattern: pulls structural_signals, investor_intel, and news_analysis
    rows deterministically. Gemma only writes a short commentary on the
    short-watchlist and anti-pattern flags. GME immunity math is handled
    separately by run_cto_dv_score at 9:10.
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
            from llm_config import llm_generate
            raw = llm_generate(prompt, num_predict=160, temperature=0.3, timeout=30)
            raw = " ".join(raw.strip().strip('"').strip("'").split())
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
    """Daily 08:00 ET — EDGAR scan + agent narrative.

    Was Sundays-only (id='cto_scan' in configure_schedule), but a missed Sunday
    meant a 7-day blind spot for new 8-K triggers (CRO hires, sale-leasebacks,
    benefit cuts). Going daily with days_back=2 gives 1-day overlap so a
    single missed run never costs more than a day.
    """
    from agents import cto_agent
    from tasks import cto_structural_scan_task
    log.info("[CTO] === Daily structural scan ===")
    try:
        # Run live EDGAR scan before the agent brief. days_back=2 = today +
        # yesterday, with overlap so a missed run is recoverable next day.
        from sec_scanner import SECScanner
        scanner = SECScanner()
        scanner.scan_watchlist(days_back=2)
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

        if not per_agent:
            conn.close()
            write_log("Synthesis", "no recent agent logs in last 4h", "synthesis", "error")
            return

        conn.close()
        # Dedup is enforced AFTER the LLM call, on the structured fields of the
        # brief (consensus_dir, structural_status, trend_dir, signal_action,
        # price_bucket). Input-side fingerprinting tried to dedup before the LLM
        # call but missed too many "same-meaning, different wording" cycles, so
        # the channel saw 4+ identical briefs in a row. See post-LLM block below.

        logs_block = "\n".join(f"  {name}: {content}" for name, content in per_agent.items())
        # PRICE token is pre-formatted from market fact — LLM must use it verbatim.
        # Includes the day-on-day pct so 'falling' becomes quantitative ('-0.4%').
        pct = fact.get('pct_change', 0.0) or 0.0
        arrow = "🔻" if pct < -0.5 else ("🔺" if pct > 0.5 else "↔")
        price_token = f"${fact['price']:.2f} {arrow} {pct:+.2f}%"
        # Volume regime as a comparative anchor for the LLM (rec #6)
        try:
            vol_conn = sqlite3.connect(DB_PATH)
            vol_info = _volume_regime(vol_conn, "GME")
            vol_conn.close()
            vol_anchor = f"vol {vol_info['label']} ({vol_info['ratio']:.2f}x avg)"
        except Exception:
            vol_anchor = "vol n/a"
        # Closed vocabulary for parenthetical reasons — prevents hallucinated
        # "(earnings beat)"-style fabrications. The LLM must pick one of these.
        suffix_vocab = (
            "DATA suffixes: (no gaps) | (1 feed down) | (2+ feeds down) | (stale ticks) | (recent gap)\n"
            "  STRUCTURAL suffixes: (cash-rich) | (consolidating) | (debt-heavy) | (insider-selling) | (filings-quiet)\n"
            "  NEWS suffixes: (no catalysts) | (earnings event) | (regulatory headline) | (acquisition headline) | "
            "(insider news) | (macro headline) | (analyst action)"
        )
        prompt = (
            "You are the team's Synthesis agent. Produce a THREE-LINE consensus brief that a "
            "retail investor can act on. Use plain English. Format (replace bracketed values):\n"
            f"NOW: PRICE: {price_token} | DATA: [clean/degraded] ([reason]) | "
            "NEWS: [bullish/bearish/neutral] [score -1.0 to 1.0] ([reason]) | STRUCTURAL: [GREEN/CAUTION/RED] ([reason])\n"
            "NEXT: CONSENSUS: [BULLISH/BEARISH/NEUTRAL] [XX]% (X/Y agents; TopAgent NN%) | "
            "TREND: [UP/DOWN/SIDEWAYS] [0.0-1.0] | PREDICTION: [BIAS] [confidence 0.0-1.0]\n"
            "SIGNAL: [BUY/SELL/HOLD/WAIT] [optional entry/stop/target for BUY/SELL only] — [reason]\n\n"
            "  - BUY format:  SIGNAL: BUY @ $[entry] (stop $[stop], target $[target]) — [reason]\n"
            "  - SELL format: SIGNAL: SELL @ $[entry] (stop $[stop], target $[target]) — [reason]\n"
            "  - HOLD format: SIGNAL: HOLD — [reason]  (no price fields)\n"
            "  - WAIT format: SIGNAL: WAIT — [reason]  (no price fields)\n\n"
            "Rules:\n"
            "  * Labels UPPERCASE. Directional values (BULLISH/BEARISH/NEUTRAL/UP/DOWN/SIDEWAYS/GREEN/CAUTION/RED/BUY/SELL/HOLD/WAIT) UPPERCASE.\n"
            "  * Use ONLY the data below, do not invent. If an agent is missing, write 'n/a'.\n"
            f"  * Use the PRICE token EXACTLY as given: '{price_token}' — do NOT change the direction.\n"
            "  * Consensus pct = share of non-n/a agents agreeing with the direction. CAP AT 95% — never write 100%.\n"
            "    Always include '(X/Y agents; TopAgent NN%)'. Example: 'CONSENSUS: BEARISH 67% (4/6 agents; Futurist 78%)'.\n"
            "  * TREND strength must be a NUMBER 0.0-1.0 (e.g. 'TREND: UP 0.7'). Do not write 'strong'/'weak'.\n"
            "  * NEWS score is a SIGNED DECIMAL between -1.0 (very bearish) and +1.0 (very bullish). NOT a percentage.\n"
            "    Example: 'NEWS: bullish 0.75 (analyst action)'. Never 'NEWS: BULLISH 75%'.\n"
            "  * Parenthetical reasons MUST come from this closed vocabulary — do not invent:\n"
            f"  {suffix_vocab}\n"
            "  * STRUCTURAL value MUST be exactly GREEN, CAUTION, or RED — never a reason word.\n"
            "    Correct:   'STRUCTURAL: CAUTION (consolidating)'\n"
            "    Wrong:     'STRUCTURAL: consolidating (filings-quiet)'  ← status missing\n"
            "    Wrong:     'STRUCTURAL: filings-quiet (filings-quiet)'   ← status missing, suffix duplicated\n"
            "  * SIGNAL line: BUY if CONSENSUS bullish and PREDICTION confidence>=0.65. SELL if same threshold bearish.\n"
            "    HOLD if you already have a position and conviction is mid (0.5-0.65). WAIT otherwise.\n"
            "    Entry = current price for BUY/SELL. OMIT entry/stop/target for HOLD/WAIT — they are noise.\n"
            "    Stop = -3% from entry for BUY, +3% for SELL. Target = +6% for BUY, -6% for SELL.\n"
            "    Reason: <=15 words, plain English, no jargon. NEVER restate the action word ('WAIT for a clearer\n"
            "    trend' is wrong — the reader already saw WAIT). Describe the WHY, not the WHAT.\n"
            "  * NOW = current observations. NEXT = forecast call. SIGNAL = actionable trade.\n"
            "  * Exactly three lines, each starting with NOW:, NEXT:, SIGNAL:. No blank lines between.\n"
            "  * No preamble, no markdown, no quotes, no emoji.\n\n"
            f"{fact['prompt_line']}\n"
            f"Current volume regime: {vol_anchor}\n"
            f"Recent per-agent outputs (last 4h):\n{logs_block}\n"
        )

        from llm_config import llm_generate
        brief = llm_generate(prompt, num_predict=320, temperature=0.2, timeout=60)
        brief = brief.strip().strip('"').strip("'")
        # Keep up to the first three non-empty lines (NOW: / NEXT: / SIGNAL:).
        # Drop trailing commentary the LLM sometimes appends.
        lines = [ln.strip() for ln in brief.split("\n") if ln.strip()][:3]
        brief = "\n".join(lines)[:700]

        if not brief or "PRICE" not in brief.upper():
            write_log("Synthesis", f"malformed brief: {brief[:200]}", "synthesis", "error")
            return

        # Safety net: force correct PRICE token even if LLM drifted.
        # Non-greedy match up to (but not including) trailing whitespace before
        # a pipe or end-of-line, so the existing ' | ' separator stays intact
        # instead of getting consumed and producing 'PRICE: ...%| DATA: ...'.
        import re
        brief = re.sub(
            r'PRICE:\s*\$[\d.]+[^|\n]*?(?=\s*(?:[|\n]|$))',
            f'PRICE: {price_token}',
            brief,
            count=1,
            flags=re.IGNORECASE,
        )

        # Post-LLM normalization: capitalize, trim prose, coerce trend strength
        # and NEWS score, cap consensus at 95% to prevent overclaim.
        from message_formatters import (
            clamp_consensus_pct,
            coerce_news_score,
            coerce_trend_strength,
            normalize_synthesis_capitalization,
            tighten_prose,
        )
        brief = normalize_synthesis_capitalization(brief)
        brief = coerce_trend_strength(brief)
        brief = coerce_news_score(brief)
        brief = clamp_consensus_pct(brief)
        brief = tighten_prose(brief)

        # Output-side dedup: build a coarse state_key from the structured
        # fields of the brief. If the call (consensus direction + structural
        # status + signal action + rounded price) hasn't changed since last
        # cycle, suppress the message — only emit on material shifts.
        from episodic_integration import extract_synthesis_from_output
        parsed = extract_synthesis_from_output(brief)
        signal_action = "n/a"
        m = re.search(r"SIGNAL:\s*(\w+)", brief, flags=re.IGNORECASE)
        if m:
            signal_action = m.group(1).upper()
        if parsed:
            state_key = (
                f"{parsed.consensus or 'n/a'}|"
                f"{parsed.structural_status or 'n/a'}|"
                f"{parsed.trend_direction or 'n/a'}|"
                f"{signal_action}|"
                f"{round(float(fact['price']), 1)}"
            )
        else:
            # Parse failure — fall back to round-trip on brief content so we
            # don't silence everything, but coarse enough to catch identical text.
            import hashlib
            state_key = "unparsed|" + hashlib.md5(brief.encode("utf-8")).hexdigest()[:12]

        conn = sqlite3.connect(DB_PATH)
        last_state = conn.execute(
            "SELECT content FROM agent_logs WHERE agent_name='Synthesis' "
            "AND task_type='synthesis_state' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if last_state and last_state[0] == state_key:
            log.info(f"[Synthesis] state unchanged ({state_key}) — skipping emit")
            write_log("Synthesis", "state unchanged; no new brief", "synthesis", "skipped")
            return

        log.info(f"[Synthesis] {brief}")
        write_log("Synthesis", brief, "synthesis")
        # Record new state_key for the next cycle's dedup check
        write_log("Synthesis", state_key, "synthesis_state")

        # Append to episodic memory — only on material shifts (dedup above
        # already short-circuited unchanged states). Bounded growth: ~5-20
        # distinct emits/day. Soft-fail so logging never breaks the cycle.
        if parsed is not None:
            try:
                from episodic_integration import log_synthesis
                log_synthesis(
                    price=parsed.price,
                    data_quality=parsed.data_quality,
                    news_sentiment=parsed.news_sentiment,
                    pattern_type=parsed.pattern_type,
                    trend_direction=parsed.trend_direction,
                    trend_strength=parsed.trend_strength,
                    prediction_bias=parsed.prediction_bias,
                    prediction_confidence=parsed.prediction_confidence,
                    structural_status=parsed.structural_status,
                    consensus=parsed.consensus,
                    consensus_pct=parsed.consensus_pct,
                )
            except Exception as e:
                log.warning(f"[Synthesis] episodic log failed: {e}")
    except requests.Timeout:
        log.error("[Synthesis] LLM timeout")
        write_log("Synthesis", "LLM timeout after 45s", "synthesis", "timeout")
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
    from llm_config import llm_generate_grounded
    brief = llm_generate_grounded(prompt, num_predict=120, temperature=0.3, timeout=45)
    brief = brief.strip().strip('"').strip("'").split("\n")[0].strip()[:400]
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

        # Latest options snapshot — recompute Δ against live price (snapshot Δ is stale Tue–Fri)
        options_str = ""
        opts = conn.execute(
            "SELECT max_pain_strike, net_oi_bias, expiration "
            "FROM options_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if opts and opts['max_pain_strike']:
            delta = (price - opts['max_pain_strike']) if price else 0.0
            sign = '+' if delta >= 0 else ''
            options_str = (
                f"max pain ${opts['max_pain_strike']:.2f} "
                f"(Δ{sign}{delta:.2f}) · OI: {opts['net_oi_bias']} · exp {opts['expiration']}"
            )

        conn.close()

        notify_periodic_brief(
            price=price,
            pct_change=pct_change,
            consensus=consensus,
            top_signal=top_signal,
            geo_risk=geo_risk,
            prediction=prediction,
            options=options_str,
        )
        write_log("Briefer", "4-hour digest sent", "periodic_brief")
        log.info("[Briefer] 4-hour digest sent to Telegram")
    except Exception as e:
        log.error(f"[Briefer] {e}")
        write_log("Briefer", str(e), "periodic_brief", "error")


# Day-of-week intros for the daily strategy brief. Each tuple is
# (header_tag_shown_to_team, extra_line_added_to_Gemma_FACTS_block).
_DAY_INTROS = {
    0: (  # Monday
        "Monday — first day",
        "First trading day after the weekend; gap-risk and weekend news context apply.",
    ),
    1: (  # Tuesday
        "Tuesday — confirmation",
        "Monday's move is in the books; today tests whether it holds or reverses.",
    ),
    2: (  # Wednesday
        "Wednesday — pulse",
        "Mid-week — focus on whether this week's thesis is still intact.",
    ),
    3: (  # Thursday
        "Thursday — pre-opex",
        "Tomorrow is weekly options expiry; max-pain pressure starts building today.",
    ),
    4: (  # Friday
        "Friday — opex day",
        "Weekly options expire today; max pain and the weekly close matter.",
    ),
    5: ("Saturday", ""),  # not normally invoked; safe defaults
    6: ("Sunday", ""),
}


def _day_intro(today_date):
    """Return (header_tag, fact_line) for today's daily brief.

    First-Friday-of-the-month appends an NFP-day note.
    """
    tag, line = _DAY_INTROS.get(today_date.weekday(), ("", ""))
    if today_date.weekday() == 4 and today_date.day <= 7:
        line = (line + " First Friday — NFP at 08:30 may move the market before the open.").strip()
    return tag, line


def run_daily_briefing():
    """10:00 AM ET — ELI5 strategy briefing sent to Telegram after market opens.

    Bypass pattern: numbers and direction are computed deterministically from
    daily_candles + signal_alerts. Gemma only fills the narrative fragments
    (pattern description, waiting-for, risk). Prevents the 'sideways on an
    up day' hallucination the previous CrewAI version produced.

    Day-of-week intro tag is appended deterministically around the Gemma-filled
    core — the bypass pattern is preserved.
    """
    from datetime import date, datetime
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

        today = date.today()
        day_tag, day_line = _day_intro(today)
        day_fact_line = f"- {day_line}\n" if day_line else ""

        prompt = (
            "You are writing a plain-English trading brief for a non-technical CEO. "
            "Output EXACTLY three short labelled sections, no preamble, no markdown, "
            "no quotes, no emoji. Keep each section to 1-2 short sentences.\n\n"
            "FACTS (use these — do NOT contradict):\n"
            f"- GME is currently ${current:.2f}, {direction} ({pct_vs_prev:+.1f}% vs prior close).\n"
            f"- Today's intraday range: ${day_low:.2f} to ${day_high:.2f}.\n"
            f"- Support ${support:.2f}, resistance ${resistance:.2f}.\n"
            f"- Team confidence: {team_conf}%.\n"
            f"{day_fact_line}"
            f"\nAGENT SIGNALS (last 24h):\n{signals_blob}\n\n"
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
            from llm_config import llm_generate
            text = llm_generate(prompt, num_predict=250, temperature=0.3, timeout=30).strip()
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

        header = f"<b>📋 DAILY STRATEGY BRIEF — {day_tag}</b>" if day_tag else "<b>📋 DAILY STRATEGY BRIEF</b>"

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
        notify(f"{header}\n\n{brief}")
        log.info(f"[Briefing] Brief sent — {day_tag or 'no-tag'} {direction} {pct_vs_prev:+.1f}% conf={team_conf}%")
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
            from llm_config import llm_generate
            raw = llm_generate(prompt, num_predict=80, temperature=0.4, timeout=30)
            raw = " ".join(raw.strip().strip('"').strip("'").split())
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


def run_paper_trade_checker():
    """Every 5 min — close paper trades that hit TP/SL or expired."""
    try:
        from paper_trader import check_and_close_open_trades
        result = check_and_close_open_trades(DB_PATH)
        if result["closed"]:
            log.info(f"[PaperTrader] closed {result['closed']} trades "
                     f"(tp={result.get('tp_hits',0)} sl={result.get('sl_hits',0)} "
                     f"exp={result.get('expired',0)})")
    except Exception as e:
        log.warning(f"[PaperTrader] checker failed: {e}")


def run_standup_report():
    """Mon-Fri 11 AM & 4 PM ET — verdict-first standup to Telegram.

    Categorises agents into LISTEN (passing the bar) or MUTED (gated or no
    sample), persists today's gate decisions to agent_gate_history, and
    surfaces a one-line day-over-day status diff. Replaces the old jargon
    dump (dir%/TP%/fade/trust/n=) with plain-English buckets.
    """
    log.info("[Standup] === AGENT PERFORMANCE REPORT ===")
    try:
        import signal_gate
        from paper_trader import get_trade_stats, ensure_paper_trades_table
        from standup_brief import (
            ensure_gate_history_table, categorize_all, save_gate_snapshot,
            load_previous_gate_snapshot, compute_status_diff,
        )
        from notifier import format_standup_brief
        from datetime import date

        ensure_gate_history_table(DB_PATH)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        ensure_paper_trades_table(conn)

        pt_stats = get_trade_stats(conn, days=1)

        acc_rows = conn.execute(
            """
            SELECT agent_name,
                   COUNT(*)                                              AS n,
                   AVG(directional_hit)                                 AS hit_rate,
                   AVG(CASE WHEN tp_hit IS NOT NULL THEN tp_hit END)    AS tp_rate
            FROM signal_scores
            WHERE validated_at > datetime('now', '-30 days')
              AND baseline_price != end_price
            GROUP BY agent_name
            ORDER BY n DESC
            """
        ).fetchall()
        acc = {r["agent_name"]: dict(r) for r in acc_rows}

        # Latest GME spot for the header
        spot_row = conn.execute(
            "SELECT close FROM price_ticks WHERE symbol='GME' AND close IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        spot = float(spot_row["close"]) if spot_row else None
        conn.close()

        # Gate decisions for every agent we have accuracy data for
        gate_decisions = {
            name: signal_gate.evaluate(name, DB_PATH)["decision"] for name in acc
        }

        trusted, muted = categorize_all(acc, gate_decisions)
        all_verdicts = trusted + muted

        # Status diff against the previous saved snapshot, then save today's
        today_iso = date.today().isoformat()
        previous_decisions = load_previous_gate_snapshot(DB_PATH, today_iso)
        status_diff = compute_status_diff(all_verdicts, previous_decisions)
        save_gate_snapshot(DB_PATH, all_verdicts, snapshot_date=today_iso)

        # Aggregate paper trade stats: team-level only, per-agent moved to /standup
        team_tp = team_total = 0
        team_pnl: list[float] = []
        for s in pt_stats:
            team_tp += s["tp_hits"] or 0
            team_total += (s["total"] or 0) - (s["open"] or 0)
            if s["avg_pnl"] is not None:
                team_pnl.append(float(s["avg_pnl"]))
        team_avg = (sum(team_pnl) / len(team_pnl)) if team_pnl else None

        from notifier import _send
        from message_formatters_v2 import get_ny_time_short
        msg = format_standup_brief(
            timestamp_et=get_ny_time_short(),
            spot_price=spot,
            trusted=trusted,
            muted=muted,
            last_24h_total=team_total,
            last_24h_wins=team_tp,
            last_24h_avg_pnl_pct=team_avg,
            status_diff=status_diff,
        )
        _send(msg)
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

        # job_defaults prevents Ollama-induced pileup: if a job is queued
        # because the previous one is still running, coalesce collapses
        # multiple missed firings into one, and max_instances=1 stops a
        # second copy from launching mid-run. misfire_grace_time keeps
        # firings within 60s usable instead of dropping them.
        self.scheduler = BackgroundScheduler(
            timezone="America/New_York",
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 60,
            },
        )
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
        # Anchor Trendy's 4h interval to 08:45 ET so daytime firings (08:45, 12:45,
        # 16:45 ET) land inside the active window (08:30-17:00 ET). Without this
        # the schedule drifts to wall-clock midnight and most firings get gated out.
        trendy_anchor = datetime.now(ET).replace(hour=8, minute=45, second=0, microsecond=0)
        self.scheduler.add_job(run_daily_trend,  IntervalTrigger(hours=4, start_date=trendy_anchor), id="trendy_interval")  # intraday

        # Signal confidence loop agents — bypass pattern (pre-fetched tools, direct Ollama)
        self.scheduler.add_job(run_trendy_signal,    IntervalTrigger(hours=4, start_date=trendy_anchor), id="trendy_signal")
        self.scheduler.add_job(run_pattern_signal,   IntervalTrigger(hours=2),    id="pattern_signal")
        # Intraday pattern detection on 5-minute bars — fires every bar close.
        # Active-window-gated, so off-hours firings exit silently.
        self.scheduler.add_job(run_intraday_pattern_signal, IntervalTrigger(minutes=5), id="intraday_pattern_signal")
        self.scheduler.add_job(run_futurist_prediction_signal, IntervalTrigger(hours=2), id="futurist_signal")
        # Dropped: run_synthesis_signal, run_newsie_signal, run_futurist_cycle —
        # CrewAI twins of run_synthesis / run_news / run_futurist_prediction_signal.
        # Gemma hallucinated without tool output; bypass versions already cover these.
        self.scheduler.add_job(run_georisk,      IntervalTrigger(hours=1),    id="georisk")   # hourly geopolitical scan

        # Daily jobs (market-hours aware) — timezone pinned to ET on every trigger
        # since BackgroundScheduler(timezone=...) default isn't being honored on
        # this host (suspected missing tzdata).
        self.scheduler.add_job(run_daily_huddle,      CronTrigger(hour=9,  minute=0,  timezone=ET), id="huddle")
        self.scheduler.add_job(run_daily_briefing,    CronTrigger(day_of_week="mon-fri", hour=10, minute=0,  timezone=ET), id="briefing")
        self.scheduler.add_job(run_standup_report,    CronTrigger(day_of_week="mon-fri", hour=11, minute=0,  timezone=ET), id="standup_midday")
        # 8 PM ET EOD analysis bypasses @active_window_required (window ends 17:00 ET).
        self.scheduler.add_job(run_daily_trend.__wrapped__, CronTrigger(hour=20, minute=0,  timezone=ET), id="trendy_eod")
        self.scheduler.add_job(run_daily_aggregation, CronTrigger(hour=16, minute=35, timezone=ET), id="aggregator")
        # Overwrite the aggregator's partial-capture rows with yfinance's
        # exchange-reported volume on weekdays. 16:40 = 5 min after aggregator,
        # by which point Yahoo's daily bar is finalized for the session.
        self.scheduler.add_job(run_history_overwrite, CronTrigger(day_of_week="mon-fri", hour=16, minute=40, timezone=ET), id="history_overwrite")
        self.scheduler.add_job(run_intraday_aggregation, IntervalTrigger(minutes=5), id="aggregator_intraday")
        self.scheduler.add_job(run_voice_forwarder, IntervalTrigger(minutes=1), id="voice_forwarder")
        self.scheduler.add_job(run_standup_report,    CronTrigger(day_of_week="mon-fri", hour=16, minute=0,  timezone=ET), id="standup_close")

        # Learning sessions — agents review their own performance and adapt
        self.scheduler.add_job(run_learning_debrief, CronTrigger(hour=16, minute=30, timezone=ET), id="debrief")
        # 5-min buffer after debrief — performance_scores has settled before
        # the producer mines signal_scores for new graduated lessons.
        self.scheduler.add_job(run_lesson_producer,  CronTrigger(hour=16, minute=35, timezone=ET), id="lesson_producer")
        self.scheduler.add_job(run_weekly_review,    CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=ET), id="weekly_review")
        # Saturday week-in-review digest — outside trading hours by design,
        # team reads the week's performance + £5k tracker over coffee.
        self.scheduler.add_job(run_saturday_review,  CronTrigger(day_of_week="sat", hour=9,  minute=0, timezone=ET), id="saturday_review")

        # CTO structural intelligence — PE playbook monitoring and short side research
        self.scheduler.add_job(run_cto_daily_brief,    CronTrigger(hour=9,  minute=5,  timezone=ET),                   id="cto_brief")
        self.scheduler.add_job(run_cto_dv_score,       CronTrigger(hour=9,  minute=10, day_of_week='mon-fri', timezone=ET), id="cto_dv")
        self.scheduler.add_job(run_dv_history_log,     CronTrigger(hour=9,  minute=15, day_of_week='mon-fri', timezone=ET), id="dv_history_log")
        self.scheduler.add_job(run_dv_history_resolve, CronTrigger(hour=3, minute=30, timezone=ET),                    id="dv_history_resolve")
        self.scheduler.add_job(run_cto_structural_scan, CronTrigger(hour=8, minute=0, timezone=ET), id="cto_scan")
        self.scheduler.add_job(run_investor_intel_scan, CronTrigger(hour=8, minute=0, timezone=ET),                    id="investor_intel")

        # Monday weekend digest — pre-open news + gap-risk read before the 09:00 huddle
        self.scheduler.add_job(run_monday_weekend_digest, CronTrigger(day_of_week="mon", hour=8, minute=0, timezone=ET), id="monday_digest")

        # Options intelligence — max pain every Monday pre-market
        self.scheduler.add_job(run_options_update, CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=ET), id="options")
        # Every 30 min Mon-Fri 08:00-16:30 ET. yfinance + FINRA are cheap and
        # idempotent — this catches FINRA's mid-morning publish without waiting
        # until next day's pre-open refresh.
        self.scheduler.add_job(run_fundamentals_update, CronTrigger(day_of_week="mon-fri", hour="8-16", minute="0,30", timezone=ET), id="fundamentals")

        # Weekly coffee nudge — Sundays 10:00 AM ET
        self.scheduler.add_job(run_sunday_support_message, CronTrigger(day_of_week="sun", hour=10, minute=0, timezone=ET), id="sunday_support")
        self.scheduler.add_job(run_sunday_week_ahead, CronTrigger(day_of_week="sun", hour=13, minute=0, timezone=ET), id="sunday_week_ahead")

        # NOTE: The twice-daily run_promo_broadcast cron entries (10:00 + 15:30)
        # were removed — they duplicated context already in /briefing and
        # /standup_close. The function itself stays for ad-hoc /force promo
        # invocation; re-add a single cron entry here if a scheduled promo
        # becomes wanted again.

        # Social monitor — DISABLED 2026-05-14. Both backend paths are dead:
        # Nitter instances (poast.org/privacydev.net) return 403/DNS-fail in
        # 2026 (X actively blocks them); the Supabase Edge function silently
        # returns [] for every account (likely TwitterAPI.io key expired or
        # function not deployed). Re-enable by uncommenting the line below
        # after either (a) setting X_BEARER_TOKEN in .env or (b) repairing
        # the Supabase Edge function. The run_social_scan implementation
        # already correctly distinguishes failure from no-posts.
        # self.scheduler.add_job(run_social_scan, IntervalTrigger(minutes=15), id="social")

        # Periodic intelligence digest — every 4 hours to Telegram
        self.scheduler.add_job(run_periodic_brief, IntervalTrigger(hours=4), id="periodic_brief")

        # Prediction calibration — truth-serum for stated confidence numbers.
        # Scores predictions against the price at (made_at + horizon), not EOD
        # close. Runs every 10 min so we catch 1h predictions the moment their
        # window elapses instead of waiting until 4:30 PM debrief.
        self.scheduler.add_job(run_calibration, IntervalTrigger(minutes=10), id="calibration")

        # Paper trade close-checker — scan open hypothetical trades against
        # real price ticks every 5 min; close on TP/SL first-touch or 4h expiry.
        self.scheduler.add_job(run_paper_trade_checker, IntervalTrigger(minutes=5), id="paper_trade_checker")

        # Nightly DB maintenance: backup + prune old backups + log cleanup (3 AM ET)
        from db_maintenance import nightly_maintenance
        self.scheduler.add_job(nightly_maintenance, CronTrigger(hour=3, minute=0, timezone=ET), id="db_nightly")

    def start(self):
        self.configure_schedule()
        # Start bot + sync threads first, then defer scheduler 60s. Otherwise
        # APScheduler fires backfilled jobs (Trendy, Synthesis, Futurist...)
        # the instant scheduler.start() returns, all hitting the single Ollama
        # runner at once and starving any user /commands queued in Telegram.
        start_bot_thread()
        start_sync_thread()
        # Yahoo Finance fallback poller — fills price_ticks every 5 min when
        # TradingView webhooks go quiet. INSERT OR IGNORE on UNIQUE(symbol,
        # timestamp) so primary TradingView data wins when both fire.
        start_yahoo_feed()
        log.info("[startup] Bot online — deferring scheduler 60s to keep Ollama free for queued /commands")
        time.sleep(60)
        self.scheduler.start()
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
║  PaperTrader (close checker)   every 5 min — auto TP/SL close    ║
║  Chatty   (commentary)         every 5 min — reads Synthesis     ║
║  Newsie   (news sentiment)     every 30 min                      ║
║  Pattern  (multi-day)          every 2 hours                     ║
║  Trendy   (daily trend)        every 4 hours + 8:00 PM ET EOD    ║
║  Futurist (strategic signal)   every 2 hours (gate-checked)      ║
║  Voice Forwarder               every 10 min — agent→Telegram     ║
║  Boss     (daily huddle)       9:00 AM ET — mission briefing     ║
║  CTO      (structural brief)   9:05 AM ET — PE playbook + shorts ║
║  CTO      (DV score)           9:10 AM ET — deep-value rating    ║
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
