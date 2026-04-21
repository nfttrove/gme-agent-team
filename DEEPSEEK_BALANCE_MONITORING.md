# DeepSeek Balance Monitoring

Added real-time DeepSeek account balance checking to prevent overspend and monitor LLM costs.

## What's New

### 3 New Endpoints

```bash
# Check actual DeepSeek account balance
GET /api/costs/deepseek/balance

# Get overall account health (balance + burn rate + runway)
GET /api/costs/account-health
```

### Example Responses

**Balance:**
```json
{
  "balance_usd": 45.67,
  "currency": "USD",
  "status": "active",
  "last_checked": "2026-04-21T15:30:42.123456"
}
```

**Account Health:**
```json
{
  "deepseek_balance": 45.67,
  "daily_burn": 0.47,
  "days_until_empty": 97.2,
  "daily_budget": 5.0,
  "status": "HEALTHY",
  "alerts": []
}
```

## Alert System

Automatic alerts when:
- Daily spend > 80% of budget → "⚠️ Daily budget at 85%"
- Balance < 1 day of spending → "🔴 CRITICAL: Balance is 1 day of runway"
- Balance < 3 days of spending → "⚠️ LOW: Balance is 3 days of runway"
- Balance < 7 days of spending → "ℹ️ INFO: Balance is 1 week of runway"

## How to Use

### 1. Check Balance Anytime
```bash
curl http://localhost:5000/api/costs/deepseek/balance
```

### 2. Monitor Health
```bash
curl http://localhost:5000/api/costs/account-health
```

### 3. Add to Telegram Bot
```python
def handle_command(text: str):
    if cmd == "/balance":
        # Check DeepSeek balance
        balance_response = requests.get("http://localhost:5000/api/costs/deepseek/balance").json()
        health = requests.get("http://localhost:5000/api/costs/account-health").json()
        
        if balance_response.get("balance_usd"):
            msg = f"""
            <b>💰 DeepSeek Account</b>
            Balance: ${balance_response['balance_usd']:.2f}
            Daily burn: ${health['daily_burn']:.2f}
            Runway: {health['days_until_empty']:.0f} days
            Status: {health['status']}
            """
            if health['alerts']:
                msg += "\n<b>Alerts:</b>\n" + "\n".join(health['alerts'])
        else:
            msg = f"❌ Could not fetch balance: {balance_response.get('error')}"
        
        _send(msg)
```

### 4. Add to Dashboard
```typescript
const [health, setHealth] = useState(null);

useEffect(() => {
  fetch('http://localhost:5000/api/costs/account-health')
    .then(r => r.json())
    .then(setHealth);
}, []);

return (
  <div className="cost-card">
    <h3>Account Health</h3>
    <p>Balance: ${health?.deepseek_balance?.toFixed(2)}</p>
    <p>Daily Burn: ${health?.daily_burn?.toFixed(2)}</p>
    <p>Runway: {health?.days_until_empty?.toFixed(0)} days</p>
    <p>Status: <span className={health?.status}>●</span> {health?.status}</p>
    {health?.alerts?.map(alert => <p key={alert}>{alert}</p>)}
  </div>
);
```

## How It Works

1. **Real-time balance check** — API calls `https://api.deepseek.com/user/balance` with your API key
2. **Burn rate calculation** — Divides daily cost by balance to estimate runway
3. **Automatic alerts** — Triggers warnings at key thresholds (1 day, 3 days, 7 days)
4. **Status levels** — HEALTHY (7+ days), WARNING (3-7 days), CRITICAL (< 1 day)

## Requirements

- `DEEPSEEK_API_KEY` must be set in `.env`
- API calls to `https://api.deepseek.com/user/balance` succeed
- Daily burn rate is being tracked

## Example Workflow

```
Day 1: Balance $100, daily burn $0.50
  └─ Runway: 200 days ✅ HEALTHY

Day 50: Balance $75, daily burn $0.60
  └─ Runway: 125 days ✅ HEALTHY

Day 100: Balance $40, daily burn $1.00
  └─ Runway: 40 days ⚠️ INFO (< 7 days)

Day 130: Balance $5, daily burn $2.00
  └─ Runway: 2.5 days ⚠️ LOW (< 3 days)

Day 132: Balance $1.50, daily burn $2.00
  └─ Runway: 0.75 days 🔴 CRITICAL (< 1 day)
  └─ Action: Top up account or reduce agents
```

## Testing

```bash
# Start API server
cd dashboard && python api_server.py

# In another terminal
curl http://localhost:5000/api/costs/deepseek/balance

# Should return:
# {"balance_usd": 45.67, "currency": "USD", "status": "active", ...}
```

## Future: Automation

Phase 2 will add:
- Auto-alert to Telegram when runway < 3 days
- Auto-pause non-critical agents when balance < 1 day
- Monthly cost report with trends
- Cost optimization suggestions (e.g., "Switch to Gemma for Valerie to save $X/day")
