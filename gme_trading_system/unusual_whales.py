"""Optional Unusual Whales API client for options-flow context.

This module intentionally uses Unusual Whales' official API rather than
scraping the rendered web app. The public stock overview page points agents at
https://api.unusualwhales.com/docs and the API requires a bearer token.

All methods soft-fail: no token or upstream errors return structured metadata
so scheduled jobs can continue using yfinance-derived options data.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

API_KEY_ENV = "UNUSUAL_WHALES_API_KEY"
DEFAULT_BASE_URL = "https://api.unusualwhales.com"
SYMBOL = "GME"


def _compact_json(value: Any, max_chars: int = 4000) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _data(payload: Any) -> Any:
    """Return the conventional API payload data if present."""
    if isinstance(payload, dict):
        return payload.get("data", payload)
    return payload


def _first_record(payload: Any) -> dict[str, Any]:
    data = _data(payload)
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, list):
            return nested[0] if nested and isinstance(nested[0], dict) else {}
        return data
    return {}


def _pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _top_records(payload: Any, n: int, sort_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    data = _data(payload)
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        return []

    rows = [row for row in data if isinstance(row, dict)]

    def score(row: dict[str, Any]) -> float:
        vals = [_to_float(row.get(key)) for key in sort_keys]
        return max([v for v in vals if v is not None] or [0.0])

    return sorted(rows, key=score, reverse=True)[:n]


@dataclass
class UnusualWhalesClient:
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 15

    @classmethod
    def from_env(cls) -> "UnusualWhalesClient":
        return cls(api_key=os.getenv(API_KEY_ENV, "").strip() or None)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a JSON endpoint, returning a structured error on soft failure."""
        if not self.enabled:
            return {"ok": False, "error": f"missing {API_KEY_ENV}"}

        clean_path = path if path.startswith("/") else f"/{path}"
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
        url = f"{self.base_url}{clean_path}{'?' + query if query else ''}"
        req = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "gme-agent-team/1.0",
            },
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:  # nosec B310 - fixed HTTPS API base URL by default
                body = resp.read().decode("utf-8")
                payload = json.loads(body) if body else {}
                if isinstance(payload, dict):
                    payload.setdefault("ok", True)
                    return payload
                return {"ok": True, "data": payload}
        except HTTPError as e:
            log.warning("[unusual_whales] HTTP %s for %s", e.code, clean_path)
            return {"ok": False, "status": e.code, "error": e.reason, "path": clean_path}
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("[unusual_whales] request failed for %s: %s", clean_path, e)
            return {"ok": False, "error": str(e), "path": clean_path}

    # Official stock/options endpoints used by the GME options analyst.
    def options_volume(self, ticker: str = SYMBOL, limit: int = 5) -> dict[str, Any]:
        return self.get(f"/api/stock/{ticker.upper()}/options-volume", {"limit": limit})

    def max_pain(self, ticker: str = SYMBOL) -> dict[str, Any]:
        return self.get(f"/api/stock/{ticker.upper()}/max-pain")

    def flow_recent(self, ticker: str = SYMBOL, side: str | None = None, min_premium: int | None = None) -> dict[str, Any]:
        return self.get(f"/api/stock/{ticker.upper()}/flow-recent", {"side": side, "min_premium": min_premium})

    def flow_per_strike(self, ticker: str = SYMBOL) -> dict[str, Any]:
        return self.get(f"/api/stock/{ticker.upper()}/flow-per-strike")

    def oi_per_strike(self, ticker: str = SYMBOL) -> dict[str, Any]:
        return self.get(f"/api/stock/{ticker.upper()}/oi-per-strike")

    def net_prem_ticks(self, ticker: str = SYMBOL) -> dict[str, Any]:
        return self.get(f"/api/stock/{ticker.upper()}/net-prem-ticks")

    def spot_exposures_by_strike(self, ticker: str = SYMBOL) -> dict[str, Any]:
        return self.get(f"/api/stock/{ticker.upper()}/spot-exposures/strike")

    def options_overview(self, ticker: str = SYMBOL, limit: int = 5) -> dict[str, Any]:
        """Small snapshot of the Unusual Whales option-flow context we care about."""
        if not self.enabled:
            return {"ok": False, "source": "unusual_whales", "error": f"missing {API_KEY_ENV}"}

        overview = {
            "ok": True,
            "source": "unusual_whales",
            "ticker": ticker.upper(),
            "options_volume": self.options_volume(ticker, limit=limit),
            "max_pain": self.max_pain(ticker),
            "flow_recent": self.flow_recent(ticker),
            "flow_per_strike": self.flow_per_strike(ticker),
            "oi_per_strike": self.oi_per_strike(ticker),
            "net_prem_ticks": self.net_prem_ticks(ticker),
            "spot_exposures_by_strike": self.spot_exposures_by_strike(ticker),
        }
        overview["summary"] = summarize_options_overview(overview, limit=limit)
        return overview


def summarize_options_overview(snapshot: dict[str, Any], limit: int = 5) -> str:
    """Human-readable agent_log summary from heterogeneous UW payloads."""
    if not snapshot.get("ok"):
        return f"Unusual Whales unavailable: {snapshot.get('error', 'unknown error')}"

    lines = ["Unusual Whales options-flow snapshot:"]

    volume = _first_record(snapshot.get("options_volume"))
    if volume:
        lines.append(
            "Options volume: "
            f"call_vol={_pick(volume, 'call_volume', 'call_vol', 'calls_volume', 'net_call_volume') or 'n/a'}, "
            f"put_vol={_pick(volume, 'put_volume', 'put_vol', 'puts_volume', 'net_put_volume') or 'n/a'}, "
            f"call_prem={_pick(volume, 'call_premium', 'net_call_premium', 'call_prem') or 'n/a'}, "
            f"put_prem={_pick(volume, 'put_premium', 'net_put_premium', 'put_prem') or 'n/a'}"
        )

    max_pain = _first_record(snapshot.get("max_pain"))
    if max_pain:
        lines.append(
            "Max pain: "
            f"expiry={_pick(max_pain, 'expiry', 'expiration', 'expiry_date', 'date') or 'n/a'}, "
            f"strike={_pick(max_pain, 'max_pain', 'max_pain_strike', 'strike') or 'n/a'}"
        )

    flow = _top_records(snapshot.get("flow_recent"), limit, ("premium", "total_premium", "volume"))
    if flow:
        chunks = []
        for row in flow[:limit]:
            chunks.append(
                f"{_pick(row, 'option_symbol', 'symbol', 'contract_symbol') or 'contract'} "
                f"{_pick(row, 'side', 'put_call', 'option_type') or ''} "
                f"prem={_pick(row, 'premium', 'total_premium') or 'n/a'}"
            )
        lines.append("Recent flow: " + "; ".join(chunks))

    oi = _top_records(snapshot.get("oi_per_strike"), limit, ("call_oi", "put_oi", "open_interest", "total_oi"))
    if oi:
        lines.append(
            "Top OI strikes: "
            + "; ".join(
                f"{_pick(row, 'strike') or 'n/a'} C_OI={_pick(row, 'call_oi', 'call_open_interest') or 'n/a'} "
                f"P_OI={_pick(row, 'put_oi', 'put_open_interest') or 'n/a'}"
                for row in oi[:limit]
            )
        )

    # Keep the raw payload available in the log for schema drift debugging, but
    # bounded so Telegram/Synthesis context does not get flooded.
    if len(lines) == 1:
        lines.append("No recognized summary fields; raw=" + _compact_json(snapshot, max_chars=1200))

    return "\n".join(lines)
