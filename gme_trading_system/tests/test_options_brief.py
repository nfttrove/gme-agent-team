from options_brief import (
    persona_label,
    compute_wow_diff,
    gone_strikes,
    shares_translation,
)


def _c(strike: float, moneyness_pct: float, iv: float, oi: int = 1000, score: float = 50.0):
    return {
        "strike": strike,
        "moneyness_pct": moneyness_pct,
        "iv": iv,
        "open_interest": oi,
        "bid": 0.5,
        "ask": 0.55,
        "volume": 100,
        "score": score,
    }


def test_persona_label_assigns_lottery_for_high_iv_otm():
    peers = [
        _c(strike=23.0, moneyness_pct=0.065, iv=0.52),
        _c(strike=22.0, moneyness_pct=0.02, iv=0.44),
        _c(strike=21.5, moneyness_pct=-0.005, iv=0.42),
    ]
    emoji, label = persona_label(peers[0], peers)
    assert (emoji, label) == ("🎰", "lottery ticket")


def test_persona_label_assigns_sensible_for_near_money_low_iv():
    peers = [
        _c(strike=23.0, moneyness_pct=0.065, iv=0.52),
        _c(strike=22.0, moneyness_pct=0.02, iv=0.44),
        _c(strike=21.5, moneyness_pct=-0.005, iv=0.42),
    ]
    emoji, label = persona_label(peers[2], peers)
    assert (emoji, label) == ("🎯", "sensible")


def test_persona_label_assigns_balanced_for_middle_moneyness():
    peers = [
        _c(strike=23.0, moneyness_pct=0.065, iv=0.52),
        _c(strike=22.0, moneyness_pct=0.02, iv=0.44),
        _c(strike=21.5, moneyness_pct=-0.005, iv=0.42),
    ]
    emoji, label = persona_label(peers[1], peers)
    assert emoji == "⚖️"
    assert label == "balanced"


def test_persona_label_assigns_deep_itm_for_far_below_spot():
    candidate = _c(strike=19.0, moneyness_pct=-0.07, iv=0.40)
    emoji, label = persona_label(candidate, [candidate])
    assert (emoji, label) == ("💎", "deep ITM")


def test_compute_wow_diff_marks_new_and_oi_delta():
    current = [_c(strike=23.0, moneyness_pct=0.065, iv=0.52, oi=4000),
               _c(strike=22.0, moneyness_pct=0.02, iv=0.44, oi=1600)]
    previous = [{"strike": 23.0, "open_interest": 3000},
                {"strike": 21.0, "open_interest": 500}]

    diff = compute_wow_diff(current, previous)

    assert diff[23.0]["is_new"] is False
    assert round(diff[23.0]["oi_delta_pct"], 1) == round((4000 - 3000) / 3000 * 100, 1)
    assert diff[22.0]["is_new"] is True
    assert diff[22.0]["oi_delta_pct"] is None


def test_gone_strikes_returns_strikes_dropped_off():
    current = [_c(strike=23.0, moneyness_pct=0.065, iv=0.52)]
    previous = [{"strike": 23.0}, {"strike": 21.0}, {"strike": 25.0}]
    assert gone_strikes(current, previous) == [21.0, 25.0]


def test_shares_translation_reads_bullish_lottery_when_crowd_reaches():
    candidates = [
        _c(strike=23.0, moneyness_pct=0.065, iv=0.55),
        _c(strike=24.0, moneyness_pct=0.11, iv=0.60),
        _c(strike=22.5, moneyness_pct=0.04, iv=0.52),
    ]
    msg = shares_translation(candidates)
    assert "lottery" in msg.lower()
    assert "premium" in msg.lower()


def test_shares_translation_reads_balanced_with_no_lottery_count():
    candidates = [
        _c(strike=21.5, moneyness_pct=-0.005, iv=0.42),
        _c(strike=22.0, moneyness_pct=0.02, iv=0.44),
    ]
    msg = shares_translation(candidates)
    assert "balanced" in msg.lower()


def test_shares_translation_returns_empty_for_no_candidates():
    assert shares_translation([]) == ""
