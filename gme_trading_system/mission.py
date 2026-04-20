"""
Loads the company mission and provides the operative directive
injected into every agent briefing and crew kickoff.
"""
import os

_MISSION_PATH = os.path.join(os.path.dirname(__file__), "MISSION.md")

with open(_MISSION_PATH) as f:
    MISSION_FULL = f.read()

OPERATIVE_DIRECTIVE = """
=== COMPANY DIRECTIVE (read before every decision) ===
Primary objective: PROFIT GENERATION.
Every analysis must answer: does this make us money?
Protect the capital first. Grow it second. Everything else is noise.
A missed trade costs nothing. A bad trade costs capital we cannot get back.
Be honest, be rigorous, take no shortcuts.
Make money. Do good. In that order.
======================================================
""".strip()
