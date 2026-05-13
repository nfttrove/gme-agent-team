"""Architectural fitness tests.

These tests treat `AGENTS.md` as an executable contract with the code.
When the doc and the code disagree, CI fails — and the repo's instruction
(`CLAUDE.md`: "When docs and code disagree, the code wins") tells you which
side to update.

First-pass scope: the `## Active window` section. Future fitness tests can
extend this file to cover the schedule section, agent inventory, etc.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"
MARKET_HOURS = REPO_ROOT / "gme_trading_system" / "market_hours.py"
ORCHESTRATOR = REPO_ROOT / "gme_trading_system" / "orchestrator.py"


# Canonical mapping from decorated cycle function names to agent identities.
# When you add a new @active_window_required function, add an entry here AND
# in the AGENTS.md § Active window paragraph. The fitness test below will
# catch a mismatch in either direction.
CYCLE_TO_AGENT = {
    "run_validation":                 "Valerie",
    "run_daily_trend":                "Trendy",
    "run_trendy_signal":              "Trendy",
    "run_futurist_prediction_signal": "Futurist",
    "run_synthesis":                  "Synthesis",
    "run_synthesis_signal":           "Synthesis",
    "run_pattern_signal":             "Pattern",
    "run_intraday_pattern_signal":    "Pattern",
    "run_newsie_signal":              "Newsie",
    "run_georisk":                    "GeoRisk",
    "run_periodic_brief":             "Briefing",
    # run_voice_forwarder is decorated too but it's the outbound forwarder,
    # not an agent. Documented separately under "Cross-cutting schedules".
    "run_voice_forwarder":            None,
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _active_window_section() -> str:
    """Return the text of AGENTS.md's `## Active window` section."""
    text = AGENTS_MD.read_text()
    m = re.search(r"## Active window\n(.+?)(?:\n## |\Z)", text, flags=re.DOTALL)
    assert m, "AGENTS.md is missing the `## Active window` section entirely"
    return m.group(1)


def _documented_window_string() -> tuple[str, str]:
    """Extract the start/end time strings from the AGENTS.md active-window paragraph.
    Returns ('08:30', '17:00')."""
    section = _active_window_section()
    m = re.search(r"\b(\d\d:\d\d)\s*[–—\-]\s*(\d\d:\d\d)\s*ET\b", section)
    assert m, f"Could not find a time range like 'HH:MM–16:00 ET' in:\n{section}"
    return m.group(1), m.group(2)


def _documented_agent_set() -> set[str]:
    """Extract the agent names listed in the AGENTS.md active-window paragraph.

    Looks only at the sentence introduced by 'decorated cycles are:' and ending
    at the first period followed by a new sentence (e.g. one that starts with
    'Chatty', 'Daily', or 'The window'). This keeps later 'run regardless'
    mentions of agents like Chatty out of the gated set.
    """
    section = _active_window_section()
    decorated_sentence_m = re.search(
        r"decorated cycles are:(.+?)\.\s+(?=Chatty|Daily|The window)",
        section, flags=re.DOTALL
    )
    assert decorated_sentence_m, (
        "Could not locate the 'decorated cycles are: ...' enumeration in "
        "AGENTS.md § Active window. Expected the sentence to end with '. ' "
        "before a follow-up beginning with 'Chatty', 'Daily', or 'The window'."
    )
    decorated_text = decorated_sentence_m.group(1)
    candidates = re.findall(r"\b([A-Z][a-zA-Z]+)\b", decorated_text)
    known_agents = {
        "Valerie", "Trendy", "Futurist", "Synthesis", "Pattern", "Newsie",
        "Chatty", "GeoRisk", "Briefing",
    }
    return {c for c in candidates if c in known_agents}


def _market_hours_window_literals() -> tuple[tuple[int, int], tuple[int, int]]:
    """AST-parse market_hours.py to read ACTIVE_WINDOW_START/END literals.
    Returns ((start_hour, start_min), (end_hour, end_min))."""
    tree = ast.parse(MARKET_HOURS.read_text())
    start = end = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id not in ("ACTIVE_WINDOW_START", "ACTIVE_WINDOW_END"):
                    continue
                # Expect: time(H, M)
                call = node.value
                assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "time", (
                    f"{target.id} should be a time(H, M) literal, got {ast.dump(call)}"
                )
                hour = call.args[0].value
                minute = call.args[1].value if len(call.args) > 1 else 0
                if target.id == "ACTIVE_WINDOW_START":
                    start = (hour, minute)
                else:
                    end = (hour, minute)
    assert start and end, (
        "Could not find ACTIVE_WINDOW_START / ACTIVE_WINDOW_END in market_hours.py"
    )
    return start, end


def _decorated_cycle_functions() -> set[str]:
    """AST-walk orchestrator.py for every FunctionDef carrying
    @active_window_required. Returns the set of function names."""
    tree = ast.parse(ORCHESTRATOR.read_text())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            # Bare-name decorator
            name = dec.id if isinstance(dec, ast.Name) else (
                dec.func.id if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                else None
            )
            if name == "active_window_required":
                out.add(node.name)
                break
    return out


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_documented_active_window_matches_market_hours_module():
    """
    Given the time range published in AGENTS.md § Active window
    When market_hours.py defines ACTIVE_WINDOW_START / ACTIVE_WINDOW_END
    Then the two must agree, so a contributor reading the doc gets the truth.

    This catches the exact drift fixed in commit (this commit) — the doc had
    said 07:30–18:00 ET while the code enforced 08:30–17:00 ET for months,
    and nobody noticed until a Thoughtworks-style audit.
    """
    # Given
    start_str, end_str = _documented_window_string()
    # When
    (start_h, start_m), (end_h, end_m) = _market_hours_window_literals()
    code_start = f"{start_h:02d}:{start_m:02d}"
    code_end = f"{end_h:02d}:{end_m:02d}"
    # Then
    assert start_str == code_start and end_str == code_end, (
        f"AGENTS.md says {start_str}–{end_str} ET but "
        f"market_hours.py says {code_start}–{code_end}. "
        "Code wins (per CLAUDE.md): update AGENTS.md § Active window."
    )


def test_documented_active_window_agent_list_matches_decorators():
    """
    Given the agent names listed in AGENTS.md § Active window
    When orchestrator.py decorates cycle functions with @active_window_required
    Then the set of documented agents equals the set of agents that those
    cycles map to (via tests/test_docs_architecture.CYCLE_TO_AGENT).

    This is the load-bearing assertion: it prevents the doc from saying
    'Chatty is gated' when the code has no such decorator (today's bug).
    """
    # Given
    documented = _documented_agent_set()
    # When
    decorated_fns = _decorated_cycle_functions()
    # Each decorated function must map to a known agent (or to None, meaning
    # it's not agent-facing, e.g. run_voice_forwarder).
    unmapped = {fn for fn in decorated_fns if fn not in CYCLE_TO_AGENT}
    assert not unmapped, (
        f"orchestrator.py has @active_window_required functions not listed in "
        f"CYCLE_TO_AGENT: {sorted(unmapped)}. Add them to the mapping in "
        f"tests/test_docs_architecture.py."
    )
    from_decorators = {
        CYCLE_TO_AGENT[fn] for fn in decorated_fns if CYCLE_TO_AGENT[fn] is not None
    }
    # Then
    assert documented == from_decorators, (
        f"AGENTS.md § Active window lists {sorted(documented)} but the "
        f"@active_window_required decorators in orchestrator.py cover "
        f"{sorted(from_decorators)}. "
        "Code wins (per CLAUDE.md): update AGENTS.md to match the decorator set, "
        "or remove the decorator if the agent shouldn't be gated."
    )


def test_documented_active_window_hours_match_decorator_docstring():
    """
    Given the time range published in AGENTS.md
    When active_window_required's docstring also restates the window
    Then those two must agree, so a developer reading the decorator gets the
    same window the doc promises.

    Belt-and-braces: market_hours.py has the canonical literals AND a docstring
    restating them. If someone updates the literals but forgets the docstring,
    a future contributor grepping for the window finds a stale answer.
    """
    # Given
    start_str, end_str = _documented_window_string()
    # When
    src = MARKET_HOURS.read_text()
    # Find the active_window_required def and inspect its first docstring line
    m = re.search(
        r"def active_window_required\(func\):\s*\n\s*\"\"\"(.+?)\"\"\"",
        src, flags=re.DOTALL
    )
    assert m, "Could not find active_window_required docstring in market_hours.py"
    docstring = m.group(1)
    # Then — the doc range must appear verbatim inside the decorator docstring
    expected = f"{start_str}–{end_str} ET"
    assert expected in docstring, (
        f"AGENTS.md publishes {expected} but active_window_required's docstring "
        f"in market_hours.py doesn't restate it. "
        f"Docstring excerpt: {docstring[:200]!r}. "
        "Code wins: update the docstring (or AGENTS.md) so both match."
    )
