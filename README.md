# GME Agent Team

A 12-agent CrewAI system that watches GameStop (GME), produces confident trading signals, and pushes them to a Telegram chat where the team logs execution decisions. The system runs locally on **Gemma 2:9b** (Ollama) with Gemini Flash/Pro as a rate-limit fallback. No autonomous execution. Telegram is the only user interface.

## What this is

- **12 agents** on independent cycles (1 min to weekly) — see [AGENTS.md](AGENTS.md) for the full list
- **One SQLite database** (`gme_trading_system/agent_memory.db`, WAL mode, nightly backup) shared by all agents
- **One Telegram bot** for human interaction — alerts out, commands in, feedback ledger
- **One ops surface** — Flask daemon on `:8765` for TradingView webhooks and Prometheus metrics

If anything in this README contradicts the code, **the code wins**. File a fix.

## Quick start

Prerequisites:
- Python 3.12
- [Ollama](https://ollama.com) with `gemma2:9b` pulled
- Google API key (Gemini fallback), Telegram bot token + chat id

```bash
# 1. Clone
git clone https://github.com/nfttrove/gme-agent-team.git
cd gme-agent-team

# 2. Create the venv
python3.12 -m venv venv
source venv/bin/activate
pip install -r gme_trading_system/requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — minimum required: GOOGLE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 4. Start Ollama (separate terminal) and pull the model
ollama serve
ollama pull gemma2:9b

# 5. Initialise the database
cd gme_trading_system
python -m alembic upgrade head
# One-shot, non-alembic migration (see RUNBOOK.md "Known issues"):
sqlite3 agent_memory.db < migrations_add_goals.sql

# 6. Run it
python orchestrator.py    # in one terminal
python telegram_bot.py    # in another
python logger_daemon.py   # optional, for TradingView webhooks
```

You should now receive Telegram messages within the next active window (07:30–18:00 ET, Mon–Fri). Type `/help` to the bot to see all commands.

## Production (macOS launchd)

On this machine the orchestrator runs under launchd, not supervisord:

```bash
# Status
launchctl print gui/$(id -u)/com.gme.orchestrator | head -20

# Restart
launchctl kickstart -k gui/$(id -u)/com.gme.orchestrator

# Logs
tail -f gme_trading_system/logs/orchestrator.log
```

The plist at `~/Library/LaunchAgents/com.gme.orchestrator.plist` sets `KeepAlive=true`, so the process auto-respawns. See [RUNBOOK.md](RUNBOOK.md) for the full ops cheat sheet.

## How you interact with the system

Everything happens in the configured Telegram chat. Key commands:

| Command | What it does |
|---------|--------------|
| `/status` | System heartbeat — ticks today, last agent run, circuit breaker state |
| `/standup` | Paper-trade win rates + 30-day signal accuracy |
| `/signals [N]` | Recent signals with live TP/SL outcomes |
| `/brief` | Today's strategy brief from the Synthesis agent |
| `/swot` | Owner-only GME strengths/weaknesses/opportunities/threats synthesis from recent agent intel |
| `/force <agent>` | Run an agent on demand (e.g. `/force futurist`) |
| `/learn "<rule>" --why "<reason>"` | Teach the agents a lesson — feeds episodic memory |
| `/candidates`, `/graduate <id>`, `/reject <id>` | Triage lesson candidates |
| `/frequency low\|medium\|high` | Change how chatty the bot is |
| `/help` | Full command list |

Signals carry confidence scores; you reply (or use `/executed`, `/ignored`, `/missed`) to feed the win-rate ledger. The feedback loop is what calibrates each agent's confidence multiplier over time.

## Ops surface (no UI)

There is no web dashboard — the system is Telegram-only. For programmatic monitoring:

```bash
curl http://localhost:8765/health         # JSON tick count, DB size
curl http://localhost:8765/metrics        # Prometheus-format metrics
```

`logger_daemon.py` also receives TradingView webhooks on `POST /tick`.

## Architecture at a glance

- `gme_trading_system/orchestrator.py` — APScheduler entry point, cycle coordination
- `gme_trading_system/agents.py` — 12 `ResilientAgent` definitions (Gemma → Gemini fallback)
- `gme_trading_system/tasks.py` — CrewAI task descriptions (output spec per agent)
- `gme_trading_system/telegram_bot.py` — Telegram listener and command router
- `gme_trading_system/notifier.py` — Telegram push (circuit-breaker wrapped)
- `gme_trading_system/signal_manager.py` — canonical signal CRUD layer
- `gme_trading_system/circuit_breaker.py` — 3-state breakers for external services
- `gme_trading_system/market_hours.py` — `is_market_open()`, `is_active_window()`
- `gme_trading_system/db_maintenance.py` — WAL, nightly backup, 14-day retention
- `gme_trading_system/logger_daemon.py` — Flask `:8765` for webhooks + `/metrics`
- `gme_trading_system/agent_memory.db` — SQLite, ~16 tables, WAL mode
- `gme_trading_system/alembic/` — migrations
- `.agent/` — episodic memory + lesson lifecycle (see [.agent/README.md](.agent/README.md))

Full per-agent contract in [AGENTS.md](AGENTS.md). Operator procedures in [RUNBOOK.md](RUNBOOK.md).

## LLM routing

| Tier | Model | When |
|------|-------|------|
| Primary | `ollama/gemma2:9b` (local) | Default for all agents |
| Fallback | `gemini-2.5-flash` | On Gemma 429 / timeout |
| Complex | `gemini-2.5-pro` | Futurist + CTO when reasoning depth matters |

Local Gemma keeps the cost near zero. The fallback chain is implemented in `llm_config.py` and consumed by `ResilientAgent` in `agents.py`.

## Testing

```bash
./venv/bin/python -m pytest gme_trading_system/tests/ -v
```

Test style and BDD conventions live in [gme_trading_system/tests/README.md](gme_trading_system/tests/README.md).

## Environment

See `.env.example`. Required:
- `GOOGLE_API_KEY` — Gemini fallback
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — alerts + commands

Optional: `FINNHUB_API_KEY`, `NEWSAPI_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `DISCORD_BOT_TOKEN`. Defaults: `OLLAMA_HOST=http://localhost:11434`, market timezone `America/New_York`, active window 07:30–18:00.

## Never commit

`.env`, `*.db`, `gme_trading_system/agent_memory.db`, `gme_trading_system/logs/`, `venv/`, `__pycache__/`. The `.gitignore` enforces this.

## License

MIT. Personal research system.
