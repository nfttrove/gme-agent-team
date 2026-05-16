"""Saturday review fixes: DV list cap + accuracy-driven focus."""
import sqlite3
import sys
import os

# orchestrator imports many heavy modules at import time; pull in the helpers
# directly via path so we can unit-test the pure functions.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _seed_dv(db, snapshot_date: str, tickers: list[tuple[str, float, str, float]]):
    """Seed dv_score_history with one snapshot."""
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dv_score_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                score_date TEXT, ticker TEXT, score REAL, rating TEXT,
                price_at_score REAL
            );
        """)
        conn.executemany(
            "INSERT INTO dv_score_history (score_date, ticker, score, rating, price_at_score) VALUES (?,?,?,?,?)",
            [(snapshot_date, t, s, r, p) for t, s, r, p in tickers],
        )


def test_compose_dv_section_caps_at_top_n_and_appends_overflow_hint(tmp_path):
    from orchestrator import _compose_dv_section
    db = tmp_path / "dv.db"
    tickers = [(f"T{i:02d}", float(100 - i), "★★★☆☆", 50.0 + i) for i in range(30)]
    _seed_dv(db, "2026-05-16", tickers)

    with sqlite3.connect(db) as conn:
        out = _compose_dv_section(conn, top_n=15)

    # 15 ticker rows + the overflow hint + the header = 17 lines
    assert out.count("\n• ") == 15
    assert "+ 15 more — use /dv for full list" in out
    # Highest-scored should appear, lowest should not
    assert "T00" in out
    assert "T29" not in out


def test_compose_dv_section_no_overflow_hint_when_below_cap(tmp_path):
    from orchestrator import _compose_dv_section
    db = tmp_path / "dv.db"
    tickers = [(f"T{i:02d}", float(100 - i), "★★★☆☆", 50.0 + i) for i in range(8)]
    _seed_dv(db, "2026-05-16", tickers)

    with sqlite3.connect(db) as conn:
        out = _compose_dv_section(conn, top_n=15)

    assert out.count("\n• ") == 8
    assert "more — use /dv" not in out


def test_compose_dv_section_uncapped_default_shows_all(tmp_path):
    from orchestrator import _compose_dv_section
    db = tmp_path / "dv.db"
    tickers = [(f"T{i:02d}", float(100 - i), "★★★☆☆", 50.0 + i) for i in range(30)]
    _seed_dv(db, "2026-05-16", tickers)

    with sqlite3.connect(db) as conn:
        out = _compose_dv_section(conn)  # top_n=None → no cap

    assert out.count("\n• ") == 30
    assert "more — use /dv" not in out
