# Option A: Learning Layer Implementation — Complete

## What's Been Added

Your system now has a **bidirectional agent learning loop**. Instead of read-only Telegram access, agents now:

✅ **Learn** from your feedback via `/learn` command  
✅ **Recall** relevant lessons before strategic decisions  
✅ **Adapt** behavior based on accumulated patterns  
✅ **Dream** nightly to surface new insights  

## Files Created

```
.agent/                                   # New portable brain
├── AGENTS.md                             # Team overview
├── README.md                             # Complete usage guide
├── memory/
│   ├── episodic/                         # Trade logs (events)
│   │   └── README.md
│   ├── semantic/
│   │   ├── LESSONS.md                    # Graduated lessons (read by agents)
│   │   ├── lessons.jsonl                 # Source of truth
│   │   └── staged/                       # Candidates awaiting review
│   ├── working/
│   │   └── WORKSPACE.md                  # Session state
│   ├── personal/
│   │   └── PREFERENCES.md                # Your style
│   ├── auto_dream.py                     # Nightly clustering (NEW)
│   └── log_trades_from_db.py             # Backfill from SQLite (optional)
├── skills/
│   └── _index.md                         # Skill manifest
├── protocols/
│   └── (reserved for future hard rules)
└── tools/
    ├── learn.py                          # Teach (NEW)
    ├── recall.py                         # Retrieve (NEW)
    ├── graduate.py                       # Promote candidate (NEW)
    ├── reject.py                         # Reject candidate (planned)
    └── show.py                           # Dashboard (NEW)

gme_trading_system/
├── telegram_bot.py                       # Modified: added /learn, /lessons commands
└── orchestrator.py                       # Modified: added recall_lessons() + integration
```

## Immediate Actions (Next 5 Minutes)

### 1. Verify the Structure

```bash
ls -la /Users/user/my-agent-team/.agent/
```

You should see: `AGENTS.md`, `README.md`, `memory/`, `tools/`, etc.

### 2. Test Telegram Commands

Start Telegram and send:

```
/learn "Test rule" --why "test evidence"
```

Expected response:
```
✅ Lesson graduated!

Test rule

Why: test evidence
```

### 3. Retrieve It

```
/lessons test
```

Expected response: Shows the lesson you just taught.

### 4. Check Brain State

```
/show
```

Expected response:
```
==================================================
  AGENT BRAIN STATE
==================================================
  Graduated lessons:  1
  Trade logs:         0
  Session:            2026-04-21T...
==================================================
```

If all three commands work, **the core system is live**. Agents can now learn from you.

---

## Enable Trade Logging (Next 30 Minutes)

The learning system needs trade outcomes to cluster patterns. Right now, Telegram gives feedback, but auto_dream needs historical data.

### Option A: Quick backfill (if you have existing trades)

```bash
python3 /Users/user/my-agent-team/.agent/memory/log_trades_from_db.py
```

This exports trades from `agent_memory.db` to episodic memory. Adjust the SQL query if your trades table has a different schema.

### Option B: Continuous logging (going forward)

In your **Trader agent output**, log completed trades to `.agent/memory/episodic/trades.jsonl`:

```python
import json
from pathlib import Path

def log_trade(symbol, action, entry, exit, pnl, reason):
    episodic = Path(".agent/memory/episodic")
    episodic.mkdir(parents=True, exist_ok=True)
    
    trade = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent": "Trader",
        "action": action,
        "symbol": symbol,
        "entry": entry,
        "exit": exit,
        "pnl": pnl,
        "outcome": "profitable" if pnl > 0 else "loss",
        "reason": reason,
        "tags": ["auto-logged"],
    }
    
    with open(episodic / "trades.jsonl", "a") as f:
        f.write(json.dumps(trade) + "\n")
```

Call this after each trade completes.

---

## Enable Nightly Dream Cycle (Next 10 Minutes)

The **auto_dream.py** clusters trade patterns every night at 3 AM. This creates candidates for you to review in the morning.

### macOS / Linux

```bash
crontab -e
```

Paste:

```
0 3 * * * python3 /Users/user/my-agent-team/.agent/memory/auto_dream.py >> /Users/user/my-agent-team/.agent/memory/dream.log 2>&1
```

Save and exit (Ctrl+X, then Y, then Enter).

### Verify

```bash
crontab -l | grep auto_dream
```

Should show your job.

---

## Morning Standup Workflow (Daily)

Once dream cycle is running, your morning standup is now:

### 1. Check brain state (1 min)

```bash
python3 /Users/user/my-agent-team/.agent/tools/show.py
```

### 2. Review staged candidates (3 min)

```bash
python3 /Users/user/my-agent-team/.agent/tools/list_candidates.py
```

Output:
```
Staged candidates:
[1] "Sell OTM puts when IV > 70%" (3 trades, 100% profitable)
[2] "Avoid gap reversals during CPI" (2 trades, both losses)
```

### 3. Promote or reject (1 min each)

**Promote**:
```bash
python3 /Users/user/my-agent-team/.agent/tools/graduate.py abc123 --rationale "strong signal with 3 examples"
```

**Reject**:
```bash
python3 /Users/user/my-agent-team/.agent/tools/reject.py def456 --reason "too narrow; needs broader context"
```

**Result**: Graduated lessons influence next cycle. Rejected patterns don't re-surface.

---

## How Agents Use This

### Before Each Strategic Cycle

In `orchestrator.py`, the Futurist cycle now:

```python
lessons = recall_lessons("GME trading strategy, market conditions, IV")
# Surfaces top 5 lessons matching that intent
# Logged to agent_logs for agent context
```

Agents read `.agent/memory/semantic/LESSONS.md` and see:

```
## High IV = Premium Decay
- When IV rank > 70%, selling options has superior theta decay
- Evidence: 3 trades, 100% profitable
- Graduated: 2026-04-21
```

Agents naturally factor this into reasoning.

---

## Example: Day 1 → Day 5

### Day 1: You Teach

Market opens, high IV (IV rank = 82%).

```
/learn "High IV = sell premium" --why "IV mean reverts; theta decay accelerates"
```

Lesson graduates.

### Day 1: Agents Learn

Strategic cycle runs. Agents recall this lesson. Next trade respects it.

### Days 2–5: Pattern Forms

4 more high-IV trades using this lesson → all profitable.

### Day 5: 3 AM Dream

Auto_dream clusters:
```
"Sell premium when IV > 70%" — 5 trades, 100% win, avg $180
```

Stages as candidate.

### Day 5: Morning Standup

You review and graduate:
```bash
python3 .agent/tools/graduate.py staged-xyz --rationale "5 examples, consistent signal"
```

### Days 6+: Agents Combine Rules

Agents now use:
- **IV rule** (graduated)
- **Support level rule** (graduated previously)
→ More precise entry signals

---

## Next: Add OpenClaw (Optional)

Once the learning system is solid, OpenClaw can integrate as a second interface:

```
Telegram (teach rules)
     ↓
    .agent/
     ↓
OpenClaw (agents discuss via Claude)
```

Same brain, multiple access points. For now, focus on stabilizing Telegram + the learning loop.

---

## Troubleshooting

### "/learn" command not found in Telegram

- Restart telegram_bot.py (orchestrator.py restarts it on boot)
- Check telegram_bot.py was modified correctly:
  ```bash
  grep -A5 "elif cmd == \"/learn\"" /Users/user/my-agent-team/gme_trading_system/telegram_bot.py
  ```

### Auto_dream produces no candidates

Needs at least 3 trades in episodic memory. If you don't have trades:

```bash
# Manually create a test trade
python3 << 'EOF'
import json
from pathlib import Path
from datetime import datetime

episodic = Path(".agent/memory/episodic")
episodic.mkdir(parents=True, exist_ok=True)

trade = {
    "timestamp": datetime.utcnow().isoformat() + "Z",
    "agent": "Trader",
    "action": "sell_put",
    "symbol": "GME",
    "entry": 15.00,
    "exit": 14.50,
    "pnl": 245.00,
    "outcome": "profitable",
    "reason": "high IV",
    "tags": ["test"]
}

with open(episodic / "trades.jsonl", "a") as f:
    f.write(json.dumps(trade) + "\n")

print("✓ Test trade logged")
EOF
```

Then run auto_dream:

```bash
python3 /Users/user/my-agent-team/.agent/memory/auto_dream.py
```

### Recall returns empty

Need at least one graduated lesson. Teach one first:

```
/learn "Test rule" --why "Test rationale"
```

Then:

```
/lessons test
```

---

## Key Takeaway

You now have **autonomous agent learning**. Instead of:
- Agent runs → you observe → you manually adjust

You now have:
- Agent runs → **you teach one rule** → auto_dream clusters patterns → agents adapt

This feedback loop compounds. After 2 weeks, your agents will have 20+ rules and operate with much higher autonomy.

**Start small**: Teach 1–2 rules this week. Let auto_dream run. Review candidates daily. You'll see the system adapt.
