"""Twitter monitor backend-fallback contract.

Verifies that:
  - SupabaseEdgeClient raises EdgeUnavailable on transport / non-200 / shape errors
  - TwitterMonitor.scan_all populates last_scan_stats with tried/failed/posts
  - TwitterMonitor falls through to Nitter when the primary backend raises
    (preventing the silent-rot pattern where Edge timeouts looked like 'no posts')
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twitter_monitor import EdgeUnavailable, NitterFallback, SupabaseEdgeClient  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class TestSupabaseEdgeClientRaisesOnFailure:
    """Edge function failures must raise EdgeUnavailable so the caller can
    fall back. Returning [] on failure (the old behavior) was the bug — it
    looked indistinguishable from a successful 'no posts' response."""

    def _client(self):
        return SupabaseEdgeClient("https://abc.supabase.co", "fake-key")

    def test_network_exception_raises(self):
        with patch("twitter_monitor.requests.get", side_effect=ConnectionError("boom")):
            with pytest.raises(EdgeUnavailable):
                self._client().get_recent_tweets("ryancohen")

    def test_non_200_raises(self):
        with patch("twitter_monitor.requests.get",
                    return_value=_FakeResponse(503, text="upstream timeout")):
            with pytest.raises(EdgeUnavailable):
                self._client().get_recent_tweets("ryancohen")

    def test_non_json_raises(self):
        with patch("twitter_monitor.requests.get",
                    return_value=_FakeResponse(200, json_data=ValueError("bad"))):
            with pytest.raises(EdgeUnavailable):
                self._client().get_recent_tweets("ryancohen")

    def test_unexpected_shape_raises(self):
        # API returns a dict with no 'tweets' key and no list shape
        with patch("twitter_monitor.requests.get",
                    return_value=_FakeResponse(200, json_data={"error": "rate limited"})):
            with pytest.raises(EdgeUnavailable):
                self._client().get_recent_tweets("ryancohen")

    def test_empty_list_returns_empty_not_raises(self):
        """A successful call with zero posts is NOT a failure — return []."""
        with patch("twitter_monitor.requests.get",
                    return_value=_FakeResponse(200, json_data={"tweets": []})):
            result = self._client().get_recent_tweets("ryancohen")
            assert result == []

    def test_well_formed_response_parsed(self):
        with patch("twitter_monitor.requests.get",
                    return_value=_FakeResponse(200, json_data={"tweets": [
                        {"id": "1", "text": "hello", "created_at": "2026-05-14T12:00:00Z"},
                    ]})):
            result = self._client().get_recent_tweets("ryancohen")
            assert len(result) == 1
            assert result[0]["text"] == "hello"


class TestTwitterMonitorBackendFallback:
    """When primary backend raises EdgeUnavailable, monitor falls back to Nitter
    so a broken Supabase function doesn't blackhole the whole feed."""

    def _make_monitor(self, monkeypatch, use_edge=True, use_x=False):
        """Build a TwitterMonitor wired with a Supabase Edge primary, no X API."""
        monkeypatch.setenv("X_BEARER_TOKEN", "x-key" if use_x else "")
        monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co" if use_edge else "")
        monkeypatch.setenv("SUPABASE_KEY", "fake-key" if use_edge else "")
        # Reload the module to pick up env changes
        import importlib
        import twitter_monitor as tm
        importlib.reload(tm)
        # Patch DB write paths so we don't need a real sqlite
        monkeypatch.setattr(tm.TwitterMonitor, "_ensure_tables", lambda self: None)
        monkeypatch.setattr(tm.TwitterMonitor, "_already_stored", lambda self, _id: False)
        monkeypatch.setattr(tm.TwitterMonitor, "_store_tweet", lambda *a, **k: None)
        monkeypatch.setattr(tm.TwitterMonitor, "_should_notify", lambda *a, **k: False)
        # Tiny TRACKED_ACCOUNTS for fast tests
        tm.TRACKED_ACCOUNTS = {
            "ryancohen": {"display": "Ryan", "weight": 1.0, "alert_level": "INFO",
                            "keywords_bullish": [], "keywords_bearish": []}
        }
        # No actual sleep between accounts in tests
        monkeypatch.setattr(tm.time, "sleep", lambda _s: None)
        return tm

    def test_edge_failure_falls_back_to_nitter(self, monkeypatch):
        tm = self._make_monitor(monkeypatch)
        monitor = tm.TwitterMonitor()
        assert monitor._edge is not None  # Supabase Edge configured

        # Edge raises, Nitter returns one tweet
        monkeypatch.setattr(monitor._edge, "get_recent_tweets",
                              lambda *a, **k: (_ for _ in ()).throw(tm.EdgeUnavailable("HTTP 503")))
        monkeypatch.setattr(monitor._fallback, "get_recent_tweets",
                              lambda *a, **k: [{"id": "1", "text": "RC posted",
                                                 "created_at": "2026-05-14"}])

        results = monitor.scan_all()

        assert len(results) == 1
        assert results[0]["text"] == "RC posted"
        assert monitor.last_scan_stats == {"tried": 1, "failed": 0, "posts": 1}

    def test_edge_success_with_zero_posts_does_not_invoke_fallback(self, monkeypatch):
        """Genuine 'no posts' must not trigger Nitter — that would mask the
        silent state we want to preserve."""
        tm = self._make_monitor(monkeypatch)
        monitor = tm.TwitterMonitor()

        edge_calls = []
        nitter_calls = []
        monkeypatch.setattr(monitor._edge, "get_recent_tweets",
                              lambda *a, **k: (edge_calls.append(1), [])[1])
        monkeypatch.setattr(monitor._fallback, "get_recent_tweets",
                              lambda *a, **k: (nitter_calls.append(1), [])[1])

        results = monitor.scan_all()

        assert results == []
        assert len(edge_calls) == 1
        assert len(nitter_calls) == 0  # Nitter NOT called — Edge succeeded with []
        assert monitor.last_scan_stats == {"tried": 1, "failed": 0, "posts": 0}

    def test_both_backends_fail_records_failure(self, monkeypatch):
        """If Edge AND Nitter both raise, the account counts as failed —
        run_social_scan uses this to log status='error' instead of pretending
        everything was fine."""
        tm = self._make_monitor(monkeypatch)
        monitor = tm.TwitterMonitor()

        monkeypatch.setattr(monitor._edge, "get_recent_tweets",
                              lambda *a, **k: (_ for _ in ()).throw(tm.EdgeUnavailable("dead")))

        def nitter_dies(*_a, **_k):
            raise ConnectionError("nitter offline")
        monkeypatch.setattr(monitor._fallback, "get_recent_tweets", nitter_dies)

        results = monitor.scan_all()

        assert results == []
        assert monitor.last_scan_stats == {"tried": 1, "failed": 1, "posts": 0}
