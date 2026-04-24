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


# ─── signal_alerts scoring (Pattern, Trendy, …) ───────────────────────────────
#
# signal_alerts is the common table Pattern/Trendy/Futurist all write to.
# Each row carries entry_price, take_profit, stop_loss, confidence. That's
# enough to score: within a fixed validation window (default 4h), did
# price reach TP before SL? And was the stated confidence calibrated against
# the outcome? We store one score row per signal in signal_scores — same
# anti-hallucination contract as predictions: refuse to write a score if
# there's no verified price tick to score against.

SIGNAL_VALIDATION_WINDOW = timedelta(hours=4)
SIGNAL_MAX_WAIT = timedelta(hours=24)   # after this, abandon as unscorable


def _ensure_signal_scores_table(conn: sqlite3.Connection) -> None:
    """Idempotent. Sidecar table so we don't ALTER the live signal_alerts."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_scores (
            signal_id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            signal_type TEXT,
            validated_at TEXT NOT NULL,
            baseline_price REAL,
            end_price REAL,
            tp_hit INTEGER NOT NULL DEFAULT 0,
            sl_hit INTEGER NOT NULL DEFAULT 0,
            directional_hit INTEGER NOT NULL DEFAULT 0,
            brier_term REAL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )


def _ticks_in_window(conn: sqlite3.Connection, symbol: str,
                     start: datetime, end: datetime) -> list[tuple[str, float]]:
    """All (timestamp, close) ticks in (start, end], chronological."""
    start_iso = start.astimezone(ET).isoformat()
    end_iso = end.astimezone(ET).isoformat()
    rows = conn.execute(
        "SELECT timestamp, close FROM price_ticks "
        "WHERE symbol=? AND timestamp > ? AND timestamp <= ? "
        "ORDER BY timestamp ASC",
        (symbol, start_iso, end_iso),
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def _score_one_signal(conn: sqlite3.Connection, symbol: str, row: sqlite3.Row,
                      now: datetime) -> Optional[dict]:
    """Return a dict ready for INSERT into signal_scores, or None if not yet
    ready / impossible to score honestly."""
    try:
        made_at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if made_at.tzinfo is None:
            made_at = made_at.replace(tzinfo=ET)
    except Exception as e:
        log.warning(f"[calibration] bad timestamp on signal {row['id']}: {e}")
        return None

    window_end = made_at + SIGNAL_VALIDATION_WINDOW
    if window_end > now:
        return None  # window hasn't closed yet

    baseline = _price_near(conn, symbol, made_at, timedelta(minutes=5))
    if baseline is None:
        # Fallback: trust the signal's declared entry_price if no tick found.
        # This is second-best — the tick is ground truth — but better than
        # dropping signals made during tick gaps entirely.
        if row["entry_price"] is not None:
            baseline = float(row["entry_price"])

    ticks = _ticks_in_window(conn, symbol, made_at, window_end)
    if not ticks:
        # No price data in the whole 4h window. If we've waited long enough,
        # abandon. Otherwise come back later.
        if now - window_end > SIGNAL_MAX_WAIT:
            return {
                "signal_id": row["id"],
                "agent_name": row["agent_name"],
                "signal_type": row["signal_type"],
                "validated_at": now.isoformat(),
                "baseline_price": baseline,
                "end_price": None,
                "tp_hit": 0,
                "sl_hit": 0,
                "directional_hit": 0,
                "brier_term": None,
                "notes": f"unscorable: no ticks in window {made_at.isoformat()}"
                         f" .. {window_end.isoformat()} (abandoned)",
            }
        return None

    end_price = ticks[-1][1]
    tp = float(row["take_profit"]) if row["take_profit"] is not None else None
    sl = float(row["stop_loss"]) if row["stop_loss"] is not None else None

    # Direction the signal implied: bullish if TP > entry, bearish if TP < entry.
    # If TP isn't set, fall back to conf as a pure directional signal with
    # default-bullish (rare — Pattern/Trendy/Futurist all set TP).
    entry_declared = float(row["entry_price"]) if row["entry_price"] else baseline
    if tp is not None and entry_declared is not None:
        bullish = tp > entry_declared
    else:
        bullish = True  # conservative default; will still be graded on outcome

    # First-touch check — did price hit TP or SL during the window?
    tp_hit = 0
    sl_hit = 0
    if tp is not None or sl is not None:
        for _, price in ticks:
            if bullish:
                if tp is not None and price >= tp:
                    tp_hit = 1
                    break
                if sl is not None and price <= sl:
                    sl_hit = 1
                    break
            else:
                if tp is not None and price <= tp:
                    tp_hit = 1
                    break
                if sl is not None and price >= sl:
                    sl_hit = 1
                    break

    # Outcome resolution — first-touch wins. This matches how a human team
    # would actually experience the signal: if you stopped out, you lost
    # money, regardless of what the price did after your stop triggered.
    #   tp_hit → signal's target was reached → outcome = signal direction
    #   sl_hit → stopped out before target  → outcome = opposite direction
    #   neither → fall back to end-of-window vs baseline
    if tp_hit:
        moved_up = bullish                  # win in signal direction
    elif sl_hit:
        moved_up = not bullish              # forced out the opposite way
    elif baseline is not None:
        moved_up = end_price > baseline     # end-of-window direction
    else:
        moved_up = None                     # can't resolve honestly

    if moved_up is None:
        directional_hit = tp_hit            # best we can do
        brier_term: Optional[float] = None
    else:
        directional_hit = 1 if moved_up == bullish else 0
        conf = float(row["confidence"] or 0.5)
        prob_up = conf if bullish else (1 - conf)
        outcome = 1 if moved_up else 0
        brier_term = round((prob_up - outcome) ** 2, 4)

    notes = (
        f"window={made_at.isoformat()}..{window_end.isoformat()}, "
        f"ticks={len(ticks)}, dir={'bull' if bullish else 'bear'}"
    )

    return {
        "signal_id": row["id"],
        "agent_name": row["agent_name"],
        "signal_type": row["signal_type"],
        "validated_at": now.isoformat(),
        "baseline_price": baseline,
        "end_price": end_price,
        "tp_hit": tp_hit,
        "sl_hit": sl_hit,
        "directional_hit": directional_hit,
        "brier_term": brier_term,
        "notes": notes,
    }


def score_due_signals(db_path: str = DB_PATH, symbol: str = "GME") -> dict:
    """Score every signal_alerts row whose 4h window has closed and which
    doesn't yet have a row in signal_scores. Returns a summary dict.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_signal_scores_table(conn)
    now = datetime.now(ET)

    due = conn.execute(
        """
        SELECT s.id, s.agent_name, s.signal_type, s.confidence, s.entry_price,
               s.take_profit, s.stop_loss, s.timestamp
        FROM signal_alerts s
        LEFT JOIN signal_scores sc ON sc.signal_id = s.id
        WHERE sc.signal_id IS NULL
        ORDER BY s.timestamp ASC
        """
    ).fetchall()

    scored = 0
    skipped = 0
    abandoned = 0

    for row in due:
        result = _score_one_signal(conn, symbol, row, now)
        if result is None:
            skipped += 1
            continue
        if result.get("end_price") is None and result.get("notes", "").startswith("unscorable"):
            abandoned += 1

        conn.execute(
            "INSERT OR REPLACE INTO signal_scores "
            "(signal_id, agent_name, signal_type, validated_at, "
            " baseline_price, end_price, tp_hit, sl_hit, directional_hit, "
            " brier_term, notes) "
            "VALUES (:signal_id, :agent_name, :signal_type, :validated_at, "
            "        :baseline_price, :end_price, :tp_hit, :sl_hit, "
            "        :directional_hit, :brier_term, :notes)",
            result,
        )
        scored += 1

    conn.commit()
    conn.close()

    log.info(f"[calibration:signals] scored={scored} skipped={skipped} "
             f"abandoned={abandoned}")
    return {"signals_scored": scored, "signals_skipped": skipped,
            "signals_abandoned": abandoned}


def compute_agent_signal_metrics(db_path: str = DB_PATH,
                                 agent_name: str = "",
                                 lookback_days: int = 7) -> dict:
    """Roll up signal_scores for one agent. Returns MAE-from-TP, hit_rate,
    brier, and first-touch TP/SL rates.

    'Directional hit' is our primary accuracy metric — did price move in the
    signal's direction within 4h. 'TP-before-SL' is a stricter target.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_signal_scores_table(conn)

    rows = conn.execute(
        """
        SELECT sc.directional_hit, sc.brier_term, sc.tp_hit, sc.sl_hit,
               sc.baseline_price, sc.end_price
        FROM signal_scores sc
        WHERE sc.agent_name = ?
          AND sc.validated_at > datetime('now', ?)
          AND sc.brier_term IS NOT NULL
        """,
        (agent_name, f"-{lookback_days} days"),
    ).fetchall()
    conn.close()

    n = len(rows)
    if n == 0:
        return {"sample_size": 0, "agent": agent_name}

    hits = [r["directional_hit"] for r in rows]
    briers = [r["brier_term"] for r in rows if r["brier_term"] is not None]
    tp_hits = [r["tp_hit"] for r in rows]
    sl_hits = [r["sl_hit"] for r in rows]
    # MAE: absolute % move from baseline to end, as a rough dispersion metric
    abs_moves = []
    for r in rows:
        if r["baseline_price"] and r["end_price"]:
            abs_moves.append(abs(r["end_price"] - r["baseline_price"]) / r["baseline_price"] * 100)

    return {
        "agent": agent_name,
        "sample_size": n,
        "hit_rate": round(sum(hits) / n, 3),
        "brier": round(sum(briers) / len(briers), 4) if briers else None,
        "tp_hit_rate": round(sum(tp_hits) / n, 3),
        "sl_hit_rate": round(sum(sl_hits) / n, 3),
        "avg_abs_move_pct": round(sum(abs_moves) / len(abs_moves), 3) if abs_moves else None,
        "lookback_days": lookback_days,
    }


def write_signal_performance_scores(db_path: str = DB_PATH,
                                    lookback_days: int = 7) -> int:
    """UPSERT per-agent signal metrics into performance_scores.

    Covers every agent that has rows in signal_scores. Each agent gets three
    rows per run-day: direction_hit_rate, brier_score, tp_hit_rate.
    """
    conn = sqlite3.connect(db_path)
    agents = [r[0] for r in conn.execute(
        "SELECT DISTINCT agent_name FROM signal_scores").fetchall()]
    conn.close()

    today = datetime.now(ET).date().isoformat()
    written = 0
    conn = sqlite3.connect(db_path)
    for agent in agents:
        m = compute_agent_signal_metrics(db_path, agent_name=agent,
                                         lookback_days=lookback_days)
        if m.get("sample_size", 0) == 0:
            continue
        for metric_name, value in (
            ("direction_hit_rate", m["hit_rate"]),
            ("brier_score", m["brier"]),
            ("tp_hit_rate", m["tp_hit_rate"]),
        ):
            if value is None:
                continue
            conn.execute(
                "INSERT INTO performance_scores "
                "(date, agent_name, metric, value, sample_size, notes) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date, agent_name, metric) DO UPDATE SET "
                "  value=excluded.value, sample_size=excluded.sample_size, "
                "  notes=excluded.notes",
                (today, agent, metric_name, value, m["sample_size"],
                 f"source=signal_alerts, window={SIGNAL_VALIDATION_WINDOW}, "
                 f"lookback={lookback_days}d"),
            )
            written += 1
    conn.commit()
    conn.close()
    return written


def run_calibration_cycle(db_path: str = DB_PATH) -> dict:
    """Entry point called by the scheduler. Three phases, all independent:
      1. Backfill actual_price on Futurist predictions (price target regression).
      2. Score signal_alerts from Pattern/Trendy/Futurist into signal_scores
         (directional classification + first-touch TP/SL + Brier).
      3. Roll up both into performance_scores with an UPSERT.
    """
    preds = score_due_predictions(db_path)
    sigs = score_due_signals(db_path)

    written_futurist = write_performance_scores(db_path)
    written_signals = write_signal_performance_scores(db_path)

    return {
        **preds,
        **sigs,
        "metrics_rows_written": written_futurist + written_signals,
        "by_source": {
            "futurist_predictions": written_futurist,
            "signal_alerts": written_signals,
        },
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    print(json.dumps(run_calibration_cycle(), indent=2, default=str))
    print("Futurist metrics:")
    print(json.dumps(compute_futurist_metrics(), indent=2, default=str))
    print("Per-agent signal metrics:")
    conn = sqlite3.connect(DB_PATH)
    agents = [r[0] for r in conn.execute(
        "SELECT DISTINCT agent_name FROM signal_scores").fetchall()]
    conn.close()
    for a in agents:
        print(json.dumps(compute_agent_signal_metrics(agent_name=a),
                         indent=2, default=str))
