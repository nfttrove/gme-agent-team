import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("yfinance")

from options_feed import OptionsFeed


class StubOptionsFeed(OptionsFeed):
    def __init__(self, calls: pd.DataFrame, puts: pd.DataFrame | None = None, price: float = 20.0):
        self._calls = calls
        self._puts = puts if puts is not None else pd.DataFrame({"strike": [], "OI": []})
        self._price = price

    def get_chain(self, expiration: str | None = None) -> dict:
        return {
            "calls": self._calls.copy(),
            "puts": self._puts.copy(),
            "expiration": expiration or "2026-05-22",
        }

    def _current_price(self) -> float:
        return self._price


def test_call_contract_candidates_rank_liquid_near_money_calls():
    calls = pd.DataFrame(
        [
            {
                "contractSymbol": "GME260522C00021000",
                "strike": 21.0,
                "last": 0.95,
                "bid": 0.90,
                "ask": 1.00,
                "volume": 500,
                "OI": 1000,
                "IV": 0.80,
            },
            {
                "contractSymbol": "GME260522C00022000",
                "strike": 22.0,
                "last": 0.65,
                "bid": 0.60,
                "ask": 0.70,
                "volume": 300,
                "OI": 800,
                "IV": 0.75,
            },
            {
                # Too far OTM for the default +20% moneyness window.
                "contractSymbol": "GME260522C00025000",
                "strike": 25.0,
                "last": 0.30,
                "bid": 0.20,
                "ask": 0.40,
                "volume": 1000,
                "OI": 1500,
                "IV": 1.20,
            },
            {
                # Wide spread, excluded by max_spread_pct.
                "contractSymbol": "GME260522C00019000",
                "strike": 19.0,
                "last": 2.50,
                "bid": 2.00,
                "ask": 3.00,
                "volume": 800,
                "OI": 1200,
                "IV": 0.70,
            },
        ]
    )
    feed = StubOptionsFeed(calls, price=20.0)

    result = feed.call_contract_candidates(n=3)

    assert result["analysis"] == "watchlist_only_not_trade_recommendation"
    assert result["expiration"] == "2026-05-22"
    assert [c["contract_symbol"] for c in result["candidates"]] == [
        "GME260522C00021000",
        "GME260522C00022000",
    ]
    top = result["candidates"][0]
    assert top["breakeven_mid"] == 21.95
    assert top["breakeven_ask"] == 22.0
    assert top["open_interest"] == 1000
    assert top["reason"].endswith("not an execution recommendation")


def test_call_contract_candidates_returns_empty_watchlist_when_filters_reject_all():
    calls = pd.DataFrame(
        [
            {
                "contractSymbol": "GME260522C00030000",
                "strike": 30.0,
                "last": 0.10,
                "bid": 0.00,
                "ask": 0.20,
                "volume": 0,
                "OI": 1,
                "IV": 1.50,
            }
        ]
    )
    feed = StubOptionsFeed(calls, price=20.0)

    result = feed.call_contract_candidates()

    assert result["current_price"] == 20.0
    assert result["candidates"] == []
