"""Tests for the wording overhaul described in
plans/just-looking-at-this-foamy-swan.md.

Three classes of test:
  1. Golden-output tests for message_formatters helpers.
  2. Parser-invariant tests proving extract_synthesis_from_output still
     captures canonical tokens when human-readable suffixes are appended.
  3. Multi-line / NOW/NEXT format tests (Wave 5 schema change).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from episodic_integration import extract_synthesis_from_output  # noqa: E402
from message_formatters import (  # noqa: E402
    escape_html,
    format_consensus,
    format_data_status,
    format_price,
    format_rsi,
    format_volume,
    normalize_synthesis_capitalization,
    tighten_prose,
)


# ── Golden-output tests for formatters ───────────────────────────────────────


class TestFormatters:
    """Given fixed inputs, each formatter returns exactly the documented string."""

    def test_format_price_with_baseline(self):
        """Given price and prev_close, when formatting, then includes pct change."""
        assert format_price(23.21, 23.50) == "$23.21 (-1.23% on day)"

    def test_format_price_without_baseline(self):
        """Given no prev_close, when formatting, then just the price."""
        assert format_price(23.21, None) == "$23.21"

    def test_format_rsi_with_anchor(self):
        """Given current and at_open RSI, when formatting, then includes anchor."""
        assert format_rsi(44.3, 51.0) == "RSI 44 (was 51 at open)"

    def test_format_rsi_without_anchor(self):
        """Given no anchor, when formatting, then just current."""
        assert format_rsi(44.3) == "RSI 44"

    def test_format_rsi_none(self):
        """Given None, when formatting, then 'RSI n/a'."""
        assert format_rsi(None) == "RSI n/a"

    def test_format_volume(self):
        """Given label and ratio, when formatting, then 'vol <label> (Nx 20d ADV)'."""
        assert format_volume("quiet", 0.43) == "vol quiet (0.43x 20d ADV)"

    def test_format_data_status_clean(self):
        """Given clean quality with no issues, when formatting, then no suffix."""
        assert format_data_status("clean") == "DATA: clean"

    def test_format_data_status_with_gap(self):
        """Given degraded with gap and sources down, when formatting, then suffix included."""
        result = format_data_status("degraded", gap_s=180, sources_down=1)
        assert result == "DATA: degraded (1 source down, 180s tick gap)"

    def test_format_consensus_with_attribution(self):
        """Given full attribution, when formatting, then '(N/M agents; Agent NN%)' suffix."""
        result = format_consensus("BULLISH", 65, agreeing=5, total=7,
                                  top_agent="Futurist", top_conf=0.78)
        assert result == "CONSENSUS: BULLISH 65% (5/7 agents; Futurist 78%)"

    def test_format_consensus_bare(self):
        """Given no attribution, when formatting, then no suffix."""
        assert format_consensus("NEUTRAL", 50) == "CONSENSUS: NEUTRAL 50%"

    def test_escape_html(self):
        """Given <, >, & chars, when escaping, then HTML entities returned."""
        assert escape_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_escape_html_handles_none(self):
        """Given None, when escaping, then empty string (not crash)."""
        assert escape_html(None) == ""


# ── Prose tightening tests ──────────────────────────────────────────────────


class TestTightenProse:
    """Verbose phrases are replaced with terse equivalents."""

    def test_replaces_directional_conviction_phrase(self):
        """Given 'lack of directional conviction', when tightening, then 'indecisive'."""
        before = "RSI is neutral, indicating a lack of directional conviction."
        after = tighten_prose(before)
        assert "directional conviction" not in after.lower()
        assert "indecisive" in after.lower()

    def test_replaces_directional_conviction_bare(self):
        """Given 'directional conviction' alone, when tightening, then 'direction'."""
        assert tighten_prose("strong directional conviction") == "strong direction"

    def test_idempotent_on_clean_text(self):
        """Given already-terse text, when tightening, then unchanged."""
        clean = "Price below VWAP. RSI 44. Indecisive."
        assert tighten_prose(clean) == clean

    def test_handles_empty(self):
        """Given empty, when tightening, then empty."""
        assert tighten_prose("") == ""
        assert tighten_prose(None) is None


# ── Capitalization normalizer tests ─────────────────────────────────────────


class TestCapitalizationNormalizer:
    """Labels stay UPPERCASE; directional values get standardized."""

    def test_lowercase_directional_uppercased(self):
        """Given lowercase 'bearish', when normalizing, then BEARISH."""
        before = "CONSENSUS: bearish 50%"
        assert normalize_synthesis_capitalization(before) == "CONSENSUS: BEARISH 50%"

    def test_mixed_case_normalized(self):
        """Given mixed-case 'Bearish', when normalizing, then BEARISH."""
        assert normalize_synthesis_capitalization("CONSENSUS: Bearish 50%") == \
            "CONSENSUS: BEARISH 50%"

    def test_structural_colors_uppercased(self):
        """Given lowercase 'yellow', when normalizing, then YELLOW."""
        assert "YELLOW" in normalize_synthesis_capitalization("STRUCTURAL: yellow")


# ── Parser-invariant tests (the load-bearing ones) ──────────────────────────


class TestParserTolerance:
    """extract_synthesis_from_output must capture canonical tokens regardless
    of human-readable parenthetical suffixes. If these break, episodic
    memory ingestion silently drops fields — the worst kind of failure.
    """

    def test_legacy_single_line_still_parses(self):
        """Given the existing test_integration.py fixture, when parsing, then all fields captured."""
        legacy = (
            "PRICE: $21.50 | DATA: clean | NEWS: positive 0.75 | PATTERN: bull_flag | "
            "TREND: UP 0.8 | PREDICTION: BULLISH 0.70 | STRUCTURAL: GREEN | "
            "CONSENSUS: BULLISH 65%"
        )
        brief = extract_synthesis_from_output(legacy)
        assert brief is not None
        assert brief.price == 21.50
        assert brief.data_quality == "clean"
        assert brief.news_sentiment == 0.75
        assert brief.trend_direction == "UP"
        assert brief.prediction_bias == "BULLISH"
        assert brief.structural_status == "GREEN"
        assert brief.consensus == "BULLISH"
        assert brief.consensus_pct == 0.65

    @pytest.mark.parametrize("output,expected_data,expected_struct", [
        # Suffix after DATA word
        ("PRICE: $23.21 | DATA: degraded (1 source down) | STRUCTURAL: YELLOW (consolidating) | "
         "NEWS: neutral 0.0 (no catalysts) | TREND: SIDEWAYS 0.4 | PREDICTION: HOLD 0.5 | "
         "CONSENSUS: NEUTRAL 50% (3/6 agents)",
         "degraded", "YELLOW"),
        # No suffix (clean baseline)
        ("PRICE: $23.21 | DATA: clean | STRUCTURAL: GREEN | NEWS: bullish 0.7 | "
         "TREND: UP 0.8 | PREDICTION: BULLISH 0.7 | CONSENSUS: BULLISH 65%",
         "clean", "GREEN"),
        # Suffix with multiple parens
        ("PRICE: $23.21 | DATA: degraded (2 sources down, 180s gap) | STRUCTURAL: RED (debt-heavy) | "
         "NEWS: bearish -0.5 (regulatory risk) | TREND: DOWN 0.7 | PREDICTION: BEARISH 0.65 | "
         "CONSENSUS: BEARISH 60% (4/6 agents; Futurist 78%)",
         "degraded", "RED"),
    ])
    def test_suffixes_dont_break_data_and_structural(self, output, expected_data, expected_struct):
        """Given suffixed canonical tokens, when parsing, then suffix ignored, canonical captured."""
        brief = extract_synthesis_from_output(output)
        assert brief is not None
        assert brief.data_quality == expected_data
        assert brief.structural_status == expected_struct

    def test_consensus_suffix_captures_correctly(self):
        """Given CONSENSUS with attribution suffix, when parsing, then direction + pct captured."""
        output = "CONSENSUS: BULLISH 65% (5/7 agents; Futurist 78%) | PRICE: $23.21"
        brief = extract_synthesis_from_output(output)
        assert brief is not None
        assert brief.consensus == "BULLISH"
        assert brief.consensus_pct == 0.65

    def test_news_score_before_suffix(self):
        """Given NEWS: <label> <score> (<reason>), when parsing, then score captured (suffix after)."""
        output = "PRICE: $23.21 | NEWS: bearish -0.5 (regulatory risk) | DATA: clean"
        brief = extract_synthesis_from_output(output)
        assert brief is not None
        assert brief.news_sentiment == -0.5


# ── NOW/NEXT two-line format tests (Wave 5) ─────────────────────────────────


class TestNowNextFormat:
    """The two-line NOW/NEXT format must parse identically to single-line.
    Parser uses re.search across the whole string, so newlines are inert.
    """

    def test_two_line_brief_parses(self):
        """Given a NOW/NEXT brief, when parsing, then all canonical fields captured."""
        brief_text = (
            "NOW: PRICE: $23.21 falling | DATA: clean (no gaps) | "
            "NEWS: neutral 0.0 (no catalysts) | STRUCTURAL: YELLOW (consolidating)\n"
            "NEXT: CONSENSUS: BEARISH 55% (4/7 agents; Futurist 78%) | "
            "TREND: DOWN 0.6 | PREDICTION: BEARISH 0.55"
        )
        brief = extract_synthesis_from_output(brief_text)
        assert brief is not None
        assert brief.price == 23.21
        assert brief.data_quality == "clean"
        assert brief.news_sentiment == 0.0
        assert brief.structural_status == "YELLOW"
        assert brief.trend_direction == "DOWN"
        assert brief.prediction_bias == "BEARISH"
        assert brief.consensus == "BEARISH"
        assert brief.consensus_pct == 0.55

    def test_section_markers_dont_collide_with_labels(self):
        """Given NOW: and NEXT: section prefixes, when parsing, then they are ignored
        (no canonical labels conflict with them)."""
        brief_text = "NOW: PRICE: $10.00 | DATA: clean\nNEXT: CONSENSUS: BULLISH 60%"
        brief = extract_synthesis_from_output(brief_text)
        assert brief is not None
        assert brief.price == 10.00
        assert brief.consensus == "BULLISH"


# ── Daily summary jargon ────────────────────────────────────────────────────


class TestDailySummaryWording:
    """The 'Learner debrief' jargon was replaced with trader-readable text."""

    def test_no_learner_debrief_jargon(self):
        """Given notifier source, when grepping, then no 'Learner debrief'."""
        notifier_path = Path(__file__).resolve().parent.parent / "notifier.py"
        text = notifier_path.read_text()
        assert "Learner debrief" not in text
        assert "Graduated lessons" not in text

    def test_replacement_text_present(self):
        """Given notifier source, when grepping, then 'Daily review done' present."""
        notifier_path = Path(__file__).resolve().parent.parent / "notifier.py"
        text = notifier_path.read_text()
        assert "Daily review done" in text
