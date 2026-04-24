"""
Horizon-aware prediction calibrator.

The existing learner.py has a bug: it scores every prediction against that
day's EOD close regardless of the prediction's stated horizon. So a 1h
prediction made at 10:00 AM gets scored against the 4:00 PM close — six
hours later. This makes MAE meaningless.

This module does it properly:

  - For each unscored prediction whose horizon has elapsed, look up the
    actual price at (timestamp + horizon) ± a small tolerance window.
  - Compute true error_pct against THAT price, not EOD close.
  - Derive directional correctness (did the call direction match reality?).
  - Roll up per-agent/per-horizon metrics into performance_scores:
      * prediction_mae_pct — mean absolute error on price target
      * direction_hit_rate — fraction where predicted direction was right
      * brier_score       — calibration of stated confidence (lower=better)

Brier definition here: each prediction yields a prob that price will rise,
derived as stated_confidence when the call is bullish, else (1 - confidence).
Outcome is 1 if price rose vs the pre-prediction baseline, else 0. Brier is
the mean squared error between prob and outcome. Perfect = 0, worst = 1,
random (0.5 prob, 50/50 outcomes) = 0.25.

Run this every ~10 min. It's pure SQL + arithmetic — no LLM, no network.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
ET = ZoneInfo("America/New_York")

# How close the tick has to be to the target time to count as a valid score.
# 5 minutes: wide enough to survive after-hours tick gaps, tight enough that
# we're genuinely measuring the horizon we claimed.
MATCH_WINDOW = timedelta(minutes=5)

# How long we'll wait for a matching tick before giving up on a prediction.
# Past this, we flag it as 'unscorable' in notes and stop retrying.
MAX_WAIT = timedelta(hours=2)


# ─── horizon parsing ──────────────────────────────────────────────────────────

def parse_horizon(h: str) -> Optional[timedelta]:
    """'1h' → 1 hour. '4h' → 4 hours. 'EOD' → None (handled separately)."""
    if not h:
        return None
    h = h.strip().lower()
    if h.endswith("h"):
        try:
            return timedelta(hours=float(h[:-1]))
        except ValueError:
            return None
    if h.endswith("m"):
        try:
            return timedelta(minutes=float(h[:-1]))
        except ValueError:
            return None
    return None  # EOD and anything else handled upstream


def target_time(made_at: datetime, horizon: str) -> Optional[datetime]:
    """When should this prediction be scored against?"""
    if horizon.strip().upper() == "EOD":
        # 4:00 PM ET on the prediction's calendar day
        made_et = made_at.astimezone(ET)
        return made_et.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = parse_horizon(horizon)
    return made_at + delta if delta else None


# ─── price lookup ─────────────────────────────────────────────────────────────

def _price_near(conn: sqlite3.Connection, symbol: str, when: datetime,
                window: timedelta = MATCH_WINDOW) -> Optional[float]:
    """Closest tick to `when` within `window`, else None.

    We take the absolute time delta in seconds — the original julianday math
    collapsed to int and returned 0 for anything under a day.
    """
    target_iso = when.astimezone(ET).isoformat()
    window_s = int(window.total_seconds())
    row = conn.execute(
        """
        SELECT close, timestamp,
               ABS(strftime('%s', timestamp) - strftime('%s', ?)) AS dt_s
        FROM price_ticks
        WHERE symbol = ?
          AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) <= ?
        ORDER BY dt_s ASC
        LIMIT 1
        """,
        (target_iso, symbol, target_iso, window_s),
    ).fetchone()
    return float(row[0]) if row else None


# ─── baseline price at prediction time ────────────────────────────────────────

def _baseline_price(conn: sqlite3.Connection, symbol: str,
                    made_at: datetime) -> Optional[float]:
    """Price when the prediction was made — needed to derive direction.

    Looks for the closest tick within ±2 min of the prediction timestamp.
    If none, falls back to the latest tick at or before made_at within 30 min.
    """
    close = _price_near(conn, symbol, made_at, timedelta(minutes=2))
    if close is not None:
        return close
    # Fallback: last tick at or before
    made_iso = made_at.astimezone(ET).isoformat()
    row = conn.execute(
        "SELECT close FROM price_ticks WHERE symbol=? AND timestamp<=? "
        "AND strftime('%s',?) - strftime('%s',timestamp) <= 1800 "
        "ORDER BY timestamp DESC LIMIT 1",
        (symbol, made_iso, made_iso),
    ).fetchone()
    return float(row[0]) if row else None


# ─── main scorer ──────────────────────────────────────────────────────────────

def score_due_predictions(db_path: str = DB_PATH, symbol: str = "GME") -> dict:
    """Backfill actual_price on every unscored prediction whose horizon elapsed.

    Returns a summary dict: {'scored': N, 'skipped_no_tick': N, 'abandoned': N}.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now = datetime.now(ET)

    unscored = conn.execute(
        "SELECT id, timestamp, horizon, predicted_price, confidence "
        "FROM predictions WHERE actual_price IS NULL "
        "ORDER BY timestamp ASC"
    ).fetchall()

    scored = 0
    skipped = 0
    abandoned = 0

    for row in unscored:
        try:
            made_at = datetime.fromisoformat(
                row["timestamp"].replace("Z", "+00:00")
            )
            if made_at.tzinfo is None:
                made_at = made_at.replace(tzinfo=ET)
        except Exception as e:
            log.warning(f"[calibration] bad timestamp on pred {row['id']}: {e}")
            continue

        target = target_time(made_at, row["horizon"] or "")
        if target is None:
            continue  # unparseable horizon — leave it alone, admin can fix

        if target > now:
            continue  # not due yet

        actual = _price_near(conn, symbol, target)

        if actual is None:
            # If we've given up waiting, mark it so the loop stops retrying.
            if now - target > MAX_WAIT:
                err_notes = (
                    f"unscorable: no tick within {MATCH_WINDOW} of "
                    f"{target.isoformat()} (abandoned after {MAX_WAIT})"
                )
                conn.execute(
                    "UPDATE predictions SET error_pct = NULL, "
                    "reasoning = COALESCE(reasoning,'') || ' [' || ? || ']' "
                    "WHERE id = ?",
                    (err_notes, row["id"]),
                )
                # Leave actual_price NULL — this is NOT a score, it's an admission
                # we couldn't score it. Better than writing a fake number.
                abandoned += 1
            else:
                skipped += 1
            continue

        predicted = float(row["predicted_price"])
        error_pct = round((actual - predicted) / predicted * 100, 4)
        conn.execute(
            "UPDATE predictions SET actual_price=?, error_pct=? WHERE id=?",
            (actual, error_pct, row["id"]),
        )
        scored += 1

    conn.commit()
    conn.close()

    log.info(
        f"[calibration] scored={scored} skipped_awaiting_tick={skipped} "
        f"abandoned={abandoned}"
    )
    return {"scored": scored, "skipped_no_tick": skipped, "abandoned": abandoned}


# ─── metrics roll-up ──────────────────────────────────────────────────────────

def compute_futurist_metrics(db_path: str = DB_PATH,
                             symbol: str = "GME",
                             lookback_days: int = 7) -> dict:
    """Roll up scored predictions into MAE / hit-rate / Brier.

    Uses only predictions where actual_price IS NOT NULL AND baseline price
    is derivable (else we can't determine direction).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, timestamp, horizon, predicted_price, confidence, "
        "       actual_price, error_pct "
        "FROM predictions "
        "WHERE actual_price IS NOT NULL "
        "  AND timestamp > datetime('now', ?) "
        "ORDER BY timestamp DESC",
        (f"-{lookback_days} days",),
    ).fetchall()

    if not rows:
        conn.close()
        return {"sample_size": 0}

    by_horizon: dict[str, dict] = {}
    overall = {"abs_errors": [], "hits": [], "brier_terms": []}

    for row in rows:
        try:
            made_at = datetime.fromisoformat(
                row["timestamp"].replace("Z", "+00:00")
            )
            if made_at.tzinfo is None:
                made_at = made_at.replace(tzinfo=ET)
        except Exception:
            continue

        baseline = _baseline_price(conn, symbol, made_at)
        if baseline is None:
            # Can't determine direction without baseline — skip for hit rate,
            # but still count toward MAE because the error is unambiguous.
            abs_err = abs(float(row["error_pct"]))
            h = row["horizon"] or "?"
            by_horizon.setdefault(h, {"abs_errors": [], "hits": [], "brier_terms": []})
            by_horizon[h]["abs_errors"].append(abs_err)
            overall["abs_errors"].append(abs_err)
            continue

        predicted = float(row["predicted_price"])
        actual = float(row["actual_price"])
        conf = float(row["confidence"] or 0.5)

        abs_err = abs(float(row["error_pct"]))
        predicted_up = predicted > baseline
        actual_up = actual > baseline
        hit = 1 if predicted_up == actual_up else 0

        # Prob the model effectively assigned to "up"
        prob_up = conf if predicted_up else (1 - conf)
        outcome = 1 if actual_up else 0
        brier = (prob_up - outcome) ** 2

        h = row["horizon"] or "?"
        bucket = by_horizon.setdefault(
            h, {"abs_errors": [], "hits": [], "brier_terms": []}
        )
        bucket["abs_errors"].append(abs_err)
        bucket["hits"].append(hit)
        bucket["brier_terms"].append(brier)
        overall["abs_errors"].append(abs_err)
        overall["hits"].append(hit)
        overall["brier_terms"].append(brier)

    conn.close()

    def _agg(bucket: dict) -> dict:
        return {
            "n": len(bucket["abs_errors"]),
            "mae_pct": round(
                sum(bucket["abs_errors"]) / len(bucket["abs_errors"]), 3
            ) if bucket["abs_errors"] else None,
            "hit_rate": round(
                sum(bucket["hits"]) / len(bucket["hits"]), 3
            ) if bucket["hits"] else None,
            "brier": round(
                sum(bucket["brier_terms"]) / len(bucket["brier_terms"]), 4
            ) if bucket["brier_terms"] else None,
        }

    return {
        "sample_size": len(overall["abs_errors"]),
        "overall": _agg(overall),
        "by_horizon": {h: _agg(b) for h, b in by_horizon.items()},
        "lookback_days": lookback_days,
    }


def write_performance_scores(db_path: str = DB_PATH,
                             lookback_days: int = 7) -> int:
    """Compute metrics and upsert them into performance_scores."""
    metrics = compute_futurist_metrics(db_path, lookback_days=lookback_days)
    if metrics.get("sample_size", 0) == 0:
        return 0

    today = datetime.now(ET).date().isoformat()
    rows_written = 0
    conn = sqlite3.connect(db_path)
    overall = metrics["overall"]
    for metric_name, value in (
        ("prediction_mae_pct", overall["mae_pct"]),
        ("direction_hit_rate", overall["hit_rate"]),
        ("brier_score", overall["brier"]),
    ):
        if value is None:
            continue
        # UPSERT — performance_scores has UNIQUE(date, agent_name, metric) so
        # rerunning the calibrator within a day must overwrite, not append.
        conn.execute(
            "INSERT INTO performance_scores "
            "(date, agent_name, metric, value, sample_size, notes) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, agent_name, metric) DO UPDATE SET "
            "  value=excluded.value, "
            "  sample_size=excluded.sample_size, "
            "  notes=excluded.notes",
            (
                today,
                "Futurist",
                metric_name,
                value,
                overall["n"],
                f"lookback={lookback_days}d, horizons={list(metrics['by_horizon'].keys())}",
            ),
        )
        rows_written += 1
    conn.commit()
    conn.close()
    return rows_written


def run_calibration_cycle(db_path: str = DB_PATH) -> dict:
    """Entry point called by the scheduler — score due preds + refresh metrics."""
    scored = score_due_predictions(db_path)
    written = write_performance_scores(db_path)
    scored["metrics_rows_written"] = written
    return scored


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    print(json.dumps(run_calibration_cycle(), indent=2))
    print(json.dumps(compute_futurist_metrics(), indent=2))
