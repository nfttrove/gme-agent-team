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
AGENTS_PY = REPO_ROOT / "gme_trading_system" / "agents.py"


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


# Canonical mapping from agents.py `role=` kwargs to AGENTS.md `### <header>` tags
# (the human-readable name in front of the em-dash separator). When you add a
# new agent, update three places at once:
#   1. agents.py — instantiate `ResilientAgent(role="...", ...)`
#   2. AGENTS.md § The N agents — add a `### Name — Role` H3 (and bump N)
#   3. this mapping — bridge the two
# The inventory fitness test below will fail loudly if any side drifts.
AGENT_ROLE_TO_MD_HEADER = {
    "Data Validator":                                   "Valerie",
    "Stream Commentator":                               "Chatty",
    "News Analyst":                                     "Newsie",
    "Triangle Breakout & Multi-Day Pattern Specialist": "Pattern",
    "Daily Trend Analyst":                              "Trendy",
    "Market Futurist":                                  "Futurist",
    "GeoRisk Researcher":                               "GeoRisk",
    "Intelligence Synthesiser":                         "Synthesis",
    "Project Manager":                                  "Boss / Project Manager",
    "Chief Technology & Market Structure Officer":      "CTO",
    "Historical Researcher":                            "Memoria",
    "Strategy Briefing Officer":                        "Briefing",
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


def _agents_py_roles() -> set[str]:
    """AST-walk agents.py for every `ResilientAgent(...)` call and pull out
    the `role=` kwarg literal. Returns the set of role strings.

    Skips the class definition itself — only collects instantiations."""
    tree = ast.parse(AGENTS_PY.read_text())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "ResilientAgent"):
            continue
        for kw in node.keywords:
            if kw.arg == "role" and isinstance(kw.value, ast.Constant):
                out.add(kw.value.value)
                break
    return out


def _agents_md_inventory_headers() -> set[str]:
    """Extract `### Name — ...` H3 header tags from AGENTS.md's
    `## The N agents` section. Returns the set of names (the part before the
    em-dash / en-dash / hyphen separator)."""
    text = AGENTS_MD.read_text()
    m = re.search(r"## The \d+ agents\n(.+?)(?:\n## |\Z)", text, flags=re.DOTALL)
    assert m, "AGENTS.md is missing the `## The N agents` section"
    section = m.group(1)
    headers = re.findall(r"^### (.+?)\s*[—–\-]{1,2}\s", section, flags=re.MULTILINE)
    return {h.strip() for h in headers}


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


def test_agents_md_inventory_matches_agents_py():
    """
    Given the H3 headers under AGENTS.md § The N agents
    When agents.py instantiates ResilientAgent(...) with role= kwargs
    Then every documented agent must have a matching role in code and vice
    versa, bridged by AGENT_ROLE_TO_MD_HEADER in this file.

    Catches the next-most-likely drift after the active-window contract:
    renaming/adding/removing an agent in one place and forgetting the others.
    The mapping in this file is the single source of truth — if you add a
    13th agent, you update the mapping, agents.py, and AGENTS.md together.
    """
    # Given
    documented_headers = _agents_md_inventory_headers()
    # When
    code_roles = _agents_py_roles()
    # Then — code side
    expected_roles = set(AGENT_ROLE_TO_MD_HEADER.keys())
    missing_in_code = expected_roles - code_roles
    extra_in_code = code_roles - expected_roles
    assert not missing_in_code and not extra_in_code, (
        f"agents.py role kwargs drifted from AGENT_ROLE_TO_MD_HEADER. "
        f"Missing in code: {sorted(missing_in_code)}. "
        f"Extra in code: {sorted(extra_in_code)}. "
        "Code wins (per CLAUDE.md): if you added/renamed an agent, update the "
        "mapping in tests/test_docs_architecture.py AND the corresponding "
        "`### Name — Role` H3 in AGENTS.md."
    )
    # Then — doc side
    expected_headers = set(AGENT_ROLE_TO_MD_HEADER.values())
    missing_in_doc = expected_headers - documented_headers
    extra_in_doc = documented_headers - expected_headers
    assert not missing_in_doc and not extra_in_doc, (
        f"AGENTS.md § The N agents H3 headers drifted from "
        f"AGENT_ROLE_TO_MD_HEADER. "
        f"Missing in doc: {sorted(missing_in_doc)}. "
        f"Extra in doc: {sorted(extra_in_doc)}. "
        "Code wins (per CLAUDE.md): update AGENTS.md to add/rename the "
        "`### Name — Role` H3 to match agents.py, or update the mapping in "
        "tests/test_docs_architecture.py if an agent was removed."
    )


def test_agents_md_section_title_matches_inventory_count():
    """
    Given AGENTS.md's `## The N agents` section heading
    When the H3 entries in that section are enumerated
    Then N must equal the number of H3 entries.

    Belt-and-braces for the inventory test: someone could add a 13th agent,
    update agents.py + the mapping + a new H3, and still forget to bump the
    'The 12 agents' heading. This catches that.
    """
    text = AGENTS_MD.read_text()
    m = re.search(r"^## The (\d+) agents\b", text, flags=re.MULTILINE)
    assert m, "AGENTS.md is missing the `## The N agents` section heading"
    declared = int(m.group(1))
    actual = len(_agents_md_inventory_headers())
    assert declared == actual, (
        f"AGENTS.md heading says 'The {declared} agents' but the section "
        f"contains {actual} H3 entries. Update the heading or the inventory."
    )
