# GME Agent Team

A cost-optimized, 10-agent CrewAI trading system for real-time GameStop (GME) analysis, powered by local **Gemma 2:9b** (Ollama) with Gemini Flash/Pro fallback.

## Overview

**10 specialized agents** in coordinated cycles (5 min to daily) operating within market hours (07:30–18:00 ET, Mon–Fri):

| Agent | Schedule | Purpose |
|-------|----------|---------|
| **Valerie** | Every 5 min | Data validation (price, volume, OHLC sanity) |
| **Chatty** | Every 5 min | Real-time price action commentary |
| **Newsie** | Every 30 min | News sentiment analysis |
| **Pattern** | Every 2 hours | Multi-day trend analysis + clustering |
| **Trendy** | Every 4 hours + 8 PM ET | Daily trend summaries, end-of-day EOD |
| **Futurist** | Every 2 hours | Price prediction (1h/4h/EOD candles) |
| **GeoRisk** | Every 1 hour | Geopolitical risk scoring |
| **Synthesis** | Every 5 min | Cross-agent consensus brief |
| **Boss** | Daily 9:00 AM ET | Daily mission briefing + yesterday review |
| **CTO** | Daily 9:05 AM ET + Sun 8 AM | PE playbook monitoring, short-side research |

All agents share a persistent **SQLite memory** (16 tables with WAL mode), learn from each other's outputs, and escalate high-confidence signals via **Telegram/Discord**.

## Quick Start

### Prerequisites
- Python 3.12+
- **Ollama** with `gemma2:9b` model running at `localhost:11434`
- API keys: Google (Gemini Flash/Pro fallback), Telegram (alerts), optional Finnhub/NewsAPI
- Node 16+ (for React dashboard)

### Setup

1. **Clone and enter the directory**
   ```bash
   git clone https://github.com/nfttrove/gme-agent-team.git
   cd gme-agent-team
   ```

2. **Start Ollama** (in another terminal)
   ```bash
   ollama serve
   ```
   Verify: `curl http://localhost:11434/api/tags` should list `gemma2:9b`

3. **Create virtual environment**
   ```bash
   python3.12 -m venv venv
   source venv/bin/activate
   ```

4. **Install Python dependencies**
   ```bash
   pip install -r gme_trading_system/requirements.txt
   ```

5. **Configure environment**
   ```bash
   cp gme_trading_system/.env.example .env
   ```
   Edit `.env` with your API keys (never commit `.env`):
   ```bash
   GOOGLE_API_KEY=your-gemini-key        # Required (Gemini Flash/Pro fallback)
   TELEGRAM_BOT_TOKEN=your-bot-token     # Optional (for alerts)
   TELEGRAM_CHAT_ID=your-chat-id         # Optional
   DISCORD_BOT_TOKEN=your-discord-token  # Optional
   FINNHUB_API_KEY=your-finnhub-key      # Optional
   OLLAMA_HOST=http://localhost:11434    # Default; set if using custom Ollama
   ```

6. **Initialize database**
   ```bash
   cd gme_trading_system
   python -m alembic upgrade head
   ```
   Creates `agent_memory.db` with 16 tables (price_ticks, predictions, trade_decisions, agent_logs, etc.) and enables WAL mode.

### Run the System

**Option A: Supervisor (Recommended for production)**
```bash
cd gme_trading_system
supervisord -c supervisord.conf
```
Manages: orchestrator, logger_daemon (Flask :8765), discord_bot, React dashboard (:8000).

Check status:
```bash
supervisorctl status
```

**Option B: Manual (Development)**
```bash
# Terminal 1: Orchestrator (APScheduler main loop)
cd gme_trading_system
python orchestrator.py

# Terminal 2: Logger daemon (TradingView webhooks, Prometheus /metrics)
python logger_daemon.py

# Terminal 3: Telegram bot (interactive fallback LLM)
python telegram_bot.py

# Terminal 4: Discord bot (alerts + slash commands)
python discord_bot.py

# Terminal 5: React dashboard (UI)
cd dashboard
npm install
npm run dev
# Opens at http://localhost:5173
```

**View logs**:
```bash
tail -f orchestrator.log
tail -f logger_daemon.log
```

**Stop the system**:
```bash
supervisorctl shutdown
# OR: pkill -f "orchestrator.py|logger_daemon.py|telegram_bot.py|discord_bot.py"
```

## React Dashboard

Modern React + TypeScript dashboard (Vite, Tailwind, Recharts) running at `http://localhost:5173`.

### Setup

```bash
cd dashboard
npm install
cp .env.local.example .env.local
```

Edit `.env.local` with Flask API server (running at :8000):
```
VITE_API_URL=http://localhost:8000
```

### Run

```bash
npm run dev
```

### Features

- **Real-time agent activity** (5-second refresh)
- **Price chart** (Recharts candlestick + overlay indicators)
- **Agent logs** (filterable by agent/level)
- **Trade decisions** (entry/exit signals with confidence)
- **Predictions** (Futurist's next-candle forecasts)
- **Performance metrics** (agent accuracy, prediction calibration)
- **Dark/light mode** toggle

## Architecture

### Core Components

- **`orchestrator.py`** — APScheduler (cron/interval jobs), 10 agents, cycle coordination
- **`agents.py`** — CrewAI agent definitions (role, goal, backstory; no tool calling)
- **`tasks.py`** — CrewAI task descriptions (what agents output each cycle)
- **`llm_config.py`** — LLM routing: Gemma 2:9b → Gemini Flash (429) → Gemini Pro (complex)
- **`circuit_breaker.py`** — 3-state breakers (CLOSED/OPEN/HALF_OPEN) for 6 external services
- **`market_hours.py`** — `is_market_open()`, `is_active_window()` (07:30–18:00 ET Mon–Fri)
- **`db_maintenance.py`** — WAL mode, nightly backup (3 AM ET), 14-day retention
- **`logger_daemon.py`** — Flask :8765 (TradingView webhook listener, `/metrics` Prometheus)
- **`episodic_integration.py`** — Hooks: `log_futurist_prediction`, `log_manager_trade`, `log_synthesis_brief`
- **`notifier.py`** — Telegram push (circuit-breaker wrapped)
- **`telegram_bot.py`** — Interactive Telegram listener + fallback LLM (`Ollama → Gemini` chain)
- **`discord_bot.py`** — Discord bot (slash commands, alerts)
- **`backtester.py`** — CLI: replay past trades, compute Sharpe/max_drawdown
- **`agent_memory.db`** — SQLite: 16 tables (price_ticks, trade_decisions, predictions, agent_logs, etc.)
- **`alembic/`** — Database migrations (baseline + WAL marker)
- **`models/agent_outputs.py`** — Pydantic: FuturistPrediction, TraderDecision, SynthesisBrief, NewsSignal
- **`tests/test_integration.py`** — 26 integration tests (WAL, backup, Pydantic, circuit breakers, market hours)
- **`dashboard/`** — React frontend (Vite, Tailwind, Recharts)
- **`api_server.py`** — Flask :8000 for React (agent logs, predictions, stats)

### Data Flow

1. **Orchestrator** starts a scheduled cycle (5 min to daily)
2. **Agent task** runs in CrewAI Crew (crew timeout: 180–600s depending on agent)
3. **Agent output** written to SQLite with Pydantic validation
4. **Circuit breaker** wraps external calls (Telegram, Supabase, Finnhub, SEC, NewsAPI, Twitter); open after 5 failures
5. **Episodic hooks** trigger on key events (trade, prediction, synthesis)
6. **Logger daemon** listens for TradingView webhooks at :8765, exports `/metrics` (Prometheus)
7. **React dashboard** polls API server at :8000 every 5s (no Realtime required)
8. **Synthesis agent** reads recent outputs, produces one-line consensus brief
9. **Alerts** broadcast to Telegram/Discord if confidence > threshold

### LLM Model Selection

- **Gemma 2:9b (local Ollama, primary)** — Fast, cost-free inference; no rate limits
- **Gemini 2.5 Flash (fallback)** — Triggered on 429 (rate limit); cheap, fast
- **Gemini Pro (complex reasoning)** — Futurist (prediction) and CTO (PE playbook) only

## Key Features

### Cost-Optimized Inference
- Local Gemma eliminates API costs (~$0/day vs ~$2/day DeepSeek)
- Automatic fallback chain: Gemma → Flash (429) → Pro (complex task)
- No rate limit issues with local model

### Active Window Gating
- 6 agents decorated with `@active_window_required` skip silently outside 07:30–18:00 ET Mon–Fri
- Saves compute/cost when market is closed
- Market hours (NYSE) separate from active window (wider buffer)

### Robust Database
- SQLite with **WAL mode** (concurrent reads)
- Nightly backup (3 AM ET), 14-day retention
- 16 tables: price_ticks, trade_decisions, predictions, agent_logs, futures_implied_prices, etc.
- Alembic migrations for schema versioning

### Circuit Breakers
- 6 external services monitored: Telegram, Supabase, Finnhub, SEC, NewsAPI, Twitter
- 3 states: CLOSED (allow), OPEN (block), HALF_OPEN (test recovery)
- Open after 5 consecutive failures; retry after 60s

### Episodic Learning
- `.agent/memory/` stores episodes (predictions, trades, signals) + lessons learned
- Pattern discovery via K-means clustering (4 metrics)
- Nightly auto-dream job (3 AM ET) searches for insights
- Candidate→staged→graduated lesson lifecycle

### Integration Testing
- 26 tests covering: WAL concurrency, backup recovery, Pydantic validation, circuit breaker state, market hours
- Run: `pytest gme_trading_system/tests/ -v`

## Configuration

Edit `.env` or environment variables:

```bash
# LLM providers
GOOGLE_API_KEY=your-key                # Required for Gemini fallback
OLLAMA_HOST=http://localhost:11434     # Default

# Notifications
TELEGRAM_BOT_TOKEN=your-token
TELEGRAM_CHAT_ID=your-chat-id
DISCORD_BOT_TOKEN=your-token

# Data sources (optional)
FINNHUB_API_KEY=your-key
NEWSAPI_KEY=your-key

# Market/timing
MARKET_TIMEZONE=America/New_York       # Default
ACTIVE_WINDOW_START=0730               # 07:30 ET
ACTIVE_WINDOW_END=1800                 # 18:00 ET

# Database
DATABASE_URL=sqlite:///agent_memory.db # Default
BACKUP_DAYS=14                         # Retention
```

## Troubleshooting

### "Connection to Ollama failed"
```bash
# Verify Ollama is running and has the model
ollama serve

# In another terminal:
ollama pull gemma2:9b
curl http://localhost:11434/api/tags
```

### "No module named 'crewai'"
```bash
source venv/bin/activate
pip install -r gme_trading_system/requirements.txt
```

### Agents not running / orchestrator hangs
Check if orchestrator process is stuck:
```bash
ps aux | grep orchestrator.py
tail -f orchestrator.log
```

Restart:
```bash
pkill -9 -f orchestrator.py
cd gme_trading_system
python orchestrator.py
```

### Dashboard shows "API connection failed"
Verify API server is running:
```bash
curl http://localhost:8000/api/agents/status
```

If not running:
```bash
cd gme_trading_system
python api_server.py
```

### Database growing too large
Check size:
```bash
ls -lh agent_memory.db
sqlite3 agent_memory.db "SELECT COUNT(*) FROM agent_logs;"
```

Nightly backup/cleanup runs at 3 AM ET automatically. To force:
```bash
python -c "from db_maintenance import backup_db, nightly_maintenance; backup_db(); nightly_maintenance()"
```

### Circuit breaker is OPEN (external service down)
Check status:
```bash
python -c "from circuit_breaker import get_breaker; breaker = get_breaker('telegram'); print(f'State: {breaker.state}, Failures: {breaker.failure_count}')"
```

Wait 60s or manually reset (development only):
```bash
python -c "from circuit_breaker import get_breaker; get_breaker('telegram').half_open()"
```

## Deployment

### Local (Development)
Run `supervisord` or manual terminals as shown above.

### Docker (Recommended)
```bash
docker compose build
docker compose --profile with-ollama up -d
```

Services:
- `trading-system` — Python (orchestrator, logger_daemon, bots)
- `ollama` — Gemma 2:9b model server
- `dashboard` — React frontend (nginx)

Verify:
```bash
curl http://localhost:8765/health   # Logger daemon
curl http://localhost:8000/api/agents/status  # API server
curl http://localhost:5173          # Dashboard
```

### Production Considerations
- **Supervisor** manages process restarts and respawning
- **WAL mode** + nightly backups ensure data durability
- **Circuit breakers** prevent cascading failures
- **Market hours gating** reduces unnecessary compute during closed hours
- **Logs** rotate (supervisord handles via stdout capture)

## Testing

```bash
# Run all 26 integration tests
cd gme_trading_system
pytest tests/test_integration.py -v

# Manual agent cycle (for debugging)
python -c "from orchestrator import run_valerie_cycle; run_valerie_cycle()"

# Test backtester
python backtester.py --last-days 90
```

## Performance Notes

- **Cycle time**: ~30–90s per full 10-agent run (Gemma local inference ~2–5s per agent)
- **Memory**: ~300 MB baseline (Python + Gemma context), SQLite grows ~5–10 MB/month
- **Cost**: ~$0/day (local Gemma) + optional Gemini fallback (~$0.10–0.50/day if 429s occur)

## Contributing

This is a personal research system. Fork and adapt as needed. All contributions welcome.

## License

MIT

---

**Status**: Active development. 10-agent system running 24/7 since Jan 2026. All 26 integration tests passing.

**Key Recent Changes**:
- [x] Migrated LLM stack to **Gemma 2:9b local** (cost-optimized, no rate limits)
- [x] Circuit breakers for 6 external services
- [x] Flask logger daemon (TradingView webhooks, Prometheus metrics)
- [x] Episodic learning + pattern discovery
- [x] Docker + supervisord deployment
- [x] 26 integration tests (WAL, backup, Pydantic, market hours)

**Next Milestones**:
- [ ] Fine-tune Gemma on GME historical patterns (LoRA)
- [ ] Live broker integration (IBKR)
- [ ] Backtester: portfolio optimization
- [ ] Kubernetes deployment (EKS)
- [ ] Multi-symbol support (not just GME)
