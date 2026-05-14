# config/ — multi-symbol design archive (not yet wired)

These YAML and Markdown files came out of a `trading_system_v2/` scaffold from a prior Claude session (worktree `zealous-austin-18e200`) that never landed. The codebase parts of that scaffold were duplicates of existing modules and have been archived to `.archive/trading_system_v2_2026-05-14.tar.gz` (gitignored). What's preserved here is the design content that was genuinely useful:

- **`theses/<ticker>.md`** — per-symbol investment thesis as plain Markdown, intended to be injected into agent prompts (`{{thesis}}` substitution). The GME entry mirrors `project_gme_thesis.md` in user-memory; the EBAY entry tracks the rumoured GME↔EBAY merger.
- **`symbols.yaml`** — multi-symbol active universe with `enabled: true/false` flags. Lets the universe expand without code edits. Currently mirrors `dv_score.DEFAULT_WATCHLIST` with only GME + EBAY enabled.
- **`agents.yaml`** — agent prompt templates with `{{symbol}}` and `{{thesis}}` substitution. Currently the live system hardcodes prompts in `agents.py` / `tasks.py`; this YAML is what a config-driven version would look like.
- **`schedule.yaml`** — cycle cadence + cron-driven digests. Currently `orchestrator.py:configure_schedule()` does this in code.

Status: **reference only**, not loaded by the live orchestrator. The current system (`agents.py`, `orchestrator.py`, hardcoded prompts) is what runs in production. Treat these files as a sketch of where a future v2 refactor could land if/when multi-symbol support becomes a priority.
