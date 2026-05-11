# Test style

This suite is plain `pytest` — no `pytest-bdd`, no Gherkin files. We borrow the **discipline** of BDD (Wynne, Keogh — "examples, not specs") without taking on the tooling cost.

## Run them

```bash
./venv/bin/python -m pytest gme_trading_system/tests/ -v
```

Baseline at the time of writing: **144 pass, 2 pre-existing failures** (`test_signal_scorer_detects_sl_first_touch_as_loss`, `test_trove_default_watchlist`). If a commit changes that ratio, that's the signal to investigate.

## House style for new tests

1. **Name the behaviour, not the implementation.**

   Bad: `test_breaker_open`, `test_signal_dedupe`, `test_wal_concurrent`
   Good: `test_when_circuit_breaker_opens_then_telegram_calls_skip_silently`
         `test_intraday_pattern_signals_do_not_alert_twice_for_the_same_setup`
         `test_writers_do_not_block_readers_when_wal_is_enabled`

   The name should read like a sentence a non-coder could nod along to. If you can't describe what's being verified without referencing internals, you're probably testing the implementation, not the behaviour.

2. **Use Given / When / Then in the docstring.** Three short clauses, not three paragraphs. Add a sentence explaining *why this matters* — what real-world incident or design decision this test is the safety net for. That's the Wynne "example, not contract" idea: the test exists because something would visibly break in production if it failed.

3. **Mirror G/W/T as inline comments** in the body. Cheap visual structure, no abstraction overhead. If the test is so small the comments would be noise (single arrange line, single assert), don't bother.

4. **One behaviour per test.** If you find yourself writing `# also...` partway through, split.

## Worked example

Before — implementation-focused name, terse docstring:

```python
def test_concurrent_read_write_with_wal(self, test_db):
    """Verify WAL allows concurrent reads while writes are active."""
    from db_maintenance import enable_wal_mode
    import threading, time
    enable_wal_mode(test_db)
    errors = []
    # ... writer thread / reader thread / asserts ...
```

After — behaviour-focused name, G/W/T docstring and body markers, plus the *why*:

```python
def test_writers_do_not_block_readers_when_wal_is_enabled(self, test_db):
    """
    Given a database in WAL mode
    When a writer is inserting price ticks
    Then a concurrent reader queries without blocking or raising errors.

    This is what lets the orchestrator's many short writes (every 5 min from
    Valerie, Chatty, aggregator_intraday, …) coexist with the Telegram bot's
    ad-hoc reads (/status, /signals, /agents) without 'database is locked'.
    """
    from db_maintenance import enable_wal_mode
    import threading, time

    # Given
    enable_wal_mode(test_db)
    errors = []

    # ... writer / reader thread definitions ...

    # When
    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)

    # Then
    assert not errors, f"Concurrent access failed: {errors}"
```

The test logic is identical. What changed is whether a future maintainer reading the file can answer "what would break if this test failed?" without opening the source under test.

Full version in [test_integration.py](test_integration.py) at `TestWALMode::test_writers_do_not_block_readers_when_wal_is_enabled`.

## When to skip the convention

- **Existing tests stay as they are.** Don't bulk-rename. The convention applies to *new* tests, and to existing tests touched in the same commit as functional changes.
- **Truly tiny tests** (one-liner assertions about a pure function) don't need G/W/T. `test_is_market_open_at_noon_on_a_tuesday()` with a one-line body is fine.
- **Parametrised tests** — name the parametrised function for the rule, let pytest's `[case-id]` suffix carry the specifics.

## On BDD tooling (deferred)

If we ever want stakeholder-readable specs — a non-coder skimming `.feature` files — `pytest-bdd` is the natural next step. It's deliberately not in `requirements.txt` yet. The cost of the second tooling layer (.feature files + step definitions + plumbing) only pays off when there's a real audience for it. Until then, descriptive names + G/W/T docstrings are 80% of the value at 5% of the friction.

## Adding a new test

1. Find the closest existing test file by domain (signals → `test_telegram_handlers.py` or `test_calibration.py`; DB plumbing → `test_integration.py`; agent definitions → `test_agents.py`).
2. Add the test class or function there. New file only if you're testing a domain none of the existing files cover.
3. Run the full suite, not just your new test — make sure baseline holds.
4. Commit with a message that names the behaviour being protected, not the test added.
