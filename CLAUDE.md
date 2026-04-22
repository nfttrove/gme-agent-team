# GME Agent Team - Project Context for Claude Code

## Project Overview
9-agent CrewAI multi-agent system for GME real-time analysis + signal confidence scoring + team feedback loop. **Operating model:** Agents emit confident signals → team receives Telegram alerts → team logs execution decisions → system computes win rate metrics. Cost-optimized: primary LLM is **local Gemma 2:9b** (Ollama at localhost:11434), DeepSeek-r1:8b for complex reasoning agents, Gemini Flash/Pro fallback. No Claude API calls in production code. No autonomous execution via brokers.

## Tech Stack
- **Backend:** Python 3.12, CrewAI 0.28+, SQLite (agent_memory.db), Supabase (mirror)
- **Frontend:** React 18 + TypeScript, Vite, Tailwind, Recharts, Lucide icons
- **LLMs:** Gemma 2:9b local (primary), Gemini Flash 2.5 (fallback for 429), Gemini Pro (research)
- **Infra:** APScheduler (cron), Telegram/Discord bots, Ollama 0.11434, ngrok (TradingView webhook)

## Key Directories
```
/gme_trading_system/        # Main system
├── orchestrator.py         # APScheduler entry point, 9 agents + cron jobs
├── agents.py               # 9 CrewAI agents (all use ResilientAgent fallback pattern)
├── tasks.py                # 9 CrewAI tasks (no external tool calls — agents output SQL/text)
├── llm_config.py           # LLM definitions (Gemma local, Gemini fallbacks)
├── circuit_breaker.py      # 3-state CLOSED/OPEN/HALF_OPEN for 6 external services
├── market_hours.py         # is_market_open(), is_active_window() (07:30-18:00 ET Mon-Fri)
├── db_maintenance.py       # enable_wal_mode(), backup_db(), nightly_maintenance()
├── logger_daemon.py        # Flask :8765 (TradingView webhook + /metrics endpoint)
├── episodic_integration.py # Hooks: log_futurist_prediction, log_manager_trade, log_synthesis_brief
├── notifier.py             # Telegram push (wrapped with circuit breaker)
├── telegram_bot.py         # Telegram bot listener + fallback LLM (_ask_llm with Ollama→Gemini chain)
├── discord_bot.py          # Discord bot (standalone, supervised)
├── backtester.py           # CLI: replay past trades, compute Sharpe/max_drawdown
├── agent_memory.db         # SQLite: 16 tables (price_ticks, trade_decisions, predictions, agent_logs, etc.)
├── metrics.jsonl           # Flat metrics log (cycle durations, status)
├── alembic/                # DB migrations (baseline + WAL marker)
├── models/agent_outputs.py # Pydantic: FuturistPrediction, TraderDecision, SynthesisBrief, NewsSignal
├── tests/test_integration.py # 26 integration tests (WAL, backup, Pydantic, circuit breakers, market hours)
├── supervisord.conf        # Manages: logger, orchestrator, discord_bot, dashboard
├── docker-compose.yml      # Trading system + Ollama sidecar
└── requirements.txt        # Flask, CrewAI, Supabase, Prometheus, Discord, Pydantic, etc.

/dashboard/                 # React frontend + Python API
├── api_server.py           # Flask :8000 for React (/api/agents/*, /api/brief, /api/logs, /api/stats)
├── src/                    # React components (Dashboard.tsx, AgentPanel.tsx, etc.)
└── package.json            # Node deps (React, Recharts, Supabase client, TailwindCSS)

.agent/                     # Agent memory/learning
├── episodic_logger.py      # log_prediction, log_trade, log_signal, log_synthesis
├── cluster_patterns.py     # discover_patterns() — K-means on 4 agent accuracy metrics
├── auto_dream.py           # Nightly cron job (3 AM ET) for pattern discovery
├── init_memory.py          # Seed 3 lessons (pe_playbook, gme_immunity, high_confidence_prediction)
├── graduate.py, recall.py, list_candidates.py # Lifecycle: candidate → staged → graduated
└── memory/                 # episodes.jsonl, lessons.jsonl, candidates/

.env                        # Live credentials (NEVER commit) — use .env.example as template
agent_memory.db             # Root-level SQLite (used by main.py, dashboard)
```

## Core Agent Names & Cycles
| Agent | File | Schedule | Window | LLM | Purpose |
|-------|------|----------|--------|-----|---------|
| Valerie | agents.py | Every 5 min | Active | Gemma→Flash | Data validation (price, volume, OHLC sanity) |
| Chatty | agents.py | Every 5 min | Active | Gemma→Flash | Real-time commentary on price action |
| Newsie | agents.py | Every 30 min | Active | Gemma→Flash | News sentiment (Finnhub, Supabase edge, NewsAPI) |
| Pattern | agents.py | Every 2 hours | Active | Gemma→Flash | Multi-day trend analysis + clustering |
| Trendy | agents.py | Every 4 hours + 8 PM ET | Active | Gemma→Flash | Daily trend, end-of-day summary |
| Futurist | agents.py | Every 2 hours | Active | DeepSeek-r1→Flash | 1h/4h/EOD price prediction (complex reasoning) |
| GeoRisk | agents.py | Every 1 hour | Active | Gemma→Flash | Geopolitical risk scoring |
| Synthesis | agents.py | Every 5 min | Active | Gemma→Flash | Cross-agent consensus brief (context for all others) |
| Boss | agents.py | Daily 9:00 AM ET | Market | Gemma→Flash | Daily mission briefing + yesterday's review |
| CTO | agents.py | Daily 9:05 AM ET + Sun 8 AM | Market | DeepSeek-r1→Flash | PE playbook monitoring, short-side research |

## Key Constraints
1. **No tool calls in agents** — Gemma doesn't support CrewAI tool calling. Agents output SQL/text; `sql_executor.py` parses & executes.
2. **LLM fallback chain** — Gemma → Gemini Flash (rate limit) → Gemini Pro (complex). See `ResilientAgent` in agents.py.
3. **Active window gating** — 07:30-18:00 ET, Mon-Fri. 6 agents decorated with `@active_window_required` skip silently if outside window.
4. **Crew timeouts** — safe_kickoff() wraps crew.kickoff() with ThreadPoolExecutor; crews abort after 180–600s depending on agent.
5. **Circuit breakers** — 6 external services (Telegram, Supabase, Finnhub, SEC, NewsAPI, Twitter) wrapped; open after 5 failures, retry after 60s.
6. **Database** — SQLite with WAL mode (concurrent reads), nightly backup at 3 AM ET, 14-day retention.
7. **Market hours** — NYSE 09:30-16:00 ET; active window 07:30-18:00 ET (2h pre/post buffer). Decorator skips signal generation outside market hours.
8. **Signal confidence scoring** — All signals logged with 0.0-1.0 confidence. Team logs execution decisions (executed/ignored/missed) for feedback loop.

## Common Tasks

### Add a new agent
1. Define in `agents.py` (role, goal, backstory, no tools parameter)
2. Define task in `tasks.py` (description, expected_output, context)
3. Add cycle function in `orchestrator.py` (decorate with `@active_window_required` if needed)
4. Add schedule in `configure_schedule()` using `IntervalTrigger` or `CronTrigger`
5. Test: `python -c "from orchestrator import run_YOUR_AGENT; run_YOUR_AGENT()"`

### Change agent schedule
Edit `orchestrator.py` in `configure_schedule()`. Example:
```python
self.scheduler.add_job(run_newsie, IntervalTrigger(minutes=30), id="newsie")
```

### Add circuit breaker to an external call
```python
from circuit_breaker import get_breaker, CircuitOpenError
breaker = get_breaker("service_name")
try:
    result = breaker.call(requests.get, url, timeout=10)
except CircuitOpenError:
    log.warning("circuit open")
    return fallback_result
```

### Debug a cycle
```bash
cd gme_trading_system
python -c "from orchestrator import run_futurist_cycle; run_futurist_cycle()"
```

### Check metrics
```bash
curl http://localhost:8765/metrics  # Prometheus format
curl http://localhost:8765/health   # JSON tick count
```

### Run backtester
```bash
cd gme_trading_system
python backtester.py --last-days 90
```

### Deploy via Docker
```bash
docker compose build
docker compose --profile with-ollama up -d  # Includes Ollama sidecar
curl http://localhost:8765/health  # Verify
```

## Recent Commits
- **1b0f470** — Add circuit breakers, Prometheus metrics, Docker, backtester
- **042c306** — Add Pydantic models, Alembic migrations, integration tests, WAL
- **d59bcb5** — Harden orchestrator: crew timeouts + active window gating + .env template
- **61ff0fc** — Make system Gemma-only, eliminate 429 rate limit errors

## Never Commit
- `.env`, `.env.local`, `*.db`, `agent_memory.db`
- `dashboard/node_modules/`, `venv/`, `__pycache__/`, `*.pyc`
- API keys, credentials, or secrets anywhere

## Testing
```bash
./venv/bin/python -m pytest gme_trading_system/tests/ -v
```

All 26 integration tests pass. Covers: WAL concurrency, backup recovery, Pydantic validation, circuit breaker state transitions, market hours, Alembic layout.

## Environment Variables
See `.env.example`. Critical vars: `GOOGLE_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_BOT_TOKEN`.

For local Ollama: `OLLAMA_HOST=http://localhost:11434` (default).

## Notes for Claude Code Sessions
- Use **precise prompts** referencing exact file paths and line numbers
- Use `/model haiku` for quick questions; switch back to Sonnet for complex refactors
- Use `/clear` between unrelated tasks to save context
- Never paste large files; ask Claude to read them directly
- Always check the plan file (`/Users/user/.claude/plans/`) before starting implementation
