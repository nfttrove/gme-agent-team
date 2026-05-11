# Agents

Behaviour spec for every agent in the system. Each entry describes what the agent **does** — when it runs, what it reads, what it writes, what humans see — without diving into implementation.

Defined in [gme_trading_system/agents.py](gme_trading_system/agents.py); scheduled in [gme_trading_system/orchestrator.py](gme_trading_system/orchestrator.py); tasks in [gme_trading_system/tasks.py](gme_trading_system/tasks.py). If this document and the code disagree, fix this document.

## Active window

Six agents are decorated with `@active_window_required` and **silently skip outside 07:30–18:00 ET, Mon–Fri**. They are: Valerie, Chatty, Newsie, Pattern, Trendy, Futurist, GeoRisk, Synthesis. Daily/weekly cron jobs run regardless.

## Confidence scoring

Every signal emitted to Telegram carries a 0.0–1.0 confidence score. The team replies (or uses `/executed`, `/ignored`, `/missed`) to log execution decisions. `signal_manager.py` and `confidence_calibration.py` compute per-agent multipliers from win rates over rolling windows. Pattern Intraday currently floors confidence at 0.80 and de-duplicates repeat alerts.

---

## The 12 agents

### Valerie — Data Validator
- **Cadence:** every 5 min, active window
- **Reads:** `price_ticks`, `daily_candles`, `agent_logs`
- **Writes:** `data_quality_logs`, `agent_logs`
- **Signals:** anomaly alerts when OHLC sanity checks fail (negative volume, gap > 20σ, stale feed)
- **Why it exists:** stops downstream agents reasoning on garbage data

### Chatty — Stream Commentator
- **Cadence:** every 5 min, active window
- **Reads:** recent `price_ticks`, `daily_candles`, latest Synthesis brief
- **Writes:** `stream_comments`, `agent_logs`
- **Signals:** narrative one-liners on price action — not alerting, just colour
- **Why it exists:** keeps the chat alive between bigger signals; cheap context for humans

### Newsie — News Analyst
- **Cadence:** every 30 min, active window
- **Reads:** Finnhub news API, Supabase edge cache, NewsAPI (circuit-broken)
- **Writes:** `news_signals`, `agent_logs`
- **Signals:** material news with directional bias + Gemini Flash grounding
- **Why it exists:** macro/headline context that price action alone misses
- **Recent:** Gemini Flash + Google Search grounding added (commit 9406b82)

### Pattern — Triangle Breakout & Multi-Day Pattern Specialist
- **Cadence:** every 2h (analysis), every 2h (signal), every 5 min (intraday signal), active window
- **Reads:** multi-day `daily_candles`, K-means clusters from `cluster_patterns.py`
- **Writes:** `pattern_signals`, `agent_logs`
- **Signals:** triangle/wedge breakouts with confidence ≥ 0.80; intraday repeats deduplicated
- **Why it exists:** multi-timeframe pattern detection that the eyeball-the-chart approach misses
- **Recent:** intraday signal dedupe + 0.80 confidence floor (commit 61893d5)

### Trendy — Daily Trend Analyst
- **Cadence:** every 4h analysis + signal during active window; 20:00 ET end-of-day cron
- **Reads:** today's `price_ticks`, recent `daily_candles`
- **Writes:** `trend_signals`, `agent_logs`
- **Signals:** daily trend direction with conviction; end-of-day summary push
- **Why it exists:** the "what kind of day was it" framing that everything else hangs off

### Futurist — Market Futurist
- **Cadence:** every 2h, active window
- **LLM:** DeepSeek-r1 / Gemini Pro for the reasoning step (not Gemma)
- **Reads:** price history, options chain, max pain, Synthesis brief
- **Writes:** `predictions` (1h/4h/EOD forecasts)
- **Signals:** prediction signals on high-conviction setups
- **Why it exists:** structured forward look that we can score against actuals via `confidence_calibration`
- **Recent:** max pain surfaced in 4-hour periodic brief (commit 2dede38)

### GeoRisk — GeoRisk Researcher
- **Cadence:** every 1h, active window
- **Reads:** geopolitical news feeds, structural signals
- **Writes:** `georisk_scores`, `agent_logs`
- **Signals:** elevated geopolitical risk that could impact GME exposure
- **Recent:** Gemini Flash + Google Search grounding added (commit 9406b82)

### Synthesis — Intelligence Synthesiser
- **Cadence:** every 5 min, active window
- **Reads:** the most recent output of every other agent
- **Writes:** `synthesis_brief` — a one-line consensus shared as context to all other agents next cycle
- **Why it exists:** prevents each agent reasoning in isolation; cheap mutual context

### Boss / Project Manager — Daily mission
- **Cadence:** 09:00 ET daily (huddle), 10:00 ET (briefing)
- **Reads:** yesterday's outputs, current mission state, team goals
- **Writes:** daily mission brief → Telegram
- **Why it exists:** sets the day's frame; reviews the previous day

### CTO — Chief Technology & Market Structure Officer
- **Cadence:** 09:05 ET daily brief, 09:10 ET Trove score, 09:15 ET Trove history log, Sun 08:00 ET structural scan
- **LLM:** DeepSeek-r1 / Gemini Pro
- **Reads:** PE playbook signals, short-side research, dark pool data
- **Writes:** `cto_brief`, `trove_scores`, `structural_signals`
- **Signals:** PE-playbook flags; structural shifts in short interest; Trove score changes for the watchlist
- **Why it exists:** the "is this still a PE-damaged squeeze setup" check; differentiated thesis layer

### Memoria — Historical Researcher
- **Cadence:** invoked on demand by other agents (no scheduled cycle of its own)
- **Reads:** episodic memory in `.agent/memory/`, graduated lessons
- **Writes:** lesson recall results
- **Why it exists:** retrieval-augmented reasoning for the other agents

### Briefing — Strategy Briefing Officer
- **Cadence:** invoked by the `/brief` Telegram command and the 10:00 ET cron
- **Reads:** latest Synthesis, last Trendy + Futurist, Newsie
- **Writes:** Telegram brief message
- **Why it exists:** the polished human-readable "what's happening" digest
- **Day-of-week character** — header tag and a Gemma-prompt context line vary by weekday:
  Monday (first day / weekend gap-risk), Tuesday (confirmation), Wednesday (mid-week pulse),
  Thursday (pre-opex), Friday (opex day). First-Friday-of-month appends an NFP note. See
  `_day_intro()` in [orchestrator.py](gme_trading_system/orchestrator.py).
- **No personal targets in the broadcast** — the £5k-by-deadline tracker is private and
  reachable only via the owner-only `/progress` command (see below).

---

## Signal cycles (separate from analysis cycles)

Several agents have a **separate signal-emission cycle** that filters their analysis output through `signal_gate.py` before pushing to Telegram:

| Agent | Analysis cycle | Signal cycle | Notes |
|-------|---------------|-------------|-------|
| Pattern | 2h | 2h + 5 min intraday | Intraday dedupe, 0.80 floor |
| Trendy | 4h | 4h | |
| Futurist | 2h | 2h | High-conviction predictions only |

`signal_gate.py` excludes flat validation windows from hit-rate (commit 30599ee).

## Cross-cutting schedules

Beyond the 12 agents, the orchestrator runs:
- `synthesis_brief` every 5 min — the shared context layer
- `aggregator_intraday` every 5 min — rolls ticks into candles
- `voice_forwarder` every 1 min — outbound speech queue
- `calibration` every 10 min — refreshes per-agent confidence multipliers
- `paper_trade_checker` every 5 min — tracks TP/SL on open paper trades
- `standup_report` at 11:00 + 16:00 ET — win-rate updates
- `learning_debrief` at 16:30 ET — closes out the day's signals
- `lesson_producer` at 16:35 ET — promotes patterns to lesson candidates
- `weekly_review` Fri 17:00 ET — `learner.weekly_strategy_review()`, parameter adaptation
- `saturday_review` Sat 09:00 ET — Telegram digest: week's trades + PnL, signal hit rate, Trove deep-value rankings (with week-over-week deltas once a prior snapshot exists), lesson candidates, system health (no personal-target figures)
- `monday_digest` Mon 08:00 ET — pre-open weekend digest: news since Fri close, GeoRisk weekend events, gap-risk vs Fri close (does NOT replace 09:00 huddle)
- `nightly_maintenance` at 03:00 ET — DB backup + WAL checkpoint + retention purge

See [gme_trading_system/orchestrator.py](gme_trading_system/orchestrator.py) line ~2530 onward for the full schedule.

## Adding a new agent

1. Define the agent in [agents.py](gme_trading_system/agents.py) as a `ResilientAgent` — pick a role, goal, backstory, no `tools` parameter (Gemma can't tool-call).
2. Define the task in [tasks.py](gme_trading_system/tasks.py) — description, expected_output, context.
3. Add a `run_<name>` cycle function in [orchestrator.py](gme_trading_system/orchestrator.py); decorate with `@active_window_required` if it shouldn't fire outside market hours.
4. Schedule it in `configure_schedule()` near line 2530 with `IntervalTrigger` or `CronTrigger`.
5. If it emits signals, route them through `signal_manager.log_signal()` so they enter the win-rate ledger.
6. Add an entry here describing what it does, what it reads, what it writes.
