import json
from urllib.error import HTTPError

from unusual_whales import UnusualWhalesClient, summarize_options_overview


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_client_builds_bearer_request(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["accept"] = req.headers["Accept"]
        seen["timeout"] = timeout
        return FakeResponse({"data": [{"call_volume": 10, "put_volume": 3}]})

    monkeypatch.setattr("unusual_whales.urlopen", fake_urlopen)

    client = UnusualWhalesClient(api_key="test-token", timeout=7)
    payload = client.options_volume("gme", limit=2)

    assert payload["ok"] is True
    assert seen["url"].endswith("/api/stock/GME/options-volume?limit=2")
    assert seen["auth"] == "Bearer test-token"
    assert seen["accept"] == "application/json"
    assert seen["timeout"] == 7


def test_client_soft_fails_without_key():
    client = UnusualWhalesClient(api_key=None)

    payload = client.options_volume("GME")

    assert payload["ok"] is False
    assert "UNUSUAL_WHALES_API_KEY" in payload["error"]


def test_client_soft_fails_http_errors(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr("unusual_whales.urlopen", fake_urlopen)

    client = UnusualWhalesClient(api_key="bad-token")
    payload = client.max_pain("GME")

    assert payload["ok"] is False
    assert payload["status"] == 401
    assert payload["path"] == "/api/stock/GME/max-pain"


def test_summarize_options_overview_extracts_known_fields():
    snapshot = {
        "ok": True,
        "options_volume": {"data": [{"call_volume": 100, "put_volume": 50, "call_premium": "1200", "put_premium": "800"}]},
        "max_pain": {"data": [{"expiration": "2026-05-22", "max_pain": "22.5"}]},
        "flow_recent": {"data": [{"option_symbol": "GME260522C00022500", "side": "ask", "premium": "5000"}]},
        "oi_per_strike": {"data": [{"strike": "22.5", "call_oi": "1000", "put_oi": "750"}]},
    }

    summary = summarize_options_overview(snapshot)

    assert "Options volume: call_vol=100, put_vol=50" in summary
    assert "Max pain: expiry=2026-05-22, strike=22.5" in summary
    assert "GME260522C00022500" in summary
    assert "Top OI strikes: 22.5 C_OI=1000 P_OI=750" in summary
