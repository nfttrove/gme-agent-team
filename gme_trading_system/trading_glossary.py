"""Trading term glossary for signal explanations.

Provides concise, eloquent definitions of technical trading terms that appear
in agent signal reasoning. Used for inline emoji-prefix explanations in
Telegram alerts and detailed `/explain` command responses.
"""
import re
from typing import Optional, Dict, Set

# Curated trading terms with emoji-friendly 1-line definitions
TRADING_GLOSSARY: Dict[str, str] = {
    "RSI": "momentum gauge; >70 overbought, <30 oversold",
    "EMA": "recent-weighted moving average",
    "VWAP": "volume-weighted fair-value benchmark",
    "ATR": "volatility size",
    "MACD": "momentum-trend oscillator",
    "Bollinger Bands": "volatility bands around a moving average",
    "BB": "volatility bands around a moving average",
    # "Volume" intentionally NOT glossed — agents only reference it qualitatively
    # (vol quiet / elevated / spike) and the actual number lives on TradingView,
    # so a "Volume: number of shares traded" footer adds noise with no signal.
    "Support": "price floor where buyers usually step in",
    "Resistance": "price ceiling where sellers usually step in",
    "Oversold": "stretched downside; bounce likely",
    "Overbought": "stretched upside; pullback likely",
}

# Emoji mappings for different term categories
TERM_EMOJIS = {
    "RSI": "📊",
    "EMA": "📈",
    "VWAP": "💰",
    "ATR": "📉",
    "MACD": "🔄",
    "Bollinger Bands": "🎯",
    "BB": "🎯",
    "Support": "🛡️",
    "Resistance": "⚡",
    "Oversold": "⬇️",
    "Overbought": "⬆️",
}


def get_definition(term: str) -> Optional[str]:
    """Lookup definition for a term. Returns None if not found."""
    return TRADING_GLOSSARY.get(term)


def detect_terms(text: str) -> Set[str]:
    """Find all trading terms mentioned in text (case-insensitive).

    Also matches indicator-with-period variants like EMA21, EMA50, RSI14
    (agents commonly write these). The base term is returned in the set.
    """
    found = set()
    text_lower = text.lower()

    # Check each term in glossary (longer terms first to avoid partial matches)
    for term in sorted(TRADING_GLOSSARY.keys(), key=len, reverse=True):
        # Match exact term or term-with-digits-suffix (e.g. EMA, EMA21, RSI14)
        pattern = r'\b' + re.escape(term.lower()) + r'\d*\b'
        if re.search(pattern, text_lower):
            found.add(term)

    return found


def add_emoji_definitions(text: str) -> str:
    """Transform text by adding emoji-prefix definitions for trading terms.

    Example:
        Input:  "RSI oversold, EMA above VWAP"
        Output: "📊 RSI: momentum indicator — oversold, 📈 EMA: exponential moving
                 average — above 💰 VWAP: volume-weighted average price"

    Handles:
    - Term replacement with emoji prefix and definition
    - Preserves original sentence structure where possible
    - Multiple occurrences of same term (all get explained)
    """
    result = text

    # Process each term found in text
    terms_found = detect_terms(text)

    for term in sorted(terms_found, key=len, reverse=True):
        definition = get_definition(term)
        if not definition:
            continue

        emoji = TERM_EMOJIS.get(term, "📌")

        # Create replacement: emoji Term: definition
        replacement = f"{emoji} {term}: {definition}"

        # Replace all occurrences of the term (case-insensitive)
        pattern = r'\b' + re.escape(term) + r'\b'
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    return result


def glossary_footer(text: str, max_terms: int = 5) -> str:
    """Build a compact one-line footer with plain-English glosses for jargon in text.

    Use this for agent_voice forwarding so a retail reader who doesn't know
    RSI/EMA/VWAP/MACD still understands what the agent is talking about.
    Returns empty string if no glossary terms detected — keeps non-technical
    messages unchanged.

    Example:
        Input:  "Price above VWAP and EMA21, RSI 58, uptrend confirmed."
        Output: "VWAP: volume-weighted fair-value benchmark | EMA: recent-weighted moving avg | RSI: momentum (>70 overbought, <30 oversold)"
    """
    terms = detect_terms(text)
    if not terms:
        return ""
    parts = []
    for term in sorted(terms, key=len, reverse=True)[:max_terms]:
        definition = TRADING_GLOSSARY.get(term)
        if not definition:
            continue
        short_def = definition.split(";")[0].split(",")[0].strip()
        parts.append(f"{term}: {short_def}")
    if not parts:
        return ""
    return "📚 " + " | ".join(parts)


def explain_signal_terms(text: str) -> str:
    """Generate a detailed explanation of terms in signal text for `/explain` command.

    Returns a formatted paragraph explaining all terms found in the signal reasoning.

    Example:
        Input:  "RSI oversold near support, volume spike confirms"
        Output: "RSI (momentum indicator showing oversold at <30) near support
                 (price level where buyers prevent further decline). Volume spike
                 confirms move has backing from actual trading activity."
    """
    terms_found = detect_terms(text)

    if not terms_found:
        return "No technical terms detected in this signal."

    explanations = []
    for term in sorted(terms_found):
        definition = get_definition(term)
        if definition:
            explanations.append(f"**{term}**: {definition}")

    return "\n".join(explanations)


if __name__ == "__main__":
    # Test the module
    test_text = "RSI oversold, EMA above VWAP, volume spike on support"
    print("Original:")
    print(test_text)
    print("\nWith emoji definitions:")
    print(add_emoji_definitions(test_text))
    print("\nDetected terms:")
    print(detect_terms(test_text))
    print("\nDetailed explanation:")
    print(explain_signal_terms(test_text))
