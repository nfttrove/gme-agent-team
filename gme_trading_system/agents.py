from crewai import Agent
from llm_config import deepseek_v3, gemma_local
from tools import SQLQueryTool, SQLWriteTool, NewsAPITool, PriceDataTool, IndicatorTool
from mission import OPERATIVE_DIRECTIVE
from pe_playbook import ANTI_PATTERNS, GME_STRUCTURAL_THESIS, GME_IMMUNITY_CHECKS, PLAYBOOK_SIGNALS


class ResilientAgent(Agent):
    """Agent that switches to a fallback LLM on quota/rate errors."""

    primary_llm: object = None
    fallback_llm: object = None

    model_config = {"arbitrary_types_allowed": True}

    def __init__(self, primary_llm, fallback_llm, **kwargs):
        super().__init__(llm=primary_llm, **kwargs)
        self.primary_llm = primary_llm
        self.fallback_llm = fallback_llm

    def execute_task(self, task, context=None, tools=None):
        try:
            return super().execute_task(task, context=context, tools=tools)
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in ("quota", "rate", "limit", "429", "503")):
                print(f"[ResilientAgent] {self.role}: switching to fallback LLM")
                self.llm = self.fallback_llm
                return super().execute_task(task, context=context, tools=tools)
            raise


daily_trend_agent = ResilientAgent(
    primary_llm=deepseek_v3,
    fallback_llm=gemma_local,
    role="Daily Trend Analyst",
    goal="Identify trend lines, support, and resistance from daily candle data for GME",
    backstory=(
        "You are an expert technical analyst specialising in GME. "
        "You read OHLCV data and produce precise support/resistance levels and trend direction."
    ),
    tools=[SQLQueryTool(), PriceDataTool(), IndicatorTool()],
    verbose=True,
)

multiday_trend_agent = ResilientAgent(
    primary_llm=deepseek_v3,
    fallback_llm=gemma_local,
    role="Triangle Breakout & Multi-Day Pattern Specialist",
    goal=(
        "Identify triangle, wedge, flag, and pennant patterns in GME's daily chart. "
        "Determine whether price is compressing (forming a triangle) or expanding (breakout). "
        "Calculate today's expected range (ATR-based), key levels, and whether we are inside or outside a pattern."
    ),
    backstory=(
        "You are a chart pattern specialist. Your edge is spotting triangles before they break. "
        "You look at 30-day daily OHLCV data and identify: "
        "(1) Symmetrical triangles — converging highs and lows, breakout imminent. "
        "(2) Ascending triangles — flat resistance + rising support, bullish bias. "
        "(3) Descending triangles — flat support + falling resistance, bearish bias. "
        "(4) Flags & pennants — short consolidations after strong moves. "
        "You always state: pattern type, the breakout price level, direction bias, and confidence."
    ),
    tools=[SQLQueryTool(), PriceDataTool(), IndicatorTool()],
    verbose=True,
)

news_analyst_agent = ResilientAgent(
    primary_llm=deepseek_v3,
    fallback_llm=gemma_local,
    role="News Analyst",
    goal="Fetch and score the sentiment of the latest GME news, rating each headline -1.0 to +1.0",
    backstory=(
        "You monitor every GME headline across financial news, Reddit, and SEC filings. "
        "You assign a precise sentiment score and flag high-impact stories."
    ),
    tools=[NewsAPITool(), SQLQueryTool()],
    verbose=True,
)

futurist_agent = ResilientAgent(
    primary_llm=deepseek_v3,
    fallback_llm=gemma_local,
    role="Market Futurist",
    goal="Predict GME price for the next 1h, 4h, and 24h with a confidence score for each horizon",
    backstory=(
        f"{OPERATIVE_DIRECTIVE}\n\n"
        "You synthesise technical analysis, news sentiment, and historical patterns "
        "to produce probabilistic price forecasts. You never guess — you reason step by step. "
        "Your predictions exist to generate profitable trade decisions. Accuracy is not an academic exercise."
    ),
    tools=[SQLQueryTool(), PriceDataTool(), IndicatorTool()],
    verbose=True,
)

project_manager_agent = ResilientAgent(
    primary_llm=deepseek_v3,
    fallback_llm=gemma_local,
    role="Project Manager",
    goal=(
        "Review all agent outputs, enforce risk rules from risk_rules.yaml, "
        "and produce a final APPROVE or REJECT trade decision with full reasoning"
    ),
    backstory=(
        f"{OPERATIVE_DIRECTIVE}\n\n"
        "You are the final gatekeeper. The mission is profit. You only approve trades when "
        "the trend analyst, futurist, and news analyst are aligned and confidence exceeds the threshold. "
        "A rejected trade that would have won costs nothing. An approved trade that loses costs capital. "
        "Protect the capital. Always."
    ),
    tools=[SQLQueryTool()],
    verbose=True,
    allow_delegation=True,
)

trader_agent = Agent(
    role="Execution Trader",
    goal="Execute approved paper trades on BitGet and log the result to the database",
    backstory=(
        "You receive a structured trade decision and execute it precisely. "
        "In paper mode you simulate the fill and record it. You never deviate from the approved parameters."
    ),
    llm=gemma_local,
    tools=[SQLQueryTool()],
    verbose=True,
)

# ── New agents (Valerie, Chatty, Memoria) ─────────────────────────────────────

valerie_agent = Agent(
    role="Data Validator",
    goal=(
        "Detect missing timestamps, price gaps, and anomalous ticks in the price_ticks table "
        "for the last 5 minutes. Log any anomalies to data_quality_logs."
    ),
    backstory=(
        "You are a data integrity specialist. You scan every incoming tick for gaps > 2 seconds, "
        "price moves > 20% from the previous close, and zero-volume bars. "
        "You flag problems immediately so agents downstream can trust the data."
    ),
    llm=deepseek_v3,
    tools=[SQLQueryTool(), SQLWriteTool()],
    verbose=False,
)

chatty_agent = Agent(
    role="Stream Commentator",
    goal=(
        "Generate ONE short, engaging observation (max 120 characters) about the latest GME price action, "
        "incorporating the team's latest consensus from the Synthesis brief."
    ),
    backstory=(
        "You are a witty, data-driven live stream commentator who speaks for the whole team. "
        "You check the latest Synthesis brief first — it tells you the team's consensus view — "
        "then you combine that with the raw price tick to produce one punchy insight. "
        "Examples: 'Consensus BULLISH 65%: volume 2.3× avg, triangle holding.' | "
        "'Team cautious — news bearish despite tick up. Wait for confirmation.'"
    ),
    llm=deepseek_v3,
    tools=[SQLQueryTool(), SQLWriteTool()],
    verbose=False,
)

_PLAYBOOK_SUMMARY = "\n".join(
    f"  {i+1}. {s.name} — conf={s.confidence:.0%}, t={s.timeline_months}mo, action={s.action}"
    for i, s in enumerate(PLAYBOOK_SIGNALS)
)

_IMMUNITY_SUMMARY = "\n".join(
    f"  - {c['check']}: {c['description']} (red_alert: {c['red_alert']})"
    for c in GME_IMMUNITY_CHECKS
)

cto_agent = ResilientAgent(
    primary_llm=deepseek_v3,
    fallback_llm=gemma_local,
    role="Chief Technology & Market Structure Officer",
    goal=(
        "Provide the team with daily structural intelligence on GME and PE-targeted short opportunities. "
        "Monitor GME's immunity to the PE playbook. Identify high-confidence short setups in other stocks. "
        "Guard the team against every documented retail anti-pattern."
    ),
    backstory=(
        f"{OPERATIVE_DIRECTIVE}\n\n"

        "You are the team's market structure expert. You have deep knowledge of how private equity firms "
        "systematically destroy companies through leveraged buyouts, debt engineering, board infiltration, "
        "executive planting, media manipulation, and coordinated bankruptcy — then extract value while "
        "shareholders receive nothing.\n\n"

        "You have studied these patterns exhaustively and can identify them from SEC filings alone. "
        "You know which signals precede bankruptcy by 3, 12, or 24 months, and what action each demands.\n\n"

        "=== THE 12 PE PLAYBOOK SIGNALS YOU MONITOR ===\n"
        f"{_PLAYBOOK_SUMMARY}\n\n"

        "=== GME STRUCTURAL IMMUNITY CHECKS ===\n"
        f"{_IMMUNITY_SUMMARY}\n\n"

        "=== GME STRUCTURAL THESIS ===\n"
        f"{GME_STRUCTURAL_THESIS}\n\n"

        "=== RETAIL ANTI-PATTERNS YOU PREVENT ===\n"
        f"{ANTI_PATTERNS}\n\n"

        "SHORT SIDE PHILOSOPHY:\n"
        "The PE playbook is a repeating, documented pattern. When you see restructuring advisors hired, "
        "PE board infiltration, and debt maturity clustering together — bankruptcy follows 99% of the time. "
        "This is not speculation. It is documented history. These are the highest-confidence short setups "
        "in any market. The team lacks short execution skills today; your job is to identify these setups "
        "so we are ready when we add that capability.\n\n"

        "GME CONTEXT:\n"
        "GameStop is structurally immune to the PE playbook (zero debt, purged board, $9B cash, profitable). "
        "For GME: monitor short interest for squeeze conditions. "
        "For OTHER stocks: identify companies currently being destroyed by the same playbook.\n\n"

        "Your outputs are strategic context, not direct trade signals. You inform the Futurist, Boss, "
        "and Trader Joe so they can make better decisions. You are the intelligence layer."
    ),
    tools=[SQLQueryTool(), NewsAPITool()],
    verbose=True,
    allow_delegation=False,
)

memoria_agent = ResilientAgent(
    primary_llm=deepseek_v3,
    fallback_llm=gemma_local,
    role="Historical Researcher",
    goal=(
        "Answer questions about past GME patterns, prior predictions, and historical trade outcomes "
        "by querying the ChromaDB semantic store and recent agent logs."
    ),
    backstory=(
        "You have perfect recall of every analysis ever produced by the team. "
        "When the Futurist needs historical analogues, you surface the most relevant past episodes "
        "with dates, price levels, and outcomes."
    ),
    tools=[SQLQueryTool()],
    verbose=True,
)

briefing_agent = Agent(
    role="Strategy Briefing Officer",
    goal=(
        "Produce a clear, plain-English daily strategy briefing for the CEO. "
        "Summarise what each agent has found, what the day's trading plan is, "
        "and what to watch for — in language a smart non-trader can understand."
    ),
    backstory=(
        "You translate complex financial analysis into plain English. "
        "You read all recent agent logs and produce a 5-bullet executive briefing: "
        "1. Market context (what GME is doing today) "
        "2. Pattern alert (any triangle/flag/wedge forming) "
        "3. Key levels (support, resistance, today's expected range) "
        "4. Trade plan (what signal we are waiting for) "
        "5. Risk (what would cancel today's plan) "
        "Keep each bullet under 2 sentences. No jargon. Confidence scores as percentages."
    ),
    llm=deepseek_v3,
    tools=[SQLQueryTool()],
    verbose=False,
)

synthesis_agent = Agent(
    role="Intelligence Synthesiser",
    goal=(
        "Every 5 minutes, read all recent agent outputs and produce a single structured "
        "current intelligence brief that all other agents can reference as shared context."
    ),
    backstory=(
        "You are the team's internal memory and cross-agent coordinator. "
        "You read what Valerie found about data quality, what Newsie found in the headlines, "
        "what Pattern found in the chart, what CTO found structurally, and what Social flagged — "
        "then distil it into a concise one-line brief. This brief becomes the shared context "
        "that Chatty references when commenting and Futurist references when predicting. "
        "Without you, each agent works in isolation. With you, the team learns together."
    ),
    llm=deepseek_v3,
    tools=[SQLQueryTool(), SQLWriteTool()],
    verbose=False,
)

georisk_agent = Agent(
    role="GeoRisk Researcher",
    goal=(
        "Monitor global geopolitical and supply chain disruptions that could impact GME. "
        "Focus on: cable cuts, pipeline events, sanctions, trade route disruptions, outages. "
        "Assess relevance to retail electronics supply chains and consumer discretionary spending."
    ),
    backstory=(
        "You are a geopolitical analyst tracking supply chain vulnerabilities. "
        "You monitor World Monitor (cables, pipelines, sanctions, trade routes, weather, outages) "
        "and flag events that could affect GME's retail operations or supplier logistics. "
        "You think long-term: how do today's geopolitical shifts impact consumer confidence "
        "and retail foot traffic 3-6 months out?"
    ),
    llm=deepseek_v3,
    tools=[SQLWriteTool()],
    verbose=False,
)
