# GME Agent Team — Context for Claude Code

## TL;DR

12-agent CrewAI system that watches GME, emits Telegram signals with confidence scores, and learns from team execution feedback. Local **Gemma 2:9b** via Ollama primary; Gemini Flash/Pro fallback. **No web dashboard — Telegram is the only UI.** No autonomous broker execution.

The canonical docs are:
- [README.md](README.md) — what this is, how to run it
- [AGENTS.md](AGENTS.md) — per-agent behaviour spec (the 12 agents, schedules, what they read/write)
- [RUNBOOK.md](RUNBOOK.md) — operator procedures, known issues, restart commands

If those three and this file ever drift, fix the docs — they reflect the live system.

## Tech stack

- **Backend:** Python 3.12, CrewAI ≥0.28, APScheduler, SQLite WAL, optional Supabase mirror
- **LLMs:** Gemma 2:9b (Ollama local, primary) → Gemini Flash 2.5 (429 fallback) → Gemini Pro (complex reasoning for Futurist + CTO)
- **Infra:** macOS launchd for the orchestrator (`com.gme.orchestrator`), Flask `:8765` for webhooks + Prometheus metrics, Telegram + Discord bots

## Key paths

```
gme_trading_system/
├── orchestrator.py         # APScheduler entry point; ~30 scheduled jobs around line 2530
├── agents.py               # 12 ResilientAgent definitions
├── tasks.py                # CrewAI task specs (description + expected_output)
├── llm_config.py           # Gemma local + Gemini fallback wiring
├── circuit_breaker.py      # 3-state breakers for 6 external services
├── market_hours.py         # is_market_open(), is_active_window() — 08:30–17:00 ET Mon–Fri
├── db_maintenance.py       # WAL mode, nightly backup, 14-day retention
├── logger_daemon.py        # Flask :8765 — TradingView webhook + /metrics
├── notifier.py             # Telegram push, circuit-breaker wrapped
├── telegram_bot.py         # Telegram listener + command router (24 commands)
├── discord_bot.py          # Discord standalone bot
├── signal_manager.py       # Canonical signal CRUD (used by orchestrator + telegram_bot)
├── confidence_calibration.py  # Per-agent win-rate multipliers
├── signal_gate.py          # Filter analysis outputs into signals
├── episodic_integration.py # Hooks: log_futurist_prediction, log_manager_trade, log_synthesis_brief
├── agent_memory.db         # SQLite, ~16 tables, WAL mode (canonical DB)
├── alembic/                # Baseline migrations
├── db_schema.sql           # Applied by alembic baseline
├── migrations_add_goals.sql  # NOT in alembic — load-bearing for services/goal_service.py (see RUNBOOK)
├── supabase_schema.sql     # Supabase mirror, applied out-of-band via SQL editor
├── models/agent_outputs.py # Pydantic: FuturistPrediction, TraderDecision, SynthesisBrief, NewsSignal
├── tests/                  # pytest suite (~140 tests, ~3000 LOC)
├── supervisord.conf        # Manages logger, orchestrator, discord_bot — but prod uses launchd
└── requirements.txt        # Python deps (canonical)

.agent/                     # Episodic memory + lesson lifecycle
├── episodic_logger.py
├── cluster_patterns.py     # K-means on agent accuracy metrics
├── auto_dream.py           # Note: also exists at .agent/memory/auto_dream.py — see RUNBOOK known issues
├── init_memory.py
├── graduate.py, recall.py, list_candidates.py  # Candidate → staged → graduated lifecycle
├── tools/                  # Partial duplicate of root .agent/ scripts (cleanup pending — RUNBOOK)
└── memory/                 # episodes.jsonl, lessons.jsonl, candidates/

.env                        # Live credentials (NEVER commit) — use .env.example as template
```

## Key constraints

1. **No tool calls in agents** — Gemma doesn't support CrewAI tool calling. Agents output structured text; `sql_executor.py` parses and executes.
2. **LLM fallback chain** — Gemma → Gemini Flash (rate limit) → Gemini Pro (complex). Lives in `ResilientAgent` (agents.py).
3. **Active window gating** — 08:30–17:00 ET, Mon–Fri. Eight agents decorated with `@active_window_required` skip silently outside.
4. **Crew timeouts** — `safe_kickoff()` wraps `crew.kickoff()` with `ThreadPoolExecutor`; crews abort after 180–600s.
5. **Circuit breakers** — 6 services (Telegram, Supabase, Finnhub, SEC, NewsAPI, Twitter); open after 5 failures, retry after 60s.
6. **Database** — SQLite WAL mode, nightly backup at 03:00 ET, 14-day retention. Canonical path: `gme_trading_system/agent_memory.db`.
7. **Production launcher** — launchd (`com.gme.orchestrator`), not supervisord. The supervisord.conf is for Docker/container use, not the macOS host.
8. **Signal confidence** — every signal carries 0.0–1.0 confidence; team execution decisions (executed/ignored/missed) feed the win-rate ledger via `signal_manager`.

## Common tasks

### Add a new agent
See [AGENTS.md § Adding a new agent](AGENTS.md#adding-a-new-agent).

### Change a schedule
Edit `configure_schedule()` in `orchestrator.py` around line 2530:
```python
self.scheduler.add_job(run_newsie, IntervalTrigger(minutes=30), id="newsie")
```

### Wrap an external call with a circuit breaker
```python
from circuit_breaker import get_breaker, CircuitOpenError
breaker = get_breaker("service_name")
try:
    result = breaker.call(requests.get, url, timeout=10)
except CircuitOpenError:
    log.warning("circuit open")
    return fallback_result
```

### Debug a single cycle
```bash
cd gme_trading_system
python -c "from orchestrator import run_futurist_prediction_signal; run_futurist_prediction_signal()"
```

### Inspect metrics
```bash
curl http://localhost:8765/health
curl http://localhost:8765/metrics
```

## Testing

```bash
./venv/bin/python -m pytest gme_trading_system/tests/ -v
```

Baseline: **436 pass, 1 pre-existing failure** (`test_dv_default_watchlist` — DV `pillar_D` KeyError; the historical `test_signal_scorer_detects_sl_first_touch_as_loss` was fixed 2026-05-15, see RUNBOOK item 6). House style for new tests: behaviour-focused names, Given/When/Then docstrings — see [gme_trading_system/tests/README.md](gme_trading_system/tests/README.md).

## Environment

See `.env.example`. Required: `GOOGLE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Optional: `FINNHUB_API_KEY`, `NEWSAPI_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `DISCORD_BOT_TOKEN`. Default `OLLAMA_HOST=http://localhost:11434`.

## Never commit
- `.env`, `*.db`, `gme_trading_system/agent_memory.db`, `gme_trading_system/logs/`
- `venv/`, `__pycache__/`, `*.pyc`
- API keys, credentials, or secrets

## Notes for Claude Code sessions
- Always check the plan file (`~/.claude/plans/`) before starting implementation.
- Reference exact file paths and line numbers in prompts and replies.
- Use `/clear` between unrelated tasks to save context.
- When docs and code disagree, **the code wins** — propose a doc fix.
- Don't reintroduce a web dashboard. Telegram is the canonical UI. If a feature would benefit from richer presentation, add a Telegram command first.
