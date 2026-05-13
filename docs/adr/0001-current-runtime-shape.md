# ADR 0001 — Current runtime shape

**Status:** Accepted (descriptive — the architecture exists; this ADR documents it)
**Date:** 2026-05-13
**Context window:** ADRs 0002+ will land one per major seam as the system evolves.

## Context

The system grew organically from a CrewAI prototype into a production-responsible 12-agent intelligence stream feeding Telegram. A Thoughtworks-style audit on 2026-05-13 identified 15 refactoring opportunities — declarative scheduling, command registry, repository pattern, schema consolidation, DI, client wrappers, presentation modules, etc. Before any of those refactors lands, the current shape needs to be written down so subsequent ADRs have something to evolve *from*.

## Current shape

```
┌──────────────────────────────────────────────────────────────────────┐
│  launchd  (com.gme.orchestrator)                                     │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  orchestrator.py  (~3,300 LOC)                                       │
│    • configure_schedule()  — imperative APScheduler.add_job calls    │
│    • run_* cycle functions — one per agent + cron job                │
│    • DB helpers, lifecycle, startup wiring all colocated             │
└──────────────────────────────────────────────────────────────────────┘
            │                       │                       │
            ▼                       ▼                       ▼
   ┌────────────────┐      ┌────────────────┐     ┌────────────────┐
   │ agents.py      │      │ tools.py       │     │ external APIs  │
   │ ResilientAgent │      │ SQL / price /  │     │ Ollama, Gemini,│
   │ (CrewAI bypass)│      │ news / options │     │ Finnhub, …     │
   └────────────────┘      └────────────────┘     └────────────────┘
            │                       │                       │
            └────────┬──────────────┴──────────────┬────────┘
                     ▼                             ▼
            ┌──────────────────────┐      ┌──────────────────────┐
            │  agent_logs (SQLite) │      │ signal_alerts, etc.  │
            └──────────────────────┘      └──────────────────────┘
                     │                             │
                     ▼                             ▼
   ┌─────────────────────────────────┐   ┌──────────────────────────┐
   │  NARRATIVE PATH                 │   │  STRUCTURED ALERT PATH   │
   │  agent_voice.forward_pending    │   │  notifier.notify_*()     │
   │   → burst parsers in            │   │   → burst formatters in  │
   │     agent_voice (per-agent      │   │     message_formatters_v2│
   │     _try_*_burst functions)     │   │   → _send → Telegram     │
   │   → notifier._send → Telegram   │   └──────────────────────────┘
   └─────────────────────────────────┘
```

## Load-bearing distinction: two output paths

Surfaced during the 2026-05-13 burst-format work. Both paths produce Telegram messages but their inputs and contracts are different:

- **Narrative path** — agent prose is written to `agent_logs`; `agent_voice.forward_pending()` reads new rows, attempts a structured burst parse via per-agent `_try_*_burst()` helpers, and falls through to the legacy prose formatter on parse miss. **Chatty stays on this path** (free prose, no parser). Synthesis, Trendy, Futurist, Pattern, Pattern Intraday, Newsie, and CTO are all routed through resilient burst parsers here.
- **Structured alert path** — orchestrator calls `notifier.notify_*()` with typed-ish kwargs (entry, SL, TP, confidence, reasoning). The notifier composes a burst from those fields and sends via `_send`. Used for `notify_signal_alert`, `notify_trade`, `notify_cto_alert`, `notify_max_pain`, `notify_immunity_red`, etc.

Why this matters: future refactors must respect the split. A "unified pipeline" temptation would couple narrative latency (LLM-bound) to structured alert latency (orchestrator-bound) and lose the resilience the narrative parsers earn from their fall-through behaviour.

## Active window enforcement

08:30–17:00 ET, Mon–Fri, excluding US market holidays. Enforced by the `@active_window_required` decorator in `market_hours.py`. The decorated set is asserted by `tests/test_docs_architecture.py` against `AGENTS.md` so doc drift fails CI.

## Known seams worth extracting later (each gets its own ADR)

1. **Declarative schedule** — `configure_schedule()` is imperative; should be a `SCHEDULED_JOBS: list[ScheduledJob]` dataclass list. Closes the doc/code loop the active-window fitness test opens.
2. **Command registry** — `telegram_bot.handle_command()` is a 45-branch dispatcher across 1,855 lines. Move to `telegram/commands/*.py` + a `COMMANDS` dict.
3. **Repository layer** — direct `sqlite3.connect` is sprinkled through orchestrator, signal_manager, telegram_bot, etc. Introduce `AgentLogRepository`, `SignalRepository`, etc.
4. **Schema consolidation** — three schema-management mechanisms today (`db_schema.sql`, Alembic, `migrations_add_goals.sql`). Pick one canonical path.
5. **Orchestrator decomposition** — split `orchestrator.py` into `orchestration/{app.py, schedule.py, jobs/{intraday,daily,weekly,maintenance}.py}`. Comes after #1 and #2 so the file shrinks naturally first.

(Items 6–14 are in the plan file: `~/.claude/plans/this-context-changes-everything-splendid-llama.md`.)

## Decision

Document the current shape (this ADR). Fix the immediate doc/code drift in `AGENTS.md`. Install a fitness test (`tests/test_docs_architecture.py`) that fails CI on future drift of the active-window contract. Defer the architectural refactors to subsequent ADRs (0002 onwards), one per seam, each with its own parity test scaffold.

## Consequences

- New contributors get a written map of the current runtime.
- Doc drift on the active-window contract fails CI rather than going unnoticed.
- Bigger refactors (declarative schedule, command registry, repositories) can be staged independently. Each gets its own ADR with explicit before/after diagrams and a parity test plan.
- This ADR is descriptive, not prescriptive. It will be superseded by ADRs that propose changes; superseded ADRs stay in the repo with `Status: Superseded by ADR-XXXX` for the audit trail.
