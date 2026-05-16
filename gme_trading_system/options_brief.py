"""Persona labelling, week-over-week diffing, and shares-translation for the
Monday options brief. Pure functions over the watchlist payload shape produced
by OptionsFeed.call_contract_candidates() — easy to test, no I/O.
"""
from __future__ import annotations

from typing import Iterable

# Persona thresholds — chosen for a meme-stock-style weekly chain. Edge cases
# (deep ITM, extreme moneyness) are uncommon in the watchlist because the
# filter window is already -8% / +20%, but the labels still cover them.
DEEP_ITM_MONEYNESS = -0.05    # strike ≥5% below spot
LOTTERY_MONEYNESS = 0.04      # strike ≥4% above spot
SENSIBLE_BAND = (-0.03, 0.01)


def persona_label(candidate: dict, all_candidates: Iterable[dict]) -> tuple[str, str]:
    """Return (emoji, label) for a single candidate, given peers for IV ranking.

    Labels are heuristics — they describe the *shape* of the bet, not its quality.
    """
    moneyness = candidate.get("moneyness_pct", 0.0)
    iv = candidate.get("iv", 0.0)
    peer_ivs = sorted([c.get("iv", 0.0) for c in all_candidates])

    if len(peer_ivs) >= 3:
        high_iv_threshold = peer_ivs[int(len(peer_ivs) * 2 / 3)]
        low_iv_threshold = peer_ivs[int(len(peer_ivs) / 3)]
    else:
        high_iv_threshold = 0.50
        low_iv_threshold = 0.40

    if moneyness <= DEEP_ITM_MONEYNESS:
        return "💎", "deep ITM"
    if moneyness >= LOTTERY_MONEYNESS and iv >= high_iv_threshold:
        return "🎰", "lottery ticket"
    if SENSIBLE_BAND[0] <= moneyness <= SENSIBLE_BAND[1] and iv <= low_iv_threshold:
        return "🎯", "sensible"
    return "⚖️", "balanced"


def compute_wow_diff(
    current_candidates: list[dict],
    previous_candidates: list[dict],
) -> dict[float, dict]:
    """Per-strike WoW changes keyed by strike.

    Returns {strike: {"is_new": bool, "oi_delta_pct": float|None, "prev_oi": int|None}}.
    """
    prev_by_strike = {round(float(p["strike"]), 2): p for p in previous_candidates}
    out: dict[float, dict] = {}
    for c in current_candidates:
        strike = round(float(c["strike"]), 2)
        prev = prev_by_strike.get(strike)
        if not prev:
            out[strike] = {"is_new": True, "oi_delta_pct": None, "prev_oi": None}
            continue
        prev_oi = int(prev.get("open_interest") or 0)
        cur_oi = int(c.get("open_interest") or 0)
        delta_pct = ((cur_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else None
        out[strike] = {"is_new": False, "oi_delta_pct": delta_pct, "prev_oi": prev_oi}
    return out


def gone_strikes(
    current_candidates: list[dict],
    previous_candidates: list[dict],
) -> list[float]:
    """Strikes that were in last week's watchlist but dropped off this week."""
    current_strikes = {round(float(c["strike"]), 2) for c in current_candidates}
    return sorted(
        round(float(p["strike"]), 2)
        for p in previous_candidates
        if round(float(p["strike"]), 2) not in current_strikes
    )


def shares_translation(candidates: list[dict], vol_regime: str = "") -> str:
    """One-line takeaway for someone holding shares, not trading options.

    Heuristic — combines average moneyness, average IV, and 'lottery ticket'
    count to read the crowd's positioning tone.
    """
    if not candidates:
        return ""
    avg_moneyness = sum(c.get("moneyness_pct", 0.0) for c in candidates) / len(candidates)
    avg_iv = sum(c.get("iv", 0.0) for c in candidates) / len(candidates)
    lottery_count = sum(
        1 for c in candidates
        if c.get("moneyness_pct", 0.0) >= LOTTERY_MONEYNESS and c.get("iv", 0.0) >= 0.50
    )

    if avg_moneyness > 0.025 and avg_iv >= 0.50 and lottery_count >= 2:
        return "Crowd reaching for upside lottery tickets — bullish positioning but premium-rich. Not a rush to add shares."
    if avg_moneyness > 0.015 and avg_iv < 0.45:
        return "Crowd modestly bullish in cheap-premium calls — steady accumulation tone. Adds welcome on dips."
    if -0.01 <= avg_moneyness <= 0.025 and lottery_count == 0:
        return "Watchlist balanced across strikes — no strong directional read this week. Holding pattern."
    if avg_moneyness < -0.015:
        return "Crowd skewing defensive (ITM calls leading) — reads cautious. Wait for confirmation before adding."
    if vol_regime == "elevated" and avg_iv >= 0.45:
        return "Mixed positioning, vol-elevated regime — options premium is the costly side. Share adds on dips, not chases."
    return "Mixed positioning, no clean read this week."
