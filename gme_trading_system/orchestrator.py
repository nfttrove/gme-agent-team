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
from safe_kickoff import safe_kickoff, CrewTimeout
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
    from agents import valerie_agent
    from tasks import validate_data_task
    write_log("Valerie", "Starting validation cycle", "validation", "running")
    try:
        crew = Crew(agents=[valerie_agent], tasks=[validate_data_task],
                    process=Process.sequential, verbose=False)
        result = safe_kickoff(crew, timeout_seconds=180, label="valerie")
        write_log("Valerie", str(result)[:500], "validation")
    except CrewTimeout as e:
        log.error(f"[Valerie] TIMEOUT: {e}")
        write_log("Valerie", f"TIMEOUT: {e}", "validation", "timeout")
    except Exception as e:
        log.error(f"[Valerie] {e}")
        write_log("Valerie", str(e), "validation", "error")


@active_window_required
def run_commentary():
    from agents import chatty_agent
    from tasks import commentary_task
    write_log("Chatty", "Composing commentary", "commentary", "running")
    try:
        crew = Crew(agents=[chatty_agent], tasks=[commentary_task],
                    process=Process.sequential, verbose=False)
        result = safe_kickoff(crew, timeout_seconds=180, label="chatty")
        log.info(f"[Chatty] {str(result)[:120]}")
        write_log("Chatty", str(result)[:500], "commentary")
    except CrewTimeout as e:
        log.error(f"[Chatty] TIMEOUT: {e}")
        write_log("Chatty", f"TIMEOUT: {e}", "commentary", "timeout")
    except Exception as e:
        log.error(f"[Chatty] {e}")
        write_log("Chatty", str(e), "commentary", "error")


@market_hours_required
def run_news():
    from agents import news_analyst_agent
    from tasks import news_task
    log.info("[Newsie] Running news sentiment cycle")
    write_log("Newsie", "Scanning news sentiment", "news", "running")
    with metrics.cycle("news"):
        try:
            crew = Crew(agents=[news_analyst_agent], tasks=[news_task],
                        process=Process.sequential, verbose=True)
            result = safe_kickoff(crew, timeout_seconds=300, label="newsie")
            write_log("Newsie", str(result)[:1000], "news")
        except CrewTimeout as e:
            log.error(f"[Newsie] TIMEOUT: {e}")
            write_log("Newsie", f"TIMEOUT: {e}", "news", "timeout")
        except Exception as e:
            log.error(f"[Newsie] {e}")
            write_log("Newsie", str(e), "news", "error")


@market_hours_required
def run_pattern():
    from agents import multiday_trend_agent
    from tasks import multiday_trend_task, daily_trend_task
    log.info("[Pattern] Running multi-day pattern analysis")
    write_log("Pattern", "Analysing multi-day chart patterns", "pattern", "running")
    with metrics.cycle("pattern"):
        try:
            crew = Crew(agents=[multiday_trend_agent], tasks=[multiday_trend_task],
                        process=Process.sequential, verbose=True)
            result = safe_kickoff(crew, timeout_seconds=300, label="pattern")
            write_log("Pattern", str(result)[:1000], "pattern")
        except CrewTimeout as e:
            log.error(f"[Pattern] TIMEOUT: {e}")
            write_log("Pattern", f"TIMEOUT: {e}", "pattern", "timeout")
        except Exception as e:
            log.error(f"[Pattern] {e}")
            write_log("Pattern", str(e), "pattern", "error")


@active_window_required
def run_daily_trend():
    from agents import daily_trend_agent
    from tasks import daily_trend_task
    log.info("[Trendy] Running daily trend analysis")
    write_log("Trendy", "Running daily trend analysis", "daily_trend", "running")
    with metrics.cycle("daily_trend"):
        try:
            crew = Crew(agents=[daily_trend_agent], tasks=[daily_trend_task],
                        process=Process.sequential, verbose=True)
            result = safe_kickoff(crew, timeout_seconds=300, label="trendy")
            write_log("Trendy", str(result)[:1000], "daily_trend")
        except CrewTimeout as e:
            log.error(f"[Trendy] TIMEOUT: {e}")
            write_log("Trendy", f"TIMEOUT: {e}", "daily_trend", "timeout")
        except Exception as e:
            log.error(f"[Trendy] {e}")
            write_log("Trendy", str(e), "daily_trend", "error")


@market_hours_required
@active_window_required
def run_futurist_prediction_signal():
    """Futurist agent prediction with signal confidence logging.

    Runs Futurist solo (not full crew) to:
    1. Generate price prediction with confidence score
    2. Log signal to signal_alerts table
    3. Send Telegram alert with entry/stop/target for team execution
    4. Enable feedback loop (team logs decision → win rate tracking)
    """
    from agents import futurist_agent
    from tasks import futurist_task
    import json
    import re
    import uuid

    log.info("[Futurist] Starting prediction signal cycle (DeepSeek-r1:8b)")
    write_log("Futurist", "Running price prediction signal cycle", "prediction_signal", "running")

    with metrics.cycle("futurist_prediction"):
        try:
            # Run Futurist solo (not full crew)
            crew = Crew(
                agents=[futurist_agent],
                tasks=[futurist_task],
                process=Process.sequential,
                verbose=True,
            )
            result = safe_kickoff(crew, timeout_seconds=300, label="futurist_prediction")

            # Parse output to FuturistPrediction Pydantic model
            # DeepSeek-r1 output includes <thought> block + final JSON
            prediction_data = _extract_futurist_prediction(str(result))
            if not prediction_data:
                log.warning("[Futurist] Could not parse prediction from output")
                write_log("Futurist", f"Failed to parse prediction:\n{str(result)[:500]}", "prediction_signal", "parse_error")
                return

            try:
                prediction = FuturistPrediction(**prediction_data)
            except Exception as e:
                log.error(f"[Futurist] Pydantic validation failed: {e}")
                write_log("Futurist", f"Validation error: {e}", "prediction_signal", "validation_error")
                return

            # Log raw output for reference
            write_log("Futurist", str(result)[:1000], "prediction_signal", "ok")

            # Log signal with confidence
            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Futurist",
                signal_type=prediction.signal_type,
                confidence=prediction.confidence,
                severity="HIGH" if prediction.confidence >= 0.80 else ("MEDIUM" if prediction.confidence >= 0.65 else "LOW"),
                entry_price=prediction.predicted_price * 0.99,  # 1% slippage allowance
                stop_loss=prediction.stop_loss,
                take_profit=prediction.take_profit,
                reasoning=prediction.reasoning[:500] if prediction.reasoning else "",
            )
            log.info(f"[Futurist] Signal logged: {alert_id[:8]} | confidence={prediction.confidence:.0%}")

            # Send Telegram alert
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
                log.info("[Futurist] Telegram alert sent")
            except Exception as e:
                log.warning(f"[Futurist] Telegram alert failed (non-critical): {e}")

            metrics.snapshot()

        except CrewTimeout as e:
            log.error(f"[Futurist] TIMEOUT: {e}")
            write_log("Futurist", f"TIMEOUT: {e}", "prediction_signal", "timeout")
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
    """Trendy agent trend signal with confidence logging."""
    from agents import daily_trend_agent
    from tasks import daily_trend_task
    from models.agent_outputs import TrendySignal

    log.info("[Trendy] Starting trend signal cycle")
    write_log("Trendy", "Running trend signal cycle", "trend_signal", "running")

    with metrics.cycle("trendy_signal"):
        try:
            crew = Crew(
                agents=[daily_trend_agent],
                tasks=[daily_trend_task],
                process=Process.sequential,
                verbose=True,
            )
            result = safe_kickoff(crew, timeout_seconds=300, label="trendy_signal")

            # Parse output to TrendySignal
            signal_data = _extract_json(str(result))
            if not signal_data:
                log.warning("[Trendy] Could not parse signal")
                write_log("Trendy", f"Failed to parse signal:\n{str(result)[:500]}", "trend_signal", "parse_error")
                return

            try:
                signal = TrendySignal(**signal_data)
            except Exception as e:
                log.error(f"[Trendy] Validation failed: {e}")
                write_log("Trendy", f"Validation error: {e}", "trend_signal", "validation_error")
                return

            write_log("Trendy", str(result)[:1000], "trend_signal", "ok")

            # Log signal
            manager = SignalManager(DB_PATH)
            entry_price = signal.support_level if signal.trend_direction == "UP" else signal.resistance_level
            alert_id = manager.log_alert(
                agent_name="Trendy",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=entry_price,
                stop_loss=signal.resistance_level if signal.trend_direction == "UP" else signal.support_level,
                take_profit=signal.resistance_level if signal.trend_direction == "UP" else signal.support_level * 0.97,
                reasoning=signal.reasoning,
            )
            log.info(f"[Trendy] Signal logged: {alert_id[:8]} | confidence={signal.confidence:.0%}")

            # Send alert
            try:
                notify_signal_alert(
                    agent_name="Trendy",
                    signal_type=signal.signal_type,
                    confidence=signal.confidence,
                    entry_price=entry_price,
                    stop_loss=signal.resistance_level if signal.trend_direction == "UP" else signal.support_level,
                    take_profit=signal.resistance_level if signal.trend_direction == "UP" else signal.support_level * 1.02,
                    reasoning=f"{signal.trend_direction} trend | Support: ${signal.support_level:.2f}, Resistance: ${signal.resistance_level:.2f}",
                    alert_id=alert_id,
                )
                log.info("[Trendy] Telegram alert sent")
            except Exception as e:
                log.warning(f"[Trendy] Telegram alert failed (non-critical): {e}")

            metrics.snapshot()

        except CrewTimeout as e:
            log.error(f"[Trendy] TIMEOUT: {e}")
            write_log("Trendy", f"TIMEOUT: {e}", "trend_signal", "timeout")
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
            result = safe_kickoff(crew, timeout_seconds=300, label="synthesis_signal")

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
    """Pattern agent chart pattern signal with confidence logging."""
    from agents import multiday_trend_agent
    from tasks import multiday_trend_task
    from models.agent_outputs import PatternSignal

    log.info("[Pattern] Starting pattern signal cycle")
    write_log("Pattern", "Running pattern signal cycle", "pattern_signal", "running")

    with metrics.cycle("pattern_signal"):
        try:
            crew = Crew(
                agents=[multiday_trend_agent],
                tasks=[multiday_trend_task],
                process=Process.sequential,
                verbose=True,
            )
            result = safe_kickoff(crew, timeout_seconds=300, label="pattern_signal")

            signal_data = _extract_json(str(result))
            if not signal_data:
                log.warning("[Pattern] Could not parse signal")
                return

            try:
                signal = PatternSignal(**signal_data)
            except Exception as e:
                log.error(f"[Pattern] Validation failed: {e}")
                return

            write_log("Pattern", str(result)[:1000], "pattern_signal", "ok")

            # Log signal
            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Pattern",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=signal.breakout_level * 0.99,
                stop_loss=signal.breakout_level * 0.96 if signal.breakout_direction == "UP" else signal.breakout_level * 1.04,
                take_profit=signal.breakout_level * 1.04 if signal.breakout_direction == "UP" else signal.breakout_level * 0.96,
                reasoning=signal.reasoning,
            )

            try:
                notify_signal_alert(
                    agent_name="Pattern",
                    signal_type=signal.signal_type,
                    confidence=signal.confidence,
                    entry_price=signal.breakout_level,
                    stop_loss=signal.breakout_level * 0.96,
                    take_profit=signal.breakout_level * 1.04,
                    reasoning=f"{signal.pattern_type} | {signal.breakout_direction} breakout at ${signal.breakout_level:.2f}",
                    alert_id=alert_id,
                )
            except Exception as e:
                log.warning(f"[Pattern] Alert failed: {e}")

            metrics.snapshot()

        except CrewTimeout as e:
            log.error(f"[Pattern] TIMEOUT: {e}")
            write_log("Pattern", f"TIMEOUT: {e}", "pattern_signal", "timeout")
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
            result = safe_kickoff(crew, timeout_seconds=300, label="newsie_signal")

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
            result = safe_kickoff(crew, timeout_seconds=600, label="futurist_full_cycle")
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
    """Every 5 min — cross-agent intelligence synthesis so all agents share a common picture."""
    from agents import synthesis_agent
    from tasks import synthesis_task
    try:
        crew = Crew(agents=[synthesis_agent], tasks=[synthesis_task],
                    process=Process.sequential, verbose=False)
        result = safe_kickoff(crew, timeout_seconds=180, label="synthesis")
        log.info(f"[Synthesis] {str(result)[:120]}")
        write_log("Synthesis", str(result)[:500], "synthesis")
    except CrewTimeout as e:
        log.error(f"[Synthesis] TIMEOUT: {e}")
        write_log("Synthesis", f"TIMEOUT: {e}", "synthesis", "timeout")
    except Exception as e:
        log.error(f"[Synthesis] {e}")
        write_log("Synthesis", str(e), "synthesis", "error")


@active_window_required
def run_georisk():
    """Hourly GeoRisk scan — monitor global supply chain events."""
    from agents import georisk_agent
    from tasks import georisk_task
    try:
        crew = Crew(agents=[georisk_agent], tasks=[georisk_task],
                    process=Process.sequential, verbose=False)
        result = safe_kickoff(crew, timeout_seconds=180, label="georisk")
        log.info(f"[GeoRisk] {str(result)[:120]}")
        write_log("GeoRisk", str(result)[:500], "georisk")
    except CrewTimeout as e:
        log.error(f"[GeoRisk] TIMEOUT: {e}")
        write_log("GeoRisk", f"TIMEOUT: {e}", "georisk", "timeout")
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


# ── Orchestrator class ─────────────────────────────────────────────────────────

class TradingSystemOrchestrator:
    def __init__(self):
        init_db()
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
        self.scheduler.add_job(run_daily_trend,       CronTrigger(hour=20, minute=0),  id="trendy_eod")
        self.scheduler.add_job(run_daily_aggregation, CronTrigger(hour=16, minute=35), id="aggregator")

        # Learning sessions — agents review their own performance and adapt
        self.scheduler.add_job(run_learning_debrief, CronTrigger(hour=16, minute=30), id="debrief")
        self.scheduler.add_job(run_weekly_review,    CronTrigger(day_of_week="fri", hour=17, minute=0), id="weekly_review")

        # CTO structural intelligence — PE playbook monitoring and short side research
        self.scheduler.add_job(run_cto_daily_brief,    CronTrigger(hour=9,  minute=5),                        id="cto_brief")
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
