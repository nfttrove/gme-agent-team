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
from market_hours import is_market_open, market_hours_required
from learner import AgentLearner
from telegram_bot import start_bot_thread, is_halted
from supabase_sync import start_sync_thread

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
metrics = MetricsLogger()
learner = AgentLearner()


# ── DB helpers ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(open(os.path.join(os.path.dirname(__file__), "db_schema.sql")).read())
    conn.commit()
    conn.close()


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

def run_validation():
    from agents import valerie_agent
    from tasks import validate_data_task
    write_log("Valerie", "Starting validation cycle", "validation", "running")
    try:
        crew = Crew(agents=[valerie_agent], tasks=[validate_data_task],
                    process=Process.sequential, verbose=False)
        result = crew.kickoff()
        write_log("Valerie", str(result)[:500], "validation")
    except Exception as e:
        log.error(f"[Valerie] {e}")
        write_log("Valerie", str(e), "validation", "error")


def run_commentary():
    from agents import chatty_agent
    from tasks import commentary_task
    write_log("Chatty", "Composing commentary", "commentary", "running")
    try:
        crew = Crew(agents=[chatty_agent], tasks=[commentary_task],
                    process=Process.sequential, verbose=False)
        result = crew.kickoff()
        log.info(f"[Chatty] {str(result)[:120]}")
        write_log("Chatty", str(result)[:500], "commentary")
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
            result = crew.kickoff()
            write_log("Newsie", str(result)[:1000], "news")
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
            result = crew.kickoff()
            write_log("Pattern", str(result)[:1000], "pattern")
        except Exception as e:
            log.error(f"[Pattern] {e}")
            write_log("Pattern", str(e), "pattern", "error")


def run_daily_trend():
    from agents import daily_trend_agent
    from tasks import daily_trend_task
    log.info("[Trendy] Running daily trend analysis")
    write_log("Trendy", "Running daily trend analysis", "daily_trend", "running")
    with metrics.cycle("daily_trend"):
        try:
            crew = Crew(agents=[daily_trend_agent], tasks=[daily_trend_task],
                        process=Process.sequential, verbose=True)
            result = crew.kickoff()
            write_log("Trendy", str(result)[:1000], "daily_trend")
        except Exception as e:
            log.error(f"[Trendy] {e}")
            write_log("Trendy", str(e), "daily_trend", "error")


@market_hours_required
def run_futurist_cycle():
    """Full strategic cycle: gate check → Futurist → Boss → Trader Joe if approved."""
    if is_halted():
        log.info("[Futurist] Trading halted by Telegram /halt — skipping cycle")
        return

    log.info("[Futurist] Starting full strategic cycle")
    write_log("Futurist", "Starting strategic cycle — gate check", "full_cycle", "running")

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
                futurist_agent, project_manager_agent, trader_agent,
            )
            from tasks import (
                daily_trend_task, multiday_trend_task, news_task,
                futurist_task, manager_task, trader_task,
            )

            crew = Crew(
                agents=[daily_trend_agent, multiday_trend_agent, news_analyst_agent,
                        futurist_agent, project_manager_agent, trader_agent],
                tasks=[daily_trend_task, multiday_trend_task, news_task,
                       futurist_task, manager_task, trader_task],
                process=Process.sequential,
                verbose=True,
            )
            result = crew.kickoff()
            write_log("Futurist", str(result)[:1000], "full_cycle")
            metrics.snapshot()
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


def run_synthesis():
    """Every 5 min — cross-agent intelligence synthesis so all agents share a common picture."""
    from agents import synthesis_agent
    from tasks import synthesis_task
    try:
        crew = Crew(agents=[synthesis_agent], tasks=[synthesis_task],
                    process=Process.sequential, verbose=False)
        result = crew.kickoff()
        log.info(f"[Synthesis] {str(result)[:120]}")
        write_log("Synthesis", str(result)[:500], "synthesis")
    except Exception as e:
        log.error(f"[Synthesis] {e}")
        write_log("Synthesis", str(e), "synthesis", "error")


def run_georisk():
    """Hourly GeoRisk scan — monitor global supply chain events."""
    from agents import georisk_agent
    from tasks import georisk_task
    try:
        crew = Crew(agents=[georisk_agent], tasks=[georisk_task],
                    process=Process.sequential, verbose=False)
        result = crew.kickoff()
        log.info(f"[GeoRisk] {str(result)[:120]}")
        write_log("GeoRisk", str(result)[:500], "georisk")
    except Exception as e:
        log.error(f"[GeoRisk] {e}")
        write_log("GeoRisk", str(e), "georisk", "error")


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
║  Futurist + Boss + Trader Joe  every 2 hours (gate-checked)      ║
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
