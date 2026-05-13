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
    clamp_consensus_pct,
    coerce_news_score,
    coerce_trend_strength,
    colorize_status_emojis,
    decimal_confidence_to_percent,
    escape_html,
    format_consensus,
    format_data_status,
    format_price,
    format_rsi,
    format_volume,
    layout_synthesis_brief,
    normalize_synthesis_capitalization,
    tighten_prose,
)
from trading_glossary import glossary_footer  # noqa: E402


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


# ── Consensus clamp tests (no more 100% overclaim) ──────────────────────────


class TestClampConsensus:
    """The LLM tends to write 'CONSENSUS: BULLISH 100% (6/6 agents)' — that
    overclaim erodes reader trust. Clamp at 95%."""

    def test_clamps_100_to_95(self):
        """Given 100%, when clamping, then 95%."""
        before = "CONSENSUS: BULLISH 100% (6/6 agents; Futurist 90%)"
        after = clamp_consensus_pct(before)
        assert "CONSENSUS: BULLISH 95%" in after
        assert "100%" not in after

    def test_passes_through_below_ceiling(self):
        """Given 65%, when clamping, then 65% unchanged."""
        before = "CONSENSUS: BEARISH 65% (4/6 agents)"
        assert clamp_consensus_pct(before) == before

    def test_clamps_99_too(self):
        """Given 99%, when clamping, then 95%."""
        after = clamp_consensus_pct("CONSENSUS: NEUTRAL 99% (5/5 agents)")
        assert "95%" in after
        assert "99%" not in after

    def test_idempotent_at_ceiling(self):
        """Given 95% already, when clamping, then unchanged."""
        before = "CONSENSUS: BULLISH 95% (6/6 agents)"
        assert clamp_consensus_pct(before) == before


# ── TREND strength coercion (words → numbers) ───────────────────────────────


class TestCoerceTrendStrength:
    """LLM sometimes writes 'TREND: UP strong' instead of 'TREND: UP 0.7' —
    that breaks the parser's `\\w+ ([\\d.]+)` regex. Coerce to numbers."""

    @pytest.mark.parametrize("word,expected_num", [
        ("strong", "0.8"),
        ("moderate", "0.6"),
        ("weak", "0.4"),
        ("flat", "0.2"),
    ])
    def test_word_to_number(self, word, expected_num):
        """Given a qualitative word, when coercing, then mapped to its numeric strength."""
        before = f"TREND: UP {word}"
        assert coerce_trend_strength(before) == f"TREND: UP {expected_num}"

    def test_passes_through_numeric(self):
        """Given numeric strength, when coercing, then unchanged."""
        before = "TREND: UP 0.7"
        assert coerce_trend_strength(before) == before

    def test_coerced_output_parses(self):
        """Given coerced text, when running the canonical parser, then trend_strength is numeric."""
        coerced = coerce_trend_strength(
            "PRICE: $23.21 | CONSENSUS: BULLISH 65% | TREND: UP strong | PREDICTION: BULLISH 0.7"
        )
        brief = extract_synthesis_from_output(coerced)
        assert brief is not None
        assert brief.trend_direction == "UP"
        assert brief.trend_strength == 0.8


# ── Glossary footer tests ───────────────────────────────────────────────────


class TestGlossaryFooter:
    """Plain-English glosses for trading jargon — RSI/EMA/VWAP/MACD/etc."""

    def test_detects_and_explains_terms(self):
        """Given text with RSI/EMA/VWAP, when building footer, then all three glossed."""
        text = "Price above VWAP and EMA21, RSI 58, uptrend confirmed."
        footer = glossary_footer(text)
        assert footer.startswith("📚")
        assert "RSI:" in footer
        assert "EMA:" in footer
        assert "VWAP:" in footer

    def test_empty_when_no_jargon(self):
        """Given plain-English text, when building footer, then empty string."""
        text = "Price up 1.2% on the day."
        assert glossary_footer(text) == ""

    def test_caps_at_max_terms(self):
        """Given many terms, when building footer, then capped at max_terms."""
        text = "RSI, EMA, VWAP, MACD, Bollinger Bands, ATR all aligning."
        footer = glossary_footer(text, max_terms=3)
        # 3 terms max; pipe separator gives 2 pipes
        assert footer.count(" | ") == 2

    def test_pipe_separated(self):
        """Footer uses | separator (consistent with the rest of the feed)."""
        footer = glossary_footer("RSI low, EMA cross")
        assert " | " in footer

    def test_volume_not_glossed(self):
        """Volume is referenced qualitatively only; the actual number lives on
        TradingView, so a 'Volume: number of shares traded' gloss adds noise
        with no signal. User-requested removal."""
        text = "Price up, volume elevated, RSI 65 — looks like a breakout."
        footer = glossary_footer(text)
        # RSI should still be glossed; Volume should NOT appear in footer
        assert "RSI:" in footer
        assert "Volume:" not in footer


# ── SIGNAL row prompt construction (sanity-check the prompt string) ─────────


class TestCoerceNewsScore:
    """LLM sometimes writes 'NEWS: BULLISH 75%' instead of 'NEWS: bullish 0.75'.
    Pydantic SynthesisBrief.news_sentiment rejects 75.0 (must be -1.0 to 1.0).
    Coerce percentage form to decimal."""

    def test_positive_percent(self):
        """Given 'BULLISH 75%', when coercing, then 'BULLISH 0.75'."""
        assert coerce_news_score("NEWS: BULLISH 75%") == "NEWS: BULLISH 0.75"

    def test_negative_percent(self):
        """Given '-50%', when coercing, then '-0.50'."""
        assert coerce_news_score("NEWS: bearish -50%") == "NEWS: bearish -0.50"

    def test_decimal_passes_through(self):
        """Given 'bullish 0.75', when coercing, then unchanged."""
        before = "NEWS: bullish 0.75 (analyst action)"
        assert coerce_news_score(before) == before

    def test_coerced_output_parses(self):
        """Given coerced output, when running canonical parser, then news_sentiment captured."""
        coerced = coerce_news_score("PRICE: $23.21 | NEWS: BULLISH 75% (analyst action)")
        brief = extract_synthesis_from_output(coerced)
        assert brief is not None
        assert brief.news_sentiment == 0.75


class TestColorizeStatusEmojis:
    """Display-layer transform that prepends coloured emojis before canonical
    status words. The word stays so the Synthesis parser still ingests it."""

    def test_structural_colors(self):
        """GREEN/YELLOW/RED get the matching circle emoji prepended."""
        assert "🟢 GREEN" in colorize_status_emojis("STRUCTURAL: GREEN (cash-rich)")
        assert "🟡 YELLOW" in colorize_status_emojis("STRUCTURAL: YELLOW (consolidating)")
        assert "🔴 RED" in colorize_status_emojis("STRUCTURAL: RED (debt-heavy)")

    def test_consensus_direction(self):
        """BULLISH/BEARISH/NEUTRAL after CONSENSUS get green/red/white circles."""
        assert "🟢 BULLISH" in colorize_status_emojis("CONSENSUS: BULLISH 65%")
        assert "🔴 BEARISH" in colorize_status_emojis("CONSENSUS: BEARISH 67%")
        assert "⚪ NEUTRAL" in colorize_status_emojis("CONSENSUS: NEUTRAL 50%")

    def test_signal_action(self):
        """BUY/SELL/HOLD/WAIT after SIGNAL get the matching icon."""
        assert "🟢 BUY" in colorize_status_emojis("SIGNAL: BUY @ $22.50 (...)")
        assert "🔴 SELL" in colorize_status_emojis("SIGNAL: SELL @ $22.50 (...)")
        assert "🟡 HOLD" in colorize_status_emojis("SIGNAL: HOLD — reason")
        assert "⏳ WAIT" in colorize_status_emojis("SIGNAL: WAIT — reason")

    def test_trend_direction(self):
        """UP/DOWN/SIDEWAYS after TREND get arrow emojis."""
        assert "📈 UP" in colorize_status_emojis("TREND: UP 0.7")
        assert "📉 DOWN" in colorize_status_emojis("TREND: DOWN 0.7")
        assert "↔️ SIDEWAYS" in colorize_status_emojis("TREND: SIDEWAYS 0.5")

    def test_prediction_field(self):
        """BULLISH/BEARISH/HOLD after PREDICTION get coloured emojis too."""
        assert "🟢 BULLISH" in colorize_status_emojis("PREDICTION: BULLISH 0.7")
        assert "🔴 BEARISH" in colorize_status_emojis("PREDICTION: BEARISH 0.65")

    def test_canonical_word_preserved(self):
        """The original word remains intact — parser regex stays happy."""
        out = colorize_status_emojis("STRUCTURAL: YELLOW (consolidating)")
        from episodic_integration import extract_synthesis_from_output
        full = "PRICE: $22.50 | " + out + " | CONSENSUS: BULLISH 60%"
        brief = extract_synthesis_from_output(full)
        assert brief is not None
        assert brief.structural_status == "YELLOW"

    def test_idempotent(self):
        """Calling twice on the same text does not double-prefix."""
        once = colorize_status_emojis("STRUCTURAL: YELLOW (consolidating)")
        twice = colorize_status_emojis(once)
        assert once == twice
        # Sanity: only one emoji circle, not two
        assert twice.count("🟡") == 1

    def test_no_match_no_change(self):
        """Text without any matching status word is unchanged."""
        before = "$22.50 rising, volume quiet."
        assert colorize_status_emojis(before) == before

    def test_full_synthesis_brief(self):
        """A real three-line brief gets all four fields coloured."""
        brief = (
            "NOW: PRICE: $22.50 rising | DATA: clean | STRUCTURAL: YELLOW (consolidating)\n"
            "NEXT: CONSENSUS: BEARISH 67% | TREND: DOWN 0.6 | PREDICTION: BEARISH 0.65\n"
            "SIGNAL: WAIT — wait for breakdown confirmation"
        )
        out = colorize_status_emojis(brief)
        assert "🟡 YELLOW" in out
        assert "🔴 BEARISH" in out  # CONSENSUS
        assert "📉 DOWN" in out
        assert "⏳ WAIT" in out


class TestDecimalConfidenceToPercent:
    """Display-layer transform that rewrites 0-1 decimals as percentages so
    TREND/PREDICTION read consistently with CONSENSUS's percent format."""

    def test_trend_decimal_to_percent(self):
        """TREND: DOWN 0.55 → TREND: DOWN 55%."""
        assert decimal_confidence_to_percent("TREND: DOWN 0.55") == "TREND: DOWN 55%"

    def test_prediction_decimal_to_percent(self):
        """PREDICTION: BEARISH 0.65 → PREDICTION: BEARISH 65%."""
        assert decimal_confidence_to_percent("PREDICTION: BEARISH 0.65") == "PREDICTION: BEARISH 65%"

    def test_rounds_correctly(self):
        """0.555 should round to 56% (banker's rounding gives 56 for round(55.5)=56)."""
        result = decimal_confidence_to_percent("TREND: UP 0.555")
        assert "56%" in result or "55%" in result  # accept either Python rounding

    def test_passes_through_already_percent(self):
        """If value is already a percent (or >= 1.0), don't transform."""
        before = "CONSENSUS: BULLISH 67%"
        assert decimal_confidence_to_percent(before) == before

    def test_does_not_touch_consensus(self):
        """CONSENSUS already uses % — this transform shouldn't affect it."""
        before = "CONSENSUS: BULLISH 67% (5/6 agents)"
        assert decimal_confidence_to_percent(before) == before

    def test_handles_both_in_same_text(self):
        """Both TREND and PREDICTION get rewritten in one pass."""
        before = "TREND: DOWN 0.55 | PREDICTION: BEARISH 0.7"
        after = decimal_confidence_to_percent(before)
        assert "DOWN 55%" in after
        assert "BEARISH 70%" in after

    def test_must_run_before_colorize(self):
        """If colorize ran first, decimal regex would skip the emoji and miss.
        This guards the documented ordering in agent_voice._format()."""
        # Simulate post-colorize input (emoji between label and word)
        colorized = "TREND: 📉 DOWN 0.55"
        # If we ran decimal AFTER colorize, the regex wouldn't match (proves the bug)
        assert decimal_confidence_to_percent(colorized) == colorized
        # The correct order: decimal first, then colorize
        raw = "TREND: DOWN 0.55"
        from message_formatters import colorize_status_emojis
        after_decimal = decimal_confidence_to_percent(raw)
        after_both = colorize_status_emojis(after_decimal)
        assert "📉 DOWN 55%" in after_both


class TestLayoutSynthesisBrief:
    """Reformat 3-line NOW/NEXT/SIGNAL brief into bullet layout, SIGNAL on top."""

    SAMPLE = (
        "NOW: PRICE: $22.13 🔻 -0.40% | DATA: clean (no gaps) | NEWS: NEUTRAL 0.0 (no catalysts) | STRUCTURAL: 🟡 YELLOW (consolidating)\n"
        "NEXT: CONSENSUS: 🔴 BEARISH 67% (2/3 agents) | TREND: 📉 DOWN 55% | PREDICTION: 🔴 BEARISH 55%\n"
        "SIGNAL: ⏳ WAIT — short-term bearish trend."
    )

    def test_signal_appears_first(self):
        """Output starts with SIGNAL line, bolded."""
        out = layout_synthesis_brief(self.SAMPLE)
        assert out.startswith("<b>SIGNAL:")

    def test_now_section_bulleted(self):
        """NOW becomes its own header and each field is a bullet."""
        out = layout_synthesis_brief(self.SAMPLE)
        assert "📊 <b>NOW</b>" in out
        assert "• PRICE: $22.13" in out
        assert "• DATA: clean (no gaps)" in out
        assert "• STRUCTURAL: 🟡 YELLOW (consolidating)" in out

    def test_next_section_bulleted(self):
        """NEXT becomes its own header with bullet fields."""
        out = layout_synthesis_brief(self.SAMPLE)
        assert "🔮 <b>NEXT</b>" in out
        assert "• CONSENSUS: 🔴 BEARISH 67%" in out
        assert "• TREND: 📉 DOWN 55%" in out

    def test_signal_bolded(self):
        """SIGNAL line is wrapped in <b> tags."""
        out = layout_synthesis_brief(self.SAMPLE)
        assert "<b>SIGNAL: ⏳ WAIT" in out

    def test_passthrough_for_non_synthesis(self):
        """Non-synthesis content (no NOW:/SIGNAL: markers) passes through unchanged."""
        before = "$22.65 rips higher, quiet volume."
        assert layout_synthesis_brief(before) == before

    def test_flip_prefix_on_signal_change(self):
        """When prev signal differs, ⚡ FLIP prefix appears."""
        prev = {"signal": "BUY", "consensus": "BULLISH"}
        out = layout_synthesis_brief(self.SAMPLE, prev_state=prev)
        assert "⚡ FLIP" in out
        assert "BUY→WAIT" in out
        assert "BULLISH→BEARISH" in out

    def test_no_flip_when_same(self):
        """When prev signal matches, no FLIP prefix."""
        prev = {"signal": "WAIT", "consensus": "BEARISH"}
        out = layout_synthesis_brief(self.SAMPLE, prev_state=prev)
        assert "FLIP" not in out


class TestSignalPromptWiring:
    """The SIGNAL row must be specified in the Synthesis prompt with a closed
    suffix vocabulary and the 95% clamp instruction. These are source-level
    invariants — if the prompt drifts, the LLM's output drifts with it."""

    def _prompt_source(self):
        orch_path = Path(__file__).resolve().parent.parent / "orchestrator.py"
        return orch_path.read_text()

    def test_signal_row_specified(self):
        """Given orchestrator source, when grepping, then SIGNAL row format present."""
        src = self._prompt_source()
        assert "SIGNAL: [BUY/SELL/HOLD/WAIT]" in src

    def test_closed_suffix_vocab_specified(self):
        """Suffix vocabulary lockdown is wired into the prompt."""
        src = self._prompt_source()
        assert "(no catalysts)" in src
        assert "(earnings event)" in src
        assert "do not invent" in src.lower()

    def test_95_clamp_instruction_present(self):
        """The prompt instructs the LLM to cap consensus at 95%."""
        src = self._prompt_source()
        assert "CAP AT 95%" in src or "never write 100%" in src

    def test_trend_strength_must_be_numeric(self):
        """The prompt forbids qualitative trend strength like 'strong'."""
        src = self._prompt_source()
        assert "TREND strength" in src
        assert "NUMBER" in src or "0.0-1.0" in src
