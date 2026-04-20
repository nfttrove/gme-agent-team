# GME Agent Team

An autonomous multi-agent AI system for continuous GameStop (GME) stock analysis, prediction, and strategic decision-making using CrewAI.

## Overview

**11 specialized agents** working in coordinated cycles every 30 minutes to:
- Validate market data (Valerie)
- Analyze technical patterns (Pattern)
- Track sentiment and news (Newsie)
- Identify trend reversals (Trendy)
- Generate price predictions (Futurist)
- Monitor key investor positions (CTO)
- Synthesize consensus intelligence (Synthesis)
- Provide real-time commentary (Chatty)
- And more...

Agents share a persistent SQLite memory, learn from each other's outputs, and escalate high-confidence signals via Telegram alerts.

## Quick Start

### Prerequisites
- Python 3.11+
- API keys for: DeepSeek, Google (Gemini), Finnhub, Supabase, IBKR, Telegram
- Optional: Ollama (for local embedding models)

### Setup

1. **Clone and enter the directory**
   ```bash
   git clone https://github.com/nfttrove/gme-agent-team.git
   cd gme-agent-team
   ```

2. **Create virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r gme_trading_system/requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp gme_trading_system/.env.example .env
   ```
   Then edit `.env` with your API keys (never commit `.env`):
   ```
   DEEPSEEK_API_KEY=sk-...
   GOOGLE_API_KEY=...
   FINNHUB_API_KEY=...
   SUPABASE_URL=https://...
   SUPABASE_KEY=...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```

5. **Initialize database**
   ```bash
   cd gme_trading_system
   python orchestrator.py
   ```
   First run creates `agent_memory.db` and runs a warm-up cycle.

### Run the System

**Start the orchestrator** (runs in background, executes every 30 min):
```bash
cd gme_trading_system
python orchestrator.py
```

**View dashboard** (React UI with Supabase Realtime live updates):
```bash
cd dashboard
cp .env.local.example .env.local
# Edit .env.local with VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY
npm run dev
# Opens at http://localhost:5173
```

**Legacy Streamlit dashboard** (still available for debugging):
```bash
cd gme_trading_system
python dashboard.py
# Opens at http://localhost:8501
```

**View logs**:
```bash
tail -f orchestrator.log
```

**Stop the system**:
```bash
pkill -f orchestrator.py
```

## React Dashboard (Realtime)

The modern React dashboard displays all agent activity with **live Supabase Realtime subscriptions** (no polling).

### Setup

```bash
cd dashboard
cp .env.local.example .env.local
```

Edit `.env.local` with your Supabase credentials:
```
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Get these from [Supabase dashboard](https://app.supabase.com/project/[PROJECT_ID]/settings/api).

### Run

```bash
npm run dev
# Opens at http://localhost:5173
```

### Pages

| Page | Data Source | Update Rate |
|------|-------------|------------|
| **Console** | agent_logs | Realtime |
| **Price** | daily_candles, price_ticks | Realtime |
| **Options** | options_snapshots | Realtime |
| **Trades** | trade_decisions | Realtime |
| **CTO** | structural_signals, short_watchlist | Realtime |
| **Social** | social_posts | Realtime |
| **Predictions** | predictions | Realtime |
| **Logs** | agent_logs (filtered) | Realtime |
| **Quality** | data_quality_logs, performance_scores, strategy_history | Realtime |

All data syncs from SQLite → Supabase every 30 seconds via `supabase_sync.py`.

## Agent Roles

| Agent | Cycle | Task |
|-------|-------|------|
| **Valerie** | 1 min | Fetch price/volume, validate data quality |
| **Chatty** | 30 sec | Real-time price commentary (Slack/Telegram-ready) |
| **Newsie** | 30 min | News sentiment from Supabase edge function |
| **Pattern** | 2 hrs | Identify chart patterns (triangles, flags, wedges) |
| **Trendy** | 4 hrs + 8 PM | Detect trend reversals, compute oscillators |
| **Futurist** | 3 hrs | Predict next candle; self-calibrate vs actuals |
| **CTO** | Daily 8 AM | Strategic brief: macro context + key investor intel |
| **Synthesis** | 5 min | Read all recent outputs, output one-line consensus |
| **Sentiment** | 30 min | Aggregate bullish/bearish signals |
| **Risk** | Hourly | Position sizing, drawdown alerts |
| **Strategist** | 2 hrs | Trading signals (buy/sell/hold confidence) |

## Architecture

### Core Components

- **`orchestrator.py`** — Scheduler (APScheduler), kick-off coordinator, persistence
- **`agents.py`** — Agent definitions with role, goal, backstory, LLM config
- **`tasks.py`** — Task descriptions (what agents should do each cycle)
- **`tools.py`** — Data tools (SQL queries, news API, price feeds, indicators)
- **`indicators.py`** — Technical indicators (EMA, RSI, ATR, VWAP, etc.)
- **`sec_scanner.py`** — SEC EDGAR monitoring (13F holdings, insider filings)
- **`dashboard/`** — React + TypeScript UI (Tailwind CSS, Recharts, Supabase Realtime)
- **`dashboard.py`** — Legacy Streamlit UI (still available for debugging)
- **`llm_config.py`** — LLM routing (DeepSeek, Gemini Flash, Gemini Pro)
- **`agent_memory.db`** — SQLite: agent outputs, predictions, trade history, logs

### Data Flow

1. **Orchestrator** starts a scheduled cycle
2. **Each agent's task** runs in CrewAI Crew (potentially parallel)
3. **Tools** fetch live data (price, news, SEC filings, indicators)
4. **Agent outputs** written to SQLite `agent_logs` table with task_type, timestamp, score
5. **Supabase Sync** mirrors SQLite tables to Supabase every 30 seconds (10 tables)
6. **Dashboard** (React) subscribes to Supabase Realtime for live updates
7. **Synthesis agent** reads recent outputs, produces consensus brief
8. **Alerts** broadcast to Telegram if confidence > threshold

### Model Selection

- **DeepSeek v3** — High-reasoning tasks (CTO, Strategist, Pattern analysis)
- **Gemini 2.5 Flash** — Fast synthesis (Synthesis agent, time-sensitive)
- **Gemini 2.5 Pro** — Backup for complex reasoning
- **Local embeddings** — Via Ollama (optional; falls back to sentence-transformers)

## Key Features

### Continuous Learning
- Agents read past outputs before deciding (context accumulation)
- Futurist compares predictions to actuals and notes calibration drift
- Synthesis brief serves as shared context all agents reference

### Robust Data Sourcing
- **Price**: Yahoo Finance fallback if DB stale
- **News**: Supabase edge function (aggregates 4 sources) → Finnhub fallback
- **SEC Filings**: Direct EDGAR XML parsing (13F, SC 13D/4)
- **Indicators**: Pre-computed (EMA, RSI, ATR, VWAP) in `indicators.py`

### Escalation & Alerts
- High-confidence signals (Strategist score > 80%) → Telegram alert
- Extreme volatility (>3σ move) → CTO brief escalation
- Unusual insider activity → Investor intel alert

### Database Persistence
- All outputs logged with task_type, agent_name, timestamp, metadata
- Queryable via SQL tool (agents can self-reflect on past predictions)
- Automatic cleanup (configurable age-based retention)

## Configuration

Edit `.env` to customize:

```bash
# LLM providers
DEEPSEEK_API_KEY=your_key
GOOGLE_API_KEY=your_key

# Data sources
FINNHUB_API_KEY=your_key
SUPABASE_URL=https://...
SUPABASE_KEY=your_key

# Trading & alerts
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
IBKR_ACCOUNT=your_account

# Optional
OLLAMA_BASE_URL=http://localhost:11434
SEC_USER_AGENT=YourApp/1.0
```

## Troubleshooting

### "No module named 'supabase'"
```bash
source venv/bin/activate
pip install supabase
```

### Agents not running
Check if orchestrator is still alive:
```bash
ps aux | grep orchestrator.py
tail -f orchestrator.log
```

Restart:
```bash
pkill -9 -f orchestrator.py
python orchestrator.py
```

### Dashboard shows stale data
Manually trigger a cycle:
```bash
python -c "from orchestrator import run_investor_intel_scan, run_synthesis, run_futurist_cycle; run_investor_intel_scan(); run_synthesis(); run_futurist_cycle()"
```

### Database growing too large
Check size:
```bash
ls -lh agent_memory.db
sqlite3 agent_memory.db "SELECT COUNT(*) FROM agent_logs;"
```

To prune old logs (keep last 30 days):
```bash
sqlite3 agent_memory.db "DELETE FROM agent_logs WHERE timestamp < datetime('now', '-30 days');"
```

## Deployment

### Local (Development)
Run `orchestrator.py` in a tmux/screen session or nohup:
```bash
nohup python gme_trading_system/orchestrator.py > orchestrator.log 2>&1 &
```

### Docker (Future)
A `Dockerfile` and `docker-compose.yml` are planned for containerized deployment.

### Cloud (Future)
Planned integrations: AWS Lambda (hourly agents), Supabase Cloud Functions (news aggregation).

## Testing

```bash
# Validate LLM connectivity
python gme_trading_system/test_models.py

# Manual cycle (for debugging)
cd gme_trading_system
python -c "from orchestrator import run_futurist_cycle; run_futurist_cycle()"
```

## Performance Notes

- **Cycle time**: ~2–5 min per full 11-agent run (depends on API latency)
- **Memory**: ~200 MB baseline (SQLite grows ~10 MB/month)
- **Cost**: ~$0.50–$2.00/day (mostly DeepSeek inference; Gemini Flash is cheap)

## Contributing

This is a personal research system. Feel free to fork and adapt.

## License

MIT

---

**Status**: Active development. Agents running 24/7 in production mode since Jan 2026.

**Next Milestones**:
- [x] Web dashboard (React + TypeScript with Supabase Realtime)
- [ ] Fine-tune prediction models on historical GME patterns
- [ ] Add portfolio backtesting engine
- [ ] Integrate live broker orders (IBKR)
- [ ] Cloud deployment (Docker + Lambda)
- [ ] Test Supabase schema on cloud instance
