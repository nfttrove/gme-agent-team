from crewai import Task
from mission import MISSION_FULL, OPERATIVE_DIRECTIVE
from agents import (
    daily_trend_agent,
    multiday_trend_agent,
    news_analyst_agent,
    futurist_agent,
    project_manager_agent,
    valerie_agent,
    chatty_agent,
    memoria_agent,
    cto_agent,
    briefing_agent,
    synthesis_agent,
    georisk_agent,
)

daily_trend_task = Task(
    description=(
        "Query the daily_candles table for the last 10 days of GME data. "
        "Calculate support and resistance levels, identify the current trend direction "
        "(bullish/bearish/sideways), and rate trend strength 0–1. "
        "Output JSON: {support, resistance, trend_direction, strength, notes}"
    ),
    expected_output='{"support": 20.50, "resistance": 24.80, "trend_direction": "bullish", "strength": 0.72, "notes": "..."}',
    agent=daily_trend_agent,
)

multiday_trend_task = Task(
    description=(
        "Query the daily_candles table for the last 30 days of GME data. "
        "Identify any multi-day chart patterns (flags, wedges, breakouts, reversals). "
        "Summarise momentum over the last 5 and 10 days. "
        "Output JSON: {pattern, momentum_5d, momentum_10d, signal, notes}"
    ),
    expected_output='{"pattern": "bull_flag", "momentum_5d": 0.6, "momentum_10d": 0.3, "signal": "bullish", "notes": "..."}',
    agent=multiday_trend_agent,
    context=[daily_trend_task],
)

news_task = Task(
    description=(
        "Fetch the latest 10 GME news headlines using the News API tool. "
        "Score each headline from -1.0 (very bearish) to +1.0 (very bullish). "
        "Compute an overall composite sentiment score. "
        "Flag any headline that could cause a >5% price move. "
        "Output JSON: {headlines: [{text, score}], composite_score, high_impact_flag, summary}"
    ),
    expected_output='{"composite_score": 0.45, "high_impact_flag": false, "summary": "Mildly positive sentiment..."}',
    agent=news_analyst_agent,
)

futurist_task = Task(
    description=(
        "Using the daily trend analysis, multi-day pattern, and news sentiment from previous tasks, "
        "predict GME price for the next 1h, 4h, and 24h.\n\n"

        "BEFORE making predictions — review your own recent accuracy:\n"
        "  SELECT timestamp, content FROM agent_logs WHERE agent_name='Futurist' "
        "  AND task_type IN ('full_cycle','gate_check') ORDER BY timestamp DESC LIMIT 5\n"
        "  Also check the current price:\n"
        "  SELECT close, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1\n\n"

        "Self-reflect: if your recent predictions were bullish but price fell, widen your uncertainty band. "
        "If your recent predictions were accurate, you may tighten it. State your calibration note.\n\n"

        "Also read the team's current consensus:\n"
        "  SELECT content FROM agent_logs WHERE agent_name='Synthesis' ORDER BY timestamp DESC LIMIT 1\n\n"

        "For each horizon provide: predicted_price, confidence (0–1), brief reasoning. "
        "State trade bias: BUY, SELL, or HOLD. "
        "Output JSON: {1h: {price, confidence, reasoning}, 4h: {...}, 24h: {...}, bias, overall_confidence, "
        "self_reflection: '<calibration note>'}"
    ),
    expected_output=(
        '{"1h": {"price": 22.10, "confidence": 0.65, "reasoning": "Triangle support holding"}, '
        '"bias": "BUY", "overall_confidence": 0.68, '
        '"self_reflection": "Last 4h prediction was +1.2% off actual — maintaining confidence band."}'
    ),
    agent=futurist_agent,
    context=[daily_trend_task, multiday_trend_task, news_task],
)

manager_task = Task(
    description=(
        "Review all previous agent outputs. Cross-check for contradictions. "
        "Apply the risk rules: min_confidence=0.70, require_trend_alignment=true, min_agents_agree=2. "
        "Emit a signal for team review. Specify: action (BUY/SELL), "
        "quantity (max $500), entry_price, stop_loss (3%), take_profit (6%), confidence score. "
        "Output JSON: {action, quantity_usd, entry_price, stop_loss, take_profit, confidence, reasoning}. "
        "Signal will be logged to signal_alerts table and sent to team via Telegram for manual execution decision."
    ),
    expected_output='{"action": "BUY", "quantity_usd": 200, "entry_price": 22.10, "stop_loss": 21.44, "take_profit": 23.43, "confidence": 0.78, "reasoning": "..."}',
    agent=project_manager_agent,
    context=[daily_trend_task, multiday_trend_task, news_task, futurist_task],
)

# ── New tasks ──────────────────────────────────────────────────────────────────

validate_data_task = Task(
    description=(
        "Run these SQL checks on price_ticks for the last 5 minutes:\n"
        "1. SELECT COUNT(*) ticks, MAX(timestamp) latest FROM price_ticks WHERE symbol='GME' AND timestamp > datetime('now','-5 minutes')\n"
        "2. Find any gap > 120 seconds between consecutive ticks.\n"
        "3. Find any close price that differs > 20% from the previous close.\n"
        "Report findings as JSON: {tick_count, latest_timestamp, gaps_found, outliers_found, status}. "
        "Insert a row into data_quality_logs with the result."
    ),
    expected_output='{"tick_count": 60, "latest_timestamp": "...", "gaps_found": 0, "outliers_found": 0, "status": "ok"}',
    agent=valerie_agent,
)

commentary_task = Task(
    description=(
        "Step 1 — Read the latest team intelligence brief (Synthesis):\n"
        "  SELECT content, timestamp FROM agent_logs WHERE agent_name='Synthesis' ORDER BY timestamp DESC LIMIT 1\n\n"
        "Step 2 — Query the latest price tick:\n"
        "  SELECT close, volume, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1\n\n"
        "Step 3 — Query 10-minute average volume:\n"
        "  SELECT AVG(volume) avg_vol FROM price_ticks WHERE symbol='GME' AND timestamp > datetime('now','-10 minutes')\n\n"
        "Generate ONE insight (max 120 chars) that combines the team's current consensus with the latest price action. "
        "If Synthesis shows BULLISH consensus, lean bullish. If BEARISH, lean bearish. If no synthesis yet, base on price only. "
        "Examples: 'Consensus BULLISH 65%: vol 2.3× avg — triangle holding.' | 'Team cautious: news drag, wait for $24.80 break.' "
        "Insert it into stream_comments: INSERT INTO stream_comments (timestamp, comment) VALUES (datetime('now'), '<comment>'). "
        "Return just the comment text."
    ),
    expected_output="Consensus BULLISH 65%: vol 2.3× avg — triangle holding at $24.20.",
    agent=chatty_agent,
)

historia_task = Task(
    description=(
        "The Futurist needs historical analogues. Query agent_logs for the last 30 days of futurist predictions: "
        "SELECT timestamp, content FROM agent_logs WHERE agent_name='FuturistAgent' ORDER BY timestamp DESC LIMIT 20\n"
        "Also query predictions: SELECT * FROM predictions ORDER BY timestamp DESC LIMIT 20\n"
        "Find the 3 most relevant past episodes that match current conditions (bullish bias, price ~$24-25 range). "
        "Return a structured summary: {similar_episodes: [{date, entry_price, outcome_24h, pct_change}], key_insight}"
    ),
    expected_output='{"similar_episodes": [{"date": "2024-01-15", "entry_price": 23.50, "outcome_24h": 25.10, "pct_change": "+6.8%"}], "key_insight": "..."}',
    agent=memoria_agent,
)

# ── CTO structural intelligence ───────────────────────────────────────────────

cto_daily_brief_task = Task(
    description=(
        "You are the Chief Market Structure Officer. Produce today's structural intelligence brief.\n\n"

        "STEP 1 — GME IMMUNITY STATUS\n"
        "Query the structural_signals table for any recent GME alerts:\n"
        "  SELECT * FROM structural_signals WHERE ticker='GME' AND filing_date >= date('now','-7 days') ORDER BY confidence DESC\n"
        "Check each of the 6 GME immunity conditions (debt-free, cash>$1B, PE-free board, Cohen control, "
        "no restructuring advisor, profitable). Based on the signals table and your knowledge, "
        "rate each GREEN / YELLOW / RED.\n\n"

        "STEP 2 — SHORT WATCHLIST\n"
        "Query structural_signals for non-GME companies with recent signals:\n"
        "  SELECT ticker, signal_name, confidence, action, timeline_months, headline "
        "  FROM structural_signals WHERE ticker != 'GME' AND filing_date >= date('now','-30 days') "
        "  ORDER BY confidence DESC LIMIT 20\n"
        "Score each company using the PE playbook signal weightings. "
        "List top 3 short candidates with their composite score and recommended action.\n\n"

        "STEP 3 — ANTI-PATTERN ALERTS\n"
        "Review today's news (query news_analysis for last 24h) and flag any stories where the team "
        "might be tempted to commit a documented anti-pattern:\n"
        "  SELECT headline, sentiment_score, summary FROM news_analysis ORDER BY timestamp DESC LIMIT 10\n"
        "If you see coordinated negative sentiment on GME without fundamental basis → flag as CONTRARIAN opportunity.\n"
        "If you see positive news on a company with known PE board infiltration → flag as PUMP-AND-DUMP risk.\n\n"

        "STEP 4 — KEY INVESTOR INTELLIGENCE\n"
        "Query the latest investor intelligence logged by the SEC scanner:\n"
        "  SELECT content FROM agent_logs WHERE task_type='investor_intel' ORDER BY timestamp DESC LIMIT 1\n"
        "Report:\n"
        "  - Ryan Cohen: any new RC Ventures SEC filings in last 30 days? (new 13D = new activist position — CRITICAL)\n"
        "  - Ryan Cohen's known non-GME positions: BABA ($1B activist, pushing buybacks), "
        "AAPL/WFC/NFLX/C (large passive). Any correlation to GME movement?\n"
        "  - Michael Burry: personally owns GME (not in Scion 13F — personal account). "
        "Scion's latest 13F holdings (check investor_intel log). Any portfolio rotation relevant to GME?\n"
        "  - Both Cohen (BABA) and Burry (Q4 2024: BABA, Baidu, JD) showed conviction in Chinese tech. "
        "This overlap in BABA sentiment is structurally significant for understanding macro positioning.\n\n"

        "STEP 5 — STRUCTURAL BIAS FOR TODAY\n"
        "State whether the structural context is BULLISH, BEARISH, or NEUTRAL for GME today "
        "based on immunity status, news environment, investor positions, and any short interest data available.\n\n"

        "Output format:\n"
        "GME_IMMUNITY: {debt_free: GREEN, cash: GREEN, board: GREEN, cohen: GREEN, no_cro: GREEN, profitable: GREEN}\n"
        "OVERALL_GME_STATUS: GREEN/YELLOW/RED\n"
        "TOP_SHORT_CANDIDATES: [{ticker, score, action, key_signals}]\n"
        "ANTI_PATTERN_ALERTS: [list]\n"
        "KEY_INVESTORS: {rc_ventures: [alert or 'no new filings'], scion_latest: [top position]}\n"
        "STRUCTURAL_BIAS: BULLISH/BEARISH/NEUTRAL\n"
        "BRIEF: [2-3 sentence strategic summary for the team]"
    ),
    expected_output=(
        "GME_IMMUNITY: {debt_free: GREEN, cash: GREEN, board: GREEN, cohen: GREEN, no_cro: GREEN, profitable: GREEN}\n"
        "OVERALL_GME_STATUS: GREEN\n"
        "TOP_SHORT_CANDIDATES: [{ticker: AMC, score: 85, action: SHORT, key_signals: [pe_board_infiltration, debt_to_equity_explosion]}]\n"
        "ANTI_PATTERN_ALERTS: []\n"
        "STRUCTURAL_BIAS: BULLISH\n"
        "BRIEF: GME remains structurally immune — zero debt, $9B cash, clean board. No new PE signals detected. "
        "AMC remains the primary short candidate with 3 concurrent PE playbook signals active."
    ),
    agent=cto_agent,
)

cto_structural_scan_task = Task(
    description=(
        "You are the Chief Market Structure Officer. Run a weekly deep structural scan.\n\n"

        "STEP 1 — EDGAR SIGNAL REVIEW\n"
        "Query all structural_signals from the last 7 days:\n"
        "  SELECT ticker, signal_name, confidence, action, headline, filing_date "
        "  FROM structural_signals WHERE filing_date >= date('now','-7 days') ORDER BY confidence DESC\n"
        "For each signal, state whether it's a new development or continuation of a known pattern.\n\n"

        "STEP 2 — PE PLAYBOOK STAGE ASSESSMENT\n"
        "For any company with 2+ signals, determine which stage of the PE playbook they are in:\n"
        "  Stage 1 (Setup): board infiltration, overexpansion\n"
        "  Stage 2 (Loading): sale-leaseback, debt loading\n"
        "  Stage 3 (Pressure): media attacks, employee cuts, activist coordination\n"
        "  Stage 4 (Endgame): debt maturity cliff, covenant violations\n"
        "  Stage 5 (Extraction): restructuring advisor hired, CRO appointed\n"
        "  Stage 5 = EXIT ALL EQUITY POSITIONS. Zero recovery expected.\n\n"

        "STEP 3 — SHORT OPPORTUNITY RANKING\n"
        "Produce a ranked short opportunity list:\n"
        "  SELECT ticker, GROUP_CONCAT(signal_name) as signals, MIN(timeline_months) as urgency "
        "  FROM structural_signals WHERE filing_date >= date('now','-90 days') "
        "  GROUP BY ticker ORDER BY COUNT(*) DESC\n"
        "Rank by: (number of signals × average confidence) / timeline_months\n"
        "Higher = more urgent short opportunity.\n\n"

        "STEP 4 — CAPABILITY GAP ASSESSMENT\n"
        "State what short-side execution capabilities the team currently lacks and what is needed "
        "to capture these opportunities. Be specific: which broker, which order types, what risk limits.\n\n"

        "STEP 5 — CTO RECOMMENDATION TO CEO\n"
        "Write a 3-bullet strategic recommendation for the CEO on:\n"
        "  1. Most urgent short opportunity and why\n"
        "  2. GME structural position — hold, add, or reduce\n"
        "  3. One capability to build next to capture the short-side edge\n\n"

        "Output as a structured strategic memo."
    ),
    expected_output=(
        "WEEKLY STRUCTURAL SCAN — [date]\n\n"
        "EDGAR SIGNALS (7 days): 3 new signals across 2 companies...\n"
        "PLAYBOOK STAGE: AMC at Stage 3 (Pressure)...\n"
        "SHORT RANKING: 1. AMC (score 85, urgency HIGH), 2. CONN (score 60, urgency MEDIUM)...\n"
        "CAPABILITY GAPS: Short selling via IBKR margin account, put options...\n"
        "CTO MEMO:\n"
        "  1. AMC: Stage 3 confirmed, 3 signals active, establish short position on next bounce\n"
        "  2. GME: Hold — immunity intact, squeeze conditions building\n"
        "  3. Build: Add IBKR paper trading for short positions to test execution"
    ),
    agent=cto_agent,
)

# ── Daily huddle ───────────────────────────────────────────────────────────────

daily_huddle_task = Task(
    description=(
        f"DAILY TEAM BRIEFING\n\n"
        f"{MISSION_FULL}\n\n"
        "--- BRIEFING AGENDA ---\n"
        "1. Restate the operative directive in your own words (1 sentence).\n"
        "2. Query today's trade decisions: SELECT action, status, pnl FROM trade_decisions WHERE timestamp LIKE date('now')||'%'\n"
        "3. Query today's predictions: SELECT horizon, predicted_price, confidence FROM predictions WHERE timestamp LIKE date('now')||'%'\n"
        "4. State whether the team is on track to generate profit today — YES or NO — and why in one sentence.\n"
        "5. Name ONE thing the team should focus on in the next cycle to improve our edge.\n"
        "Output a clean briefing summary."
    ),
    expected_output=(
        "DIRECTIVE: Make money first, do good with it second.\n"
        "TODAY: 0 trades executed, 2 predictions made (confidence avg 0.67).\n"
        "ON TRACK: YES — gate is holding correctly, no bad trades taken.\n"
        "FOCUS: Improve news sentiment scoring to reduce false positives."
    ),
    agent=project_manager_agent,
)

# ── Synthesis task (every 5 min — cross-agent shared context) ─────────────────

synthesis_task = Task(
    description=(
        "Read all recent agent outputs and produce a one-line structured intelligence brief "
        "that captures the team's current consensus. This brief is the shared context "
        "that Chatty and Futurist will read before producing their own outputs.\n\n"

        "STEP 1 — Query recent agent logs (last 2 hours):\n"
        "  SELECT agent_name, task_type, content, timestamp FROM agent_logs "
        "  WHERE timestamp > datetime('now','-2 hours') ORDER BY timestamp DESC LIMIT 40\n\n"

        "STEP 2 — Query latest price:\n"
        "  SELECT close, volume, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1\n\n"

        "STEP 3 — Extract key signals from each agent:\n"
        "  - Valerie: is the data feed clean or degraded?\n"
        "  - Newsie: composite news sentiment (bullish/bearish/neutral, score)\n"
        "  - Pattern: chart pattern type and breakout bias\n"
        "  - Trendy: trend direction and strength\n"
        "  - Futurist: trade bias (BUY/SELL/HOLD) and overall confidence\n"
        "  - CTO: structural bias (GREEN/YELLOW/RED)\n"
        "  - Social: any recent posts from tracked accounts (or 'none')\n"
        "  - SafetyGate: PASS or BLOCK and why\n\n"

        "STEP 4 — Compute team consensus:\n"
        "  Count bullish signals vs bearish signals. If ≥60% bullish → CONSENSUS: BULLISH. "
        "  If ≥60% bearish → CONSENSUS: BEARISH. Otherwise → NEUTRAL.\n\n"

        "STEP 5 — Write the brief as a single row:\n"
        "  INSERT INTO agent_logs (agent_name, timestamp, task_type, content, status) "
        "  VALUES ('Synthesis', datetime('now'), 'synthesis', '<brief>', 'ok')\n\n"

        "Brief format (one line, pipe-separated):\n"
        "PRICE: $XX.XX [trend] | DATA: [clean/degraded] | NEWS: [sentiment, score] | "
        "PATTERN: [type, bias] | TREND: [direction, strength] | "
        "PREDICTION: [bias, confidence] | STRUCTURAL: [status] | SOCIAL: [alert or none] | "
        "GATE: [PASS/BLOCK] | CONSENSUS: [BULLISH/BEARISH/NEUTRAL] [X]%\n\n"
        "Return the brief text."
    ),
    expected_output=(
        "PRICE: $24.28 flat | DATA: clean | NEWS: bullish 0.45 | "
        "PATTERN: symmetrical_triangle bullish | TREND: bullish 0.72 | "
        "PREDICTION: BUY 0.68 | STRUCTURAL: GREEN | SOCIAL: none | "
        "GATE: BLOCK no_signal | CONSENSUS: BULLISH 65%"
    ),
    agent=synthesis_agent,
)

# ── Daily strategy briefing (ELI5 for CEO) ────────────────────────────────────

daily_briefing_task = Task(
    description=(
        "Produce a plain-English strategy briefing for the CEO. No jargon.\n\n"
        "Step 1 — read the last 5 agent logs:\n"
        "  SELECT agent_name, task_type, content, timestamp FROM agent_logs ORDER BY timestamp DESC LIMIT 5\n\n"
        "Step 2 — read the latest price tick:\n"
        "  SELECT close, volume, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1\n\n"
        "Step 3 — read today's opening price (first price of the day for direction comparison):\n"
        "  SELECT close FROM price_ticks WHERE symbol='GME' AND date(timestamp)=date('now') ORDER BY timestamp ASC LIMIT 1\n\n"
        "Step 4 — read the last safety gate result:\n"
        "  SELECT content FROM agent_logs WHERE agent_name='SafetyGate' ORDER BY timestamp DESC LIMIT 1\n\n"
        "IMPORTANT: To determine if price is rising/falling/sideways TODAY:\n"
        "  - Compare current price (Step 2) to today's opening (Step 3)\n"
        "  - If current > opening + 0.5%, say 'rising'\n"
        "  - If current < opening - 0.5%, say 'falling'\n"
        "  - Otherwise, say 'sideways'\n\n"
        "Then write EXACTLY this format (fill in the blanks):\n\n"
        "📍 MARKET: GME is at $[price]. It is [rising/falling/sideways] today.\n\n"
        "📐 PATTERN: [Describe any triangle, flag or pattern forming in plain English. "
        "If no clear pattern, say what price is doing instead.]\n\n"
        "🎯 KEY LEVELS: Support at $[X] (price bounces here). Resistance at $[Y] (price struggles here). "
        "Today's range: $[low] to $[high].\n\n"
        "⏳ WAITING FOR: [What signal the system needs before it will place a trade. "
        "Explain it like the person has never traded before.]\n\n"
        "⚠️ RISK: [One thing that would stop today's plan. Keep it simple.]\n\n"
        "🔮 CONFIDENCE: [X]% — [one sentence on why the team is or isn't confident today]"
    ),
    expected_output=(
        "📍 MARKET: GME is at $24.28. It is sideways today, moving in a tight $0.50 range.\n\n"
        "📐 PATTERN: The stock is forming a symmetrical triangle — the highs are getting lower and "
        "the lows are getting higher. This is like a coiling spring. A breakout is building.\n\n"
        "🎯 KEY LEVELS: Support at $23.80 (buyers have defended here twice). "
        "Resistance at $24.80 (sellers appear every time price approaches this). "
        "Today's range: $23.90 to $24.50.\n\n"
        "⏳ WAITING FOR: RSI to dip below 45 while price stays above the moving average — "
        "this means the stock has pulled back enough to be a good buy without being in freefall.\n\n"
        "⚠️ RISK: If GME breaks below $23.80 with high volume, the triangle has failed bearishly — "
        "no trade today.\n\n"
        "🔮 CONFIDENCE: 68% — pattern is forming cleanly but volume is thin today."
    ),
    agent=briefing_agent,
)

# ── GeoRisk Intelligence (hourly monitoring) ────────────────────────────────────

georisk_task = Task(
    description=(
        "Monitor https://finance.worldmonitor.app/ for geopolitical events affecting GME.\n\n"
        "Focus on these data layers (check the map for active events):\n"
        "  • Cables: transatlantic fiber cuts (affect supply chain logistics)\n"
        "  • Pipelines: energy disruptions (increase shipping costs)\n"
        "  • Sanctions: new trade restrictions (affect retail import costs)\n"
        "  • Trade Routes: shipping lane blockades (delay inventory)\n"
        "  • Outages: power/internet grid failures (affect retailers' operations)\n"
        "  • Weather: severe storms/events (impact retail foot traffic)\n\n"
        "Steps:\n"
        "1. Scan the map for RED/ORANGE events (active/recent).\n"
        "2. For each event, assess: Is it supply chain, consumer confidence, or shipping related?\n"
        "3. Rate risk: LOW (no immediate impact), MEDIUM (watch closely), HIGH (urgent).\n"
        "4. Write a 2-3 sentence brief to agent_logs (task_type='georisk', status=risk level).\n\n"
        "Format: '[RISK_LEVEL] [Location/Event]: [Impact to GME supply chain or operations]'\n"
        "Examples:\n"
        "  MEDIUM - Red Sea: Yemen attacks disrupt shipping lanes; +5-10% transit time to US ports\n"
        "  LOW - Taiwan Weather: Typhoon warning; chip shortages unlikely to worsen\n"
        "  HIGH - UK Power Outage: National grid failure affecting London port ops; shipping delayed"
    ),
    expected_output=(
        "LOW - No significant supply chain events detected. "
        "Baltic stable, Suez open, US retail weather normal. "
        "Monitor weekend weather in Northeast (potential store impact Monday)."
    ),
    agent=georisk_agent,
)


# ── Factory functions for dynamic task creation (Telegram bot) ────────────────

def make_validate_data_task(agent, tick_count, latest_ts, gaps, outliers):
    """Factory: create a dynamic validate_data_task with live counts."""
    return Task(
        description=(
            f"Review this data quality check result:\n"
            f"  Tick count (last 5 min): {tick_count}\n"
            f"  Latest timestamp: {latest_ts}\n"
            f"  Gaps found: {gaps}\n"
            f"  Outliers found: {outliers}\n\n"
            f"Format your response as JSON: {{'tick_count': {tick_count}, 'latest_timestamp': '{latest_ts}', "
            f"'gaps_found': {gaps}, 'outliers_found': {outliers}, 'status': 'ok' if {gaps} == 0 and {outliers} == 0 else 'degraded'}}"
        ),
        expected_output='{"tick_count": 60, "latest_timestamp": "2026-04-23T14:30:00-04:00", "gaps_found": 0, "outliers_found": 0, "status": "ok"}',
        agent=agent,
    )


def make_synthesis_task(agent, price_str, agent_logs_str):
    """Factory: create a dynamic synthesis_task with live price and agent data."""
    return Task(
        description=(
            f"Produce the team's consensus brief in ONE line.\n\n"
            f"LIVE DATA (use exactly as provided):\n"
            f"  Current price: {price_str}\n"
            f"  Recent agent outputs:\n{agent_logs_str}\n\n"
            f"Output EXACTLY this format (no preamble, no markdown):\n"
            f"PRICE: $XX.XX [direction] | DATA: [clean/degraded] | NEWS: [sentiment, score] | "
            f"TREND: [direction, strength] | PREDICTION: [bias, confidence%] | "
            f"STRUCTURAL: [status] | CONSENSUS: [BULLISH/BEARISH/NEUTRAL] [XX]%"
        ),
        expected_output=(
            "PRICE: $24.28 up | DATA: clean | NEWS: bullish 0.45 | TREND: up 0.72 | "
            "PREDICTION: BUY 0.68 | STRUCTURAL: GREEN | CONSENSUS: BULLISH 65%"
        ),
        agent=agent,
    )
