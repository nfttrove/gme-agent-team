"""
Single source of truth for current market state.

All agents that reference price or direction MUST use get_market_fact() to
avoid hallucinating direction based on sentiment instead of actual price data.

Usage:
    from market_state import get_market_fact
    fact = get_market_fact()
    # Inject fact['prompt_line'] into agent task description
"""
import os
import sqlite3
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


def get_market_fact(symbol: str = "GME", db_path: Optional[str] = None) -> dict:
    """Return current market state as a verified fact.

    Returns:
        {
            'price': float or None,           # Latest tick price
            'timestamp': str or None,         # Latest tick timestamp
            'prev_close': float or None,      # Yesterday's close (previous trading day)
            'pct_change': float,              # Percent change vs prev_close
            'direction': str,                 # 'RISING' | 'FALLING' | 'SIDEWAYS' | 'UNKNOWN'
            'prompt_line': str,               # Pre-formatted line for LLM prompts
        }

    Agents should reference prompt_line verbatim rather than inferring direction
    from context. This prevents hallucination from bullish/bearish sentiment.
    """
    path = db_path or DB_PATH
    result = {
        'price': None,
        'timestamp': None,
        'prev_close': None,
        'pct_change': 0.0,
        'direction': 'UNKNOWN',
        'today_low': None,
        'today_high': None,
        'today_ticks': 0,
        'range_5d_low': None,
        'range_5d_high': None,
        'prompt_line': 'MARKET FACT: price data unavailable',
    }

    try:
        conn = sqlite3.connect(path)
        price_row = conn.execute(
            "SELECT close, timestamp FROM price_ticks WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        prev_close_row = conn.execute(
            "SELECT close FROM price_ticks WHERE symbol=? AND date(timestamp) < date('now') "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        today_row = conn.execute(
            "SELECT MIN(close), MAX(close), COUNT(*) FROM price_ticks "
            "WHERE symbol=? AND date(timestamp)=date('now','localtime')",
            (symbol,),
        ).fetchone()
        r5_row = conn.execute(
            "SELECT MIN(close), MAX(close) FROM price_ticks "
            "WHERE symbol=? AND timestamp >= datetime('now','-5 days')",
            (symbol,),
        ).fetchone()
        conn.close()

        if not price_row:
            return result

        result['price'] = float(price_row[0])
        result['timestamp'] = price_row[1]

        if prev_close_row:
            result['prev_close'] = float(prev_close_row[0])
            if result['prev_close']:
                result['pct_change'] = (result['price'] - result['prev_close']) / result['prev_close'] * 100

        if today_row and today_row[0] is not None:
            result['today_low'] = float(today_row[0])
            result['today_high'] = float(today_row[1])
            result['today_ticks'] = int(today_row[2] or 0)

        if r5_row and r5_row[0] is not None:
            result['range_5d_low'] = float(r5_row[0])
            result['range_5d_high'] = float(r5_row[1])

        # Calculate direction from actual price movement
        if result['pct_change'] > 0.5:
            result['direction'] = 'RISING'
        elif result['pct_change'] < -0.5:
            result['direction'] = 'FALLING'
        else:
            result['direction'] = 'SIDEWAYS'

        # Pre-format the prompt line that agents must reference
        lines = []
        if result['prev_close']:
            lines.append(
                f"MARKET FACT (verified from price_ticks, DO NOT contradict): "
                f"{symbol} ${result['price']:.2f} — {result['direction']} "
                f"{result['pct_change']:+.2f}% vs yesterday's close of ${result['prev_close']:.2f}"
            )
        else:
            lines.append(
                f"MARKET FACT (verified from price_ticks): "
                f"{symbol} ${result['price']:.2f} (no prior-day baseline available)"
            )
        if result['today_low'] is not None:
            lines.append(
                f"  Today's range: ${result['today_low']:.2f}–${result['today_high']:.2f} "
                f"({result['today_ticks']} ticks)"
            )
        if result['range_5d_low'] is not None:
            lines.append(
                f"  5-day range: ${result['range_5d_low']:.2f}–${result['range_5d_high']:.2f}"
            )
            lines.append(
                "  RULE: cite today's range verbatim. Any support/resistance you name must "
                "fall within the 5-day range — do NOT round to invented levels outside it."
            )
        result['prompt_line'] = "\n".join(lines)

    except Exception as e:
        result['prompt_line'] = f"MARKET FACT: error fetching price data ({e})"

    return result


def enforce_direction(text: str, fact: dict) -> str:
    """Post-process agent output to replace hallucinated direction lines.

    Looks for common direction phrases and swaps them with the verified one.
    Use this as a safety net after LLM output.
    """
    import re

    if fact['direction'] == 'UNKNOWN' or fact['price'] is None:
        return text

    direction_lower = fact['direction'].lower()
    price = fact['price']

    # Replace MARKET line in brief-style output
    text = re.sub(
        r'📍 MARKET:.*?It is (?:rising|falling|sideways|up|down|climbing|dropping|flat) today\.',
        f'📍 MARKET: GME is at ${price:.2f}. It is {direction_lower} today.',
        text,
        flags=re.IGNORECASE,
    )

    return text


def enforce_levels(text: str, fact: dict) -> str:
    """Scrub fabricated range / support / resistance claims.

    - Today's range is fully verifiable → always replace with actual low/high.
    - Support/resistance are subjective, but values outside the 5-day range
      are definitively wrong → clamp to the 5-day bound.
    """
    import re

    today_low = fact.get('today_low')
    today_high = fact.get('today_high')
    r5_low = fact.get('range_5d_low')
    r5_high = fact.get('range_5d_high')

    if today_low is not None and today_high is not None:
        text = re.sub(
            r"Today['\u2019]s range:?\s*\$?\d+(?:\.\d+)?\s*(?:to|\u2013|-)\s*\$?\d+(?:\.\d+)?",
            f"Today's range: ${today_low:.2f} to ${today_high:.2f}",
            text,
            flags=re.IGNORECASE,
        )

    if r5_high is not None:
        def _clamp_resistance(m):
            val = float(m.group(1))
            if val > r5_high:
                return f"Resistance at ${r5_high:.2f}"
            return m.group(0)
        text = re.sub(
            r"Resistance at \$?(\d+(?:\.\d+)?)",
            _clamp_resistance,
            text,
            flags=re.IGNORECASE,
        )

    if r5_low is not None:
        def _clamp_support(m):
            val = float(m.group(1))
            if val < r5_low:
                return f"Support at ${r5_low:.2f}"
            return m.group(0)
        text = re.sub(
            r"Support at \$?(\d+(?:\.\d+)?)",
            _clamp_support,
            text,
            flags=re.IGNORECASE,
        )

    return text
