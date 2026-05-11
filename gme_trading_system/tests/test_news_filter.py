"""
Tests for news_filter — GME ticker disambiguation.

Each test names a real-world false-positive scenario observed in production
(Global Medical Equipment, Graduate Medical Education) plus the obvious
true-positive case (GameStop-specific terms). The deny-list is permissive
by default; tests pin the boundary.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from news_filter import is_gme_relevant, filter_articles  # noqa: E402


class TestIsGmeRelevant:

    def test_gamestop_headline_is_included(self):
        """
        Given a headline mentioning GameStop directly
        When is_gme_relevant runs
        Then it returns True — the article is about the company.
        """
        assert is_gme_relevant("GameStop reports Q1 earnings beat") is True

    def test_ryan_cohen_mention_is_included(self):
        """Cohen-named articles are GameStop-relevant by current thesis."""
        assert is_gme_relevant("Ryan Cohen tweets crypto reference, fans speculate") is True

    def test_global_medical_equipment_is_excluded(self):
        """
        Given a healthcare industry headline citing 'Global Medical Equipment (GME)'
        When is_gme_relevant runs
        Then it returns False.

        Why this matters: this exact false positive showed up in the news
        feed and contaminated the Newsie sentiment composite.
        """
        assert is_gme_relevant(
            "Global Medical Equipment (GME) market projected to reach $X by 2030"
        ) is False

    def test_graduate_medical_education_is_excluded(self):
        """Same pattern — academic abbreviation collision."""
        assert is_gme_relevant(
            "ACGME updates Graduate Medical Education (GME) accreditation standards"
        ) is False

    def test_bare_gme_with_no_context_is_included_by_default(self):
        """
        Given an article that says only 'GME' with no disambiguation
        When is_gme_relevant runs
        Then it returns True (permissive default).

        Why this matters: most real GameStop coverage uses the ticker
        alone in headlines like 'GME pops 8% on heavy volume'. We can't
        over-filter — being permissive on the bare ticker and growing the
        deny-list explicitly is the safer trade-off.
        """
        assert is_gme_relevant("GME pops 8% on heavy volume") is True

    def test_deny_list_wins_over_summary_mentioning_gamestop(self):
        """
        Edge case: an article whose headline names a non-GME abbreviation
        but whose summary mentions GameStop in passing.

        Current rule: positive signal (GameStop in summary) wins. The user
        can choose to swap precedence later, but for now we err on inclusion.
        """
        # Headline alone is medical, but summary mentions GameStop → include
        assert is_gme_relevant(
            "Global Medical Equipment market hits new high",
            summary="Analysts note this is unrelated to GameStop's GME ticker, "
                    "which also rallied today.",
        ) is True

    def test_case_insensitive(self):
        """Filter must work on mixed-case input."""
        assert is_gme_relevant("GAMESTOP CORP UPDATES OUTLOOK") is True
        assert is_gme_relevant("GLOBAL MEDICAL EQUIPMENT REPORTS GROWTH") is False

    def test_summary_is_searched_too(self):
        """A headline without obvious GME context can be saved by the summary."""
        assert is_gme_relevant(
            "Activist investor buys retailer stake",
            summary="Ryan Cohen disclosed position in GameStop today.",
        ) is True


class TestFilterArticles:

    def test_drops_non_gme_articles_preserves_order(self):
        """
        Given a mixed list of GameStop and non-GME articles
        When filter_articles runs
        Then only GameStop-relevant articles remain, in original order.
        """
        articles = [
            {"headline": "GameStop Q1 earnings beat", "summary": ""},
            {"headline": "Global Medical Equipment industry report", "summary": ""},
            {"headline": "Ryan Cohen tweets again", "summary": ""},
            {"headline": "Graduate Medical Education funding bill", "summary": ""},
        ]
        out = filter_articles(articles)
        assert len(out) == 2
        assert out[0]["headline"] == "GameStop Q1 earnings beat"
        assert out[1]["headline"] == "Ryan Cohen tweets again"

    def test_passes_through_error_articles_unfiltered(self):
        """
        Given an article-list containing a circuit-open sentinel
        When filter_articles runs
        Then the sentinel is preserved — error rows are the caller's concern.
        """
        articles = [
            {"error": "finnhub circuit open"},
            {"headline": "Global Medical Equipment update", "summary": ""},
            {"headline": "GameStop news", "summary": ""},
        ]
        out = filter_articles(articles)
        assert len(out) == 2
        assert out[0] == {"error": "finnhub circuit open"}
        assert out[1]["headline"] == "GameStop news"

    def test_empty_list_returns_empty_list(self):
        assert filter_articles([]) == []
