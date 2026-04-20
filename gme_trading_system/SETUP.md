# GME Trading System — Complete Setup Guide

## Quick Status: What's Working vs What Needs Setup

| Component | Status | Action needed |
|---|---|---|
| DeepSeek V3 (primary LLM) | ✅ Key in .env | — |
| Gemini (fallback LLM) | ✅ Key in .env | — |
| SQLite database | ✅ Auto-created | — |
| Streamlit dashboard | ✅ Ready | `streamlit run dashboard.py` |
| TradingView 1-sec webhook | ⚠️ Infrastructure ready | Start ngrok (step 2) |
| Alpaca 1-sec backup feed | ⚠️ Ready, keys missing | Add keys to .env (step 3) |
| News sentiment (Newsie) | ⚠️ No API key | Add Finnhub key (step 4) |
| Telegram notifications | ⚠️ Not configured | Set up bot (step 5) |
| Twitter/X social monitor | ⚠️ Optional | Add X Bearer Token (step 6) |
| EDGAR CTO scan | ✅ Free, no key needed | Add email to SEC_USER_AGENT |

---

## Step 1 — Start the Dashboard (works now, no keys needed)

```bash
cd /Users/user/my-agent-team/gme_trading_system
source venv/bin/activate
streamlit run dashboard.py
```

Opens at http://localhost:8501

---

## Step 2 — TradingView 1-Second Webhook (your paid account)

This is your primary data source. Infrastructure is ready — you just need to connect it.

**A. Start the logger daemon:**
```bash
source venv/bin/activate
python logger_daemon.py
# Starts Flask server on port 8765
```

**B. Expose it publicly with ngrok:**
```bash
# Install ngrok: https://ngrok.com/download
ngrok http 8765
# Copy the https URL e.g. https://abc123.ngrok-free.app
```

**C. Configure TradingView alert:**
1. Open your GME 1-second chart on TradingView
2. Click the alarm clock icon → Create Alert
3. Set condition to "Any alert() function call" or price-based
4. In **Webhook URL**: `https://abc123.ngrok-free.app/tick`
5. In **Message** (paste exactly):
```json
{
  "symbol": "GME",
  "time": "{{time}}",
  "open": {{open}},
  "high": {{high}},
  "low": {{low}},
  "close": {{close}},
  "volume": {{volume}}
}
```
6. Set to trigger on "Bar Close"
7. Enable "Send webhook notification"

**Test it:**
```bash
curl -X POST http://localhost:8765/tick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"GME","time":"2026-04-20T14:30:00","open":22.10,"high":22.15,"low":22.05,"close":22.12,"volume":1500}'
# Should return {"status": "ok", "close": 22.12}
```

---

## Step 3 — Alpaca 1-Second Backup Feed (free paper account)

Fills gaps when TradingView webhook misses a tick. Zero cost.

1. Sign up at https://alpaca.markets (paper account, no credit card)
2. Dashboard → Paper → API Keys → Generate new key
3. Add to `.env`:
```
ALPACA_API_KEY=PKxxxxxxxxxx
ALPACA_API_SECRET=xxxxxxxxxxxxxxxxxx
ALPACA_FEED=iex
```
4. Restart logger_daemon.py — Alpaca connects automatically in background

---

## Step 4 — News API (Newsie agent)

Newsie runs every 30 minutes but is blind without a key.

**Option A — Finnhub (recommended, free tier = 60 calls/min):**
1. Sign up at https://finnhub.io/register
2. Copy your API key
3. Add to `.env`: `FINNHUB_API_KEY=your_key`

**Option B — NewsAPI (free tier = 100 calls/day):**
1. Sign up at https://newsapi.org/register
2. Add to `.env`: `NEWSAPI_KEY=your_key`

---

## Step 5 — Telegram Notifications (push alerts to your phone)

Get alerts for: trade approvals, GME immunity changes, PE signals, Ryan Cohen posts, daily P&L.

**A. Create your Telegram bot (5 minutes):**
1. Open Telegram → search `@BotFather`
2. Send: `/newbot`
3. Name it e.g. `GME Alert Bot`
4. Copy the token it gives you

**B. Get your Chat ID:**
1. Send any message to your new bot
2. Visit in browser: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat":{"id": 123456789}` — that number is your chat ID

**C. Add to `.env`:**
```
TELEGRAM_BOT_TOKEN=7123456789:AAxxxxxx
TELEGRAM_CHAT_ID=123456789
```

**D. Test it:**
```bash
source venv/bin/activate
python notifier.py
# Should send a welcome message to your Telegram
```

---

## Step 6 — Twitter/X Social Monitor (optional but valuable)

Monitors @ryancohen, @larryvc, @michaeljburry, @TheRoaringKitty in real-time.

**Free tier (500k reads/month — more than enough):**
1. Go to https://developer.x.com/en/portal/dashboard
2. Sign in with your X account → Create project → Create App
3. Under "Keys and Tokens" → copy **Bearer Token**
4. Add to `.env`:
```
X_BEARER_TOKEN=AAAAAAAAAAAAAAAAAAAAAxxxx
```

**Note:** If you'd rather not set this up, the system falls back to Nitter (public scraper).
Nitter is free but can be unreliable. X API is recommended.

---

## Step 7 — EDGAR Scanner (already works, just add your email)

SEC requires a User-Agent header with your contact info. Free, no registration.

Add to `.env`:
```
SEC_USER_AGENT=YourName youremail@example.com
```

---

## Step 8 — Run the Full System

**Terminal 1 — Data logger (run 24/7):**
```bash
cd gme_trading_system && source venv/bin/activate
python logger_daemon.py
```

**Terminal 2 — Orchestrator (run during market hours + evening):**
```bash
cd gme_trading_system && source venv/bin/activate
python orchestrator.py
```

**Terminal 3 — Dashboard:**
```bash
cd gme_trading_system && source venv/bin/activate
streamlit run dashboard.py
```

**Terminal 4 — ngrok (keep running while logger_daemon is running):**
```bash
ngrok http 8765
```

---

## Step 9 — Production Deployment (Railway)

For 24/7 operation without keeping your Mac on:

1. Push code to a private GitHub repo
2. Sign up at https://railway.app
3. New project → Deploy from GitHub → select repo
4. Add environment variables (copy from .env)
5. Railway runs all processes via Procfile:

Create `Procfile` in gme_trading_system/:
```
logger: python logger_daemon.py
orchestrator: python orchestrator.py
dashboard: streamlit run dashboard.py --server.port $PORT --server.address 0.0.0.0
```

Note: Railway gives you a public HTTPS URL — use that instead of ngrok for the TradingView webhook.

---

## Complete .env Template

```bash
# === LLMs ===
DEEPSEEK_API_KEY=sk-...           # Already set ✅
GOOGLE_API_KEY=AIza...            # Already set ✅

# === Data Feeds ===
FINNHUB_API_KEY=                  # Step 4 — Newsie
NEWSAPI_KEY=                      # Step 4 — alternative to Finnhub

ALPACA_API_KEY=                   # Step 3 — 1-sec backup feed
ALPACA_API_SECRET=                # Step 3
ALPACA_FEED=iex                   # iex (free) or sip (paid)

# === Notifications ===
TELEGRAM_BOT_TOKEN=               # Step 5 — push alerts
TELEGRAM_CHAT_ID=                 # Step 5 — your chat ID

# === Social ===
X_BEARER_TOKEN=                   # Step 6 — Twitter/X monitor

# === SEC EDGAR ===
SEC_USER_AGENT=YourName your@email.com   # Step 7

# === Infrastructure ===
LOGGER_PORT=8765
WEBHOOK_STALE_ALERT_S=300         # Alert if no webhook for 5 min

# === Exchange (paper mode only until ready for live) ===
BITGET_API_KEY=paper
BITGET_API_SECRET=paper
BITGET_PASSPHRASE=paper
```

---

## What You Get

### Daily Schedule (ET)
| Time | What happens |
|---|---|
| 24/7 | TradingView 1-sec + Alpaca backup → DB |
| 24/7 | Valerie validates data every 1 min |
| 24/7 | Chatty comments on price action every 30 sec |
| 9:00 AM | Boss daily huddle — mission briefing |
| 9:05 AM | CTO structural brief — GME immunity + short watchlist |
| Every 30 min | Newsie fetches and scores GME news |
| Every 15 min | Social monitor scans @ryancohen, @larryvc, @michaeljburry |
| Every 2 hrs | Futurist → Boss → Trader Joe cycle (gate-checked) |
| Monday 8:30 AM | Options chain + max pain for the week |
| 4:30 PM | Daily debrief — score predictions vs actuals |
| 4:35 PM | Daily aggregator — build candle from ticks |
| 8:00 PM | Trendy — daily trend analysis |
| Friday 5:00 PM | Weekly strategy review — Boss may adapt thresholds |
| Sunday 8:00 AM | CTO EDGAR scan — full PE playbook watchlist |

### Telegram Alerts You'll Receive
- Trade approved/rejected by Boss
- GME immunity condition change (debt, board, CRO)
- PE playbook signal on any watchlist stock (≥75% confidence)
- @ryancohen or @larryvc posts anything
- @michaeljburry posts something GME/market-relevant
- TradingView webhook silent >5 minutes
- Weekly max pain update (Mondays)
- Daily P&L summary (4:30 PM)
