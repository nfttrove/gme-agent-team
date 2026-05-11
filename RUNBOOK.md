# Runbook

Operator cheat sheet for the GME Agent Team. Read [README.md](README.md) first if you haven't.

## Is the system healthy?

```bash
# Orchestrator under launchd
launchctl print gui/$(id -u)/com.gme.orchestrator | grep -E "state|last exit"

# Last tick + DB connectivity
curl -s http://localhost:8765/health

# Prometheus metrics — tick count, agent cycles, circuit-breaker state, DB size
curl -s http://localhost:8765/metrics | grep -E "^(tick_count|agent_cycles|circuit_state|db_size_bytes)"

# Are agents writing recent rows?
sqlite3 gme_trading_system/agent_memory.db \
  "SELECT agent_name, datetime(MAX(timestamp), 'unixepoch', 'localtime') AS last_run
   FROM agent_logs GROUP BY agent_name ORDER BY last_run DESC;"
```

Via Telegram: `/status` (system heartbeat), `/agents` (last-run for every agent), `/freshness` (staleness check).

## Restart procedures

### Orchestrator (production, launchd)

```bash
# Graceful kick
launchctl kickstart -k gui/$(id -u)/com.gme.orchestrator

# Or unload/load
launchctl unload ~/Library/LaunchAgents/com.gme.orchestrator.plist
launchctl load   ~/Library/LaunchAgents/com.gme.orchestrator.plist
```

`KeepAlive=true` means launchd will respawn the process if it dies. `ThrottleInterval=10` rate-limits restart storms.

### Orchestrator (dev, manual)

```bash
pkill -f orchestrator.py
cd gme_trading_system && python orchestrator.py
```

### Telegram bot

```bash
pkill -f telegram_bot.py
cd gme_trading_system && python telegram_bot.py
```

The bot is not under launchd by default; if you want it supervised, add a second plist.

### Ollama

Ollama runs under launchd as `homebrew.mxcl.ollama`. Same kickstart pattern.

## Logs

| Source | Path |
|--------|------|
| Orchestrator (launchd) | `gme_trading_system/logs/orchestrator.log` |
| Orchestrator (manual) | stdout / stderr of the foreground process |
| Metrics (flat) | `gme_trading_system/metrics.jsonl` |

Tail in real time:

```bash
tail -F gme_trading_system/logs/orchestrator.log
```

The launchd plist sends stdout + stderr to the same file. If `logs/` doesn't exist on first launch, launchd creates it.

## Common problems

### "Connection to Ollama failed"

```bash
ollama list                                # confirm gemma2:9b present
ollama pull gemma2:9b                      # if missing
curl http://localhost:11434/api/tags       # confirm Ollama is up
launchctl kickstart -k gui/$(id -u)/homebrew.mxcl.ollama   # restart if dead
```

### Circuit breaker stuck OPEN

```bash
cd gme_trading_system
python -c "from circuit_breaker import get_breaker; \
  b = get_breaker('telegram'); print(f'{b.state} failures={b.failure_count}')"
```

To force half-open (development only — wait 60s in prod):

```bash
python -c "from circuit_breaker import get_breaker; get_breaker('telegram').half_open()"
```

Breakers wrap: telegram, supabase, finnhub, sec, newsapi, twitter. They open after 5 consecutive failures, retry after 60s.

### Briefs show a stale price / TradingView webhooks have gone quiet

Symptoms: every Synthesis brief carries the same `$X.XX rising` line, `/status` shows a low `Ticks today`, the most recent `price_ticks` row is more than a few minutes old during market hours.

Diagnose:

```bash
# Last 5 ticks — should be ~15s apart during market hours
sqlite3 gme_trading_system/agent_memory.db \
  "SELECT timestamp, close, source FROM price_ticks ORDER BY timestamp DESC LIMIT 5;"

# Is ngrok still up and forwarding to logger_daemon?
curl -s http://localhost:4040/api/tunnels | grep -E "public_url|rate1"

# Logger daemon healthy?
curl -s http://localhost:8765/health
```

Most likely cause: the **TradingView alert paused** (free-tier alerts can pause after each trigger; pro-tier can hit plan limits). Log into TradingView, find the GME alert, re-arm it. The orchestrator will resume on the next webhook.

**Backup feed (since 2026-05-11):** `yahoo_finance_feed.start_yahoo_feed()` is started by `TradingSystemOrchestrator.start()` and writes a Yahoo Finance quote every 5 min via `INSERT OR IGNORE` on `(symbol, timestamp)`. TradingView always wins the same-second race; Yahoo only fills gaps. If you see a `source='yahoo'` row in `price_ticks`, that's the fallback doing its job — usually a sign TradingView is silent.

### Orchestrator hangs

Crews can deadlock if Gemma stops responding. The orchestrator wraps `crew.kickoff()` in a `ThreadPoolExecutor` with a 180–600s timeout per agent, so a hang in one cycle shouldn't take down the scheduler. If it does:

```bash
pkill -9 -f orchestrator.py
launchctl kickstart -k gui/$(id -u)/com.gme.orchestrator
```

### Tests fail with `pillar_D` or `signal_scorer` errors

Pre-existing failures unrelated to recent commits — `test_trove_default_watchlist` (Trove pillar_D bug) and `test_signal_scorer_detects_sl_first_touch_as_loss` (calibration scoring). Current baseline is **180 passed, 2 failed**. If you see *new* failures, investigate.

### `/progress` (private £5k tracker) shows the wrong number

`/progress` is an owner-only Telegram command (see `OWNER_ONLY_COMMANDS` in [telegram_bot.py](gme_trading_system/telegram_bot.py)). It sums closed paper-trade PnL from `trade_decisions`, converts USD→GBP via the `USD_GBP_RATE` constant in [target_progress.py](gme_trading_system/target_progress.py), and renders the earned / target / days-left / daily-burn one-liner.

- **No live FX feed.** Default rate is 0.79. Set `USD_GBP_RATE=0.81` (or current spot) in `.env` to override.
- **Lifetime PnL, not period-bounded.** The handler reads every `status='closed' AND paper_trade=1` row. If you want a tracking start date (e.g. count only trades after a particular date), extend the SQL — there's no `target_started_at` marker in the schema yet.
- **Why it's private and not in the broadcast briefs.** The £5k is a personal monthly goal. Public briefs (`run_daily_briefing`, `run_saturday_review`) intentionally omit it; only `/progress` exposes it, only to the owner's chat_id.

### DB growing too large

```bash
ls -lh gme_trading_system/agent_memory.db
sqlite3 gme_trading_system/agent_memory.db "SELECT name, COUNT(*) FROM sqlite_master m \
  JOIN pragma_table_info('') p WHERE m.type='table' GROUP BY name;"
```

Nightly maintenance runs at 03:00 ET (backup + WAL checkpoint + 14-day retention). To force:

```bash
cd gme_trading_system
python -c "from db_maintenance import backup_db, nightly_maintenance; backup_db(); nightly_maintenance()"
```

## Database

- Canonical path: `gme_trading_system/agent_memory.db` (SQLite, WAL mode)
- Schema applied by Alembic baseline (`alembic upgrade head`) from `gme_trading_system/db_schema.sql`
- **Known issue:** `gme_trading_system/migrations_add_goals.sql` is **not** under Alembic but **is** load-bearing — `services/goal_service.py` queries `missions`, `team_goals`, `agent_tasks` tables that live only in this file. A fresh DB needs `sqlite3 agent_memory.db < migrations_add_goals.sql` after `alembic upgrade head`. Folding this into Alembic is a follow-up task.
- Supabase mirror schema lives in `gme_trading_system/supabase_schema.sql`, applied **out-of-band** via the Supabase SQL editor. `supabase_sync.py` writes to it; `supabase_sync_state.json` tracks per-table last-synced row id.

## Known issues / tech debt

These are documented so a future maintainer doesn't waste time rediscovering them.

1. **`.agent/` and `.agent/tools/` duplicates.** Three module pairs (`auto_dream`, `graduate`, `recall`) live in both locations with diverged implementations. `.agent/tools/recall.py` is dead — `orchestrator.py` line ~64 explicitly says so, and `learning.py` replaced it. The other duplicates have callers on both sides, including user CLI invocations documented in `.agent/README.md`. Picking a single canonical path requires manually running each CLI to verify behaviour.
2. **`migrations_add_goals.sql` is not in Alembic** — see Database section above.
3. **`twitter_monitor.py`, `sec_scanner.py`, `insider_buys.py`, `options_feed.py`** — defined modules with no wiring in `orchestrator.py`. May be invoked via Telegram commands or be partially built; needs runtime investigation before deciding to wire or delete.
4. **Two pre-existing test failures** as noted above. Not regressions from recent cleanup commits.
5. **No Telegram bot supervisor.** If `telegram_bot.py` crashes, alerts stop. Either add a launchd plist or accept the manual restart cost.

## Rolling back

This repo uses trunk-based development. If a commit on `main` breaks production:

```bash
# Identify the bad commit
git log --oneline -20

# Revert (creates a new commit that undoes it)
git revert <bad-sha>

# Restart the orchestrator
launchctl kickstart -k gui/$(id -u)/com.gme.orchestrator
```

Avoid `git reset --hard` on a branch that's been pushed — git history is also the system's archive (deleted files live there).

## When to page yourself

- Orchestrator restart loop (launchd respawns within `ThrottleInterval`)
- Circuit breakers for `telegram` or `supabase` stuck OPEN for > 10 min
- No new rows in `price_ticks` during active window for > 10 min
- `/metrics` shows tick_count not advancing
