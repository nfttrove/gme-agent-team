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
from telegram_bot import start_bot_thread, is_halted
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
    """Call .agent/tools/recall.py to surface relevant lessons before agent cycles."""
    try:
        agent_dir = os.path.dirname(__file__)
        recall_script = os.path.join(agent_dir, "..", ".agent", "tools", "recall.py")

        if not os.path.exists(recall_script):
            return ""  # Silent fallback if .agent not set up yet

        result = subprocess.run(
            [sys.executable, recall_script, intent],
            capture_output=True, text=True, timeout=5, cwd=agent_dir
        )

        if result.returncode == 0:
            return result.stdout
        else:
            return ""
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
def run_commentary():
    """Chatty — one-shot pithy commentary via direct Ollama call.

    Bypasses CrewAI because Gemma + CrewAI's prompt templating was returning
    empty/garbled responses, and CrewAI then fell back to echoing the agent's
    backstory as str(result) — which is what was getting logged for months
    instead of actual commentary. Direct Ollama also avoids the 180s crew
    timeout since there's no orchestration layer.
    """
    write_log("Chatty", "Composing commentary", "commentary", "running")
    try:
        conn = sqlite3.connect(DB_PATH)
        synthesis = conn.execute(
            "SELECT content FROM agent_logs WHERE agent_name='Synthesis' "
            "AND status='ok' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        latest_tick = conn.execute(
            "SELECT close, volume, timestamp FROM price_ticks "
            "WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        avg_vol_row = conn.execute(
            "SELECT AVG(volume) FROM price_ticks WHERE symbol='GME' "
            "AND timestamp > datetime('now','-10 minutes')"
        ).fetchone()

        synthesis_text = synthesis[0][:200] if synthesis else "No consensus yet"
        price, vol, _ts = latest_tick if latest_tick else (0, 0, "")
        avg_vol = avg_vol_row[0] if avg_vol_row and avg_vol_row[0] else 0
        vol_ratio = (vol / avg_vol) if avg_vol else 0

        prompt = (
            "You are GME's live-stream commentator. Produce ONE punchy insight (max 120 chars) "
            "grounded in the data below. No preamble, no quotes, no markdown — just the comment.\n\n"
            f"Price: ${price}\n"
            f"Volume: {int(vol):,} ({vol_ratio:.1f}x 10-min avg)\n"
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
    """Shared helper: chart pattern detection from 30-day candles. CrewAI bypass.

    Returns (signal: PatternSignal, narrative: str) or (None, reason: str) on failure.
    """
    from tools import IndicatorTool, PriceDataTool
    from models.agent_outputs import PatternSignal

    ind = IndicatorTool()._run(lookback_days=30)
    if not ind or not ind.get("price"):
        return None, "no indicator data available"
    candles = PriceDataTool()._run(lookback_days=30)
    if not candles or len(candles) < 10:
        return None, "insufficient candles (<10)"

    # Build a compact recent-history string — last 15 days, oldest first
    tail = candles[-15:]
    history = "\n".join(
        f"    {c.get('date','')[:10]}  "
        f"O={float(c.get('open',0) or 0):.2f} H={float(c.get('high',0) or 0):.2f} "
        f"L={float(c.get('low',0) or 0):.2f} C={float(c.get('close',0) or 0):.2f} "
        f"V={int(c.get('volume',0) or 0):,}"
        for c in tail
    )
    recent_high = max(float(c.get("high", 0) or 0) for c in tail)
    recent_low = min(float(c.get("low", 0) or 0) for c in tail if (c.get("low") or 0) > 0)
    price = float(ind["price"])

    prompt = (
        "You are the Pattern agent — multi-day chart pattern analyst for GME.\n"
        "Respond with ONE JSON object only (no markdown, no preamble).\n\n"
        f"LIVE DATA:\n"
        f"  price={price:.2f}  vwap={ind.get('vwap',0):.2f}  ema21={ind.get('ema21',0):.2f}  "
        f"ema50={ind.get('ema50',0):.2f}  rsi14={ind.get('rsi14',0):.1f}\n"
        f"  15d high={recent_high:.2f}  15d low={recent_low:.2f}\n"
        f"  Recent OHLCV (oldest → newest):\n{history}\n\n"
        "Schema (all fields required):\n"
        '{"pattern_type": "<ascending_triangle|descending_triangle|flag|wedge|breakout|breakdown|channel|consolidation|none>", '
        '"confidence": <0.0-1.0>, "breakout_level": <float>, '
        '"breakout_direction": "UP"|"DOWN", '
        '"reasoning": "<one sentence citing specific dates/levels from the data, max 220 chars>", '
        '"severity": "HIGH"|"MEDIUM"|"LOW"}\n\n'
        "Rules: breakout_level must be within the observed 15d range. "
        "Use 'none' pattern_type and confidence ≤ 0.40 if no clear structure. "
        "severity=HIGH only if confidence >= 0.75 AND price is within 2% of breakout_level."
    )

    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    r = requests.post(
        f"{ollama_host}/api/generate",
        json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
              "options": {"num_predict": 320, "temperature": 0.3}},
        timeout=60,
    )
    r.raise_for_status()
    raw = r.json().get("response", "")
    data = _extract_json(raw)
    if not data:
        return None, f"parse error: {raw[:300]}"
    try:
        signal = PatternSignal(**data)
    except Exception as e:
        return None, f"validation error: {e} | raw={raw[:300]}"

    narrative = (
        f"{signal.pattern_type} · {signal.breakout_direction} break @ ${signal.breakout_level:.2f} "
        f"(conf={signal.confidence:.0%}) · {signal.reasoning[:220]}"
    )
    return signal, narrative


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
    if is_halted():
        log.info("[Futurist] Trading halted by Telegram /halt — skipping cycle")
        return

    log.info("[Futurist] Starting full strategic cycle")
    write_log("Futurist", "Starting strategic cycle — gate check", "full_cycle", "running")

    # Recall relevant lessons before strategic cycle
    lessons = recall_lessons("GME trading strategy, market conditions, IV management, risk rules")
    if lessons:
        log.info(f"[Futurist] Recalled lessons:\n{lessons}")
        write_log("Futurist", f"Recalled lessons context:\n{lessons[:500]}", "recall")

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


def run_learning_debrief():
    """4:30 PM ET — score predictions vs actuals and compute agent metrics."""
    log.info("[Learner] === Post-market debrief ===")
    try:
        learner.post_market_debrief()
    except Exception as e:
        log.error(f"[Learner] Debrief failed: {e}")
        write_log("Learner", str(e), "daily_debrief", "error")


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

        # Ask Gemma for the interpretation paragraph only — numbers are locked above.
        prompt = (
            "You are the CTO — a deep-value analyst. In ONE paragraph (max 350 chars), "
            "interpret this Trove score. Note any tension between earnings metrics and "
            "balance-sheet strength. No preamble, no markdown, no quotes.\n\n"
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
            interpretation = " ".join(interpretation.split())[:500]
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
    """9:05 AM ET — CTO structural intelligence brief, just after morning huddle."""
    from agents import cto_agent
    from tasks import cto_daily_brief_task
    log.info("[CTO] === Daily structural intelligence brief ===")
    write_log("CTO", "Running daily structural brief", "structural_brief", "running")
    try:
        crew = Crew(agents=[cto_agent], tasks=[cto_daily_brief_task],
                    process=Process.sequential, verbose=True)
        result = crew.kickoff()
        write_log("CTO", str(result)[:2000], "structural_brief")
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
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        price_row = conn.execute(
            "SELECT close, volume, timestamp FROM price_ticks WHERE symbol='GME' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not price_row:
            write_log("Synthesis", "no price data available", "synthesis", "error")
            conn.close()
            return
        price = float(price_row["close"])

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
        prompt = (
            "You are the team's Synthesis agent. Produce ONE line summarising the current consensus, "
            "in this EXACT format (keep labels, replace bracketed values):\n"
            "PRICE: $XX.XX | DATA: [clean/degraded] | NEWS: [bullish/bearish/neutral, score] | "
            "TREND: [up/down/sideways] | PREDICTION: [bias, confidence%] | "
            "STRUCTURAL: [GREEN/YELLOW/RED] | CONSENSUS: [BULLISH/BEARISH/NEUTRAL] [XX]%\n\n"
            "Rules: use ONLY the data below, do not invent. If an agent is missing, write 'n/a'. "
            "Consensus = majority of the non-n/a directional signals. No preamble, no markdown, no quotes.\n\n"
            f"LIVE DATA:\n  Price: ${price:.2f}\n  Recent per-agent outputs (last 4h):\n{logs_block}\n"
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
        return "LOW", "LOW - No geopolitical or supply-chain signals detected in the last news scan."

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


@active_window_required
def run_periodic_brief():
    """Every 4 hours — send human-readable intelligence digest to Telegram."""
    import sqlite3
    from notifier import notify_periodic_brief

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Get latest price
        price_row = conn.execute(
            "SELECT close, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        price = price_row['close'] if price_row else 0

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

        # Calculate % change (simple: compare to previous close or assume 0)
        pct_change = 0.5  # placeholder; could enhance by querying two ticks

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
    """9:32 AM ET — ELI5 strategy briefing sent to Telegram after market opens."""
    from agents import briefing_agent
    from tasks import daily_briefing_task
    log.info("[Briefing] === Daily Strategy Brief ===")
    write_log("Briefing", "Composing daily strategy brief", "daily_brief", "running")
    try:
        crew = Crew(agents=[briefing_agent], tasks=[daily_briefing_task],
                    process=Process.sequential, verbose=False)
        result = crew.kickoff()
        brief = str(result)
        write_log("Briefing", brief[:2000], "daily_brief")
        from notifier import notify
        notify(f"<b>📋 DAILY STRATEGY BRIEF</b>\n\n{brief[:3000]}")
        log.info(f"[Briefing] Brief sent to Telegram")
    except Exception as e:
        log.error(f"[Briefing] {e}")
        write_log("Briefing", str(e), "daily_brief", "error")


def run_daily_huddle():
    """Morning briefing — Boss recaps the mission and reviews yesterday's performance."""
    from agents import project_manager_agent
    from tasks import daily_huddle_task
    log.info("[Huddle] === DAILY TEAM BRIEFING ===")
    try:
        crew = Crew(agents=[project_manager_agent], tasks=[daily_huddle_task],
                    process=Process.sequential, verbose=True)
        result = crew.kickoff()
        log.info(f"[Huddle]\n{result}")
        write_log("Boss", str(result), "daily_huddle")
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

        # Signal confidence loop agents (NEW)
        self.scheduler.add_job(run_synthesis_signal, IntervalTrigger(minutes=5),  id="synthesis_signal")
        self.scheduler.add_job(run_trendy_signal,    IntervalTrigger(hours=4),    id="trendy_signal")
        self.scheduler.add_job(run_pattern_signal,   IntervalTrigger(hours=2),    id="pattern_signal")
        self.scheduler.add_job(run_newsie_signal,    IntervalTrigger(minutes=30), id="newsie_signal")
        self.scheduler.add_job(run_futurist_prediction_signal, IntervalTrigger(hours=2), id="futurist_signal")

        self.scheduler.add_job(run_futurist_cycle, IntervalTrigger(hours=2),  id="futurist")
        self.scheduler.add_job(run_georisk,      IntervalTrigger(hours=1),    id="georisk")   # hourly geopolitical scan

        # Daily jobs (market-hours aware)
        self.scheduler.add_job(run_daily_huddle,      CronTrigger(hour=9,  minute=0),  id="huddle")
        self.scheduler.add_job(run_daily_briefing,    CronTrigger(hour=9,  minute=32), id="briefing")
        self.scheduler.add_job(run_standup_report,    CronTrigger(hour=11, minute=0),  id="standup_midday")
        self.scheduler.add_job(run_daily_trend,       CronTrigger(hour=20, minute=0),  id="trendy_eod")
        self.scheduler.add_job(run_daily_aggregation, CronTrigger(hour=16, minute=35), id="aggregator")
        self.scheduler.add_job(run_intraday_aggregation, IntervalTrigger(minutes=5), id="aggregator_intraday")
        self.scheduler.add_job(run_voice_forwarder, IntervalTrigger(minutes=10), id="voice_forwarder")
        self.scheduler.add_job(run_standup_report,    CronTrigger(hour=16, minute=0),  id="standup_close")

        # Learning sessions — agents review their own performance and adapt
        self.scheduler.add_job(run_learning_debrief, CronTrigger(hour=16, minute=30), id="debrief")
        self.scheduler.add_job(run_weekly_review,    CronTrigger(day_of_week="fri", hour=17, minute=0), id="weekly_review")

        # CTO structural intelligence — PE playbook monitoring and short side research
        self.scheduler.add_job(run_cto_daily_brief,    CronTrigger(hour=9,  minute=5),                        id="cto_brief")
        self.scheduler.add_job(run_cto_trove_score,    CronTrigger(hour=9,  minute=10),                       id="cto_trove")
        self.scheduler.add_job(run_cto_structural_scan, CronTrigger(day_of_week="sun", hour=8, minute=0),     id="cto_scan")
        self.scheduler.add_job(run_investor_intel_scan, CronTrigger(hour=8, minute=0),                        id="investor_intel")

        # Options intelligence — max pain every Monday pre-market
        self.scheduler.add_job(run_options_update, CronTrigger(day_of_week="mon", hour=8, minute=30), id="options")

        # Social monitor — scan tracked accounts every 15 minutes during market hours
        self.scheduler.add_job(run_social_scan, IntervalTrigger(minutes=15), id="social")

        # Periodic intelligence digest — every 4 hours to Telegram
        self.scheduler.add_job(run_periodic_brief, IntervalTrigger(hours=4), id="periodic_brief")

        # Nightly DB maintenance: backup + prune old backups + log cleanup (3 AM ET)
        from db_maintenance import nightly_maintenance
        self.scheduler.add_job(nightly_maintenance, CronTrigger(hour=3, minute=0), id="db_nightly")

    def start(self):
        self.configure_schedule()
        self.scheduler.start()
        start_bot_thread()
        start_sync_thread()
        write_log("Orchestrator", "All 10 agents online", "startup")

        log.info("""
╔══════════════════════════════════════════════════════════════════╗
║      GME Multi-Agent Trading System — ONLINE (Gemini-first)     ║
╠══════════════════════════════════════════════════════════════════╣
║  Synthesis (cross-agent brief) every 5 min — shared context      ║
║  Valerie  (data validator)     every 1 min                       ║
║  Chatty   (commentary)         every 30 sec — reads Synthesis    ║
║  Newsie   (news sentiment)     every 30 min                      ║
║  Pattern  (multi-day)          every 2 hours                     ║
║  Trendy   (daily trend)        every 4 hours + 8:00 PM ET EOD    ║
║  Futurist (strategic signal)   every 2 hours (gate-checked)      ║
║  Boss     (daily huddle)       9:00 AM ET — mission briefing     ║
║  CTO      (structural brief)   9:05 AM ET — PE playbook + shorts ║
║  📊 STANDUP (agent perf)       11:00 AM & 4:00 PM ET — ROI check ║
║  Aggregator                    4:35 PM ET                        ║
║  Learner  (daily debrief)      4:30 PM ET — score + adapt        ║
║  Learner  (weekly review)      Fridays 5:00 PM ET                ║
║  CTO      (structural scan)    Sundays 8:00 AM — EDGAR + shorts  ║
╚══════════════════════════════════════════════════════════════════╝
        """)

        # Warm up: build shared context before the first full cycle
        run_investor_intel_scan()
        run_synthesis()
        run_futurist_cycle()

        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.scheduler.shutdown()


if __name__ == "__main__":
    TradingSystemOrchestrator().start()
