# Episodic Memory

Raw trade logs and outcomes. Source for pattern clustering.

- `trades.jsonl` — each trade executed
- `errors.jsonl` — failures and mistakes
- `decisions.jsonl` — agent decisions (even if not executed)

Format:
```json
{
  "timestamp": "2026-04-21T14:30:00-05:00",
  "agent": "Strategist",
  "action": "sell_put",
  "symbol": "GME",
  "strike": 15.50,
  "iv_rank": 0.72,
  "outcome": "profitable",
  "pnl": 245.00,
  "reason": "high IV = theta decay",
  "tags": ["short-vol", "gme"]
}
```
