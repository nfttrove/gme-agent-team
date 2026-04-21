# Agent Learning Brain — Setup & Usage

## What This Is

A **bidirectional learning system** that makes your CrewAI trading agents autonomous and adaptive. Instead of just executing trades, they now:
1. **Learn** from your Telegram feedback
2. **Recall** relevant lessons before each cycle
3. **Adapt** behavior based on accumulated patterns
4. **Dream** nightly to surface new insights

## Quick Start

### 1. Teach Your First Lesson (via Telegram)

```
/learn "High IV = Premium decay advantage" --why "IV rank > 70%, 100% win rate on 5 trades"
```

The lesson is immediately graduated and will influence agent decisions.

### 2. View What Agents Learned

```
/lessons GME strategy
```

Returns all lessons matching that topic.

### 3. Dashboard

```
/show
```

or

```
python3 .agent/tools/show.py
```

Shows brain state: lessons, trades logged, session info.

## How It Works

### Learning Loop (Daily)

```
┌─────────────────────────────────────────────────────┐
│ 1. TRADING HAPPENS                                  │
│    ├─ Agents run cycles (strategic, daily, etc.)   │
│    └─ Trade outcomes logged to episodic memory     │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│ 2. YOU TEACH (Via Telegram)                         │
│    ├─ /learn "rule" --why "evidence"               │
│    └─ Lesson graduates immediately                 │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│ 3. AGENTS RECALL (Before Next Cycle)                │
│    ├─ Orchestrator calls recall.py                 │
│    ├─ Surfaces 5 most relevant lessons             │
│    └─ Lessons inject into agent context            │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│ 4. NIGHTLY DREAM (3 AM ET — Cron)                   │
│    ├─ auto_dream.py clusters trade patterns        │
│    ├─ "Sold puts 5 times = all profitable"         │
│    └─ Stages candidates for your review            │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│ 5. YOU GRADUATE (Morning Standup)                   │
│    ├─ python3 .agent/tools/list_candidates.py      │
│    ├─ Review staged patterns                       │
│    └─ graduate.py <id> --rationale "evidence..."   │
└─────────────────────────────────────────────────────┘
```

## Memory Layers

### Episodic (Raw events)
- Every trade logged: symbol, entry, exit, outcome, reason
- Path: `.agent/memory/episodic/trades.jsonl`
- **Never deleted** — source of truth for clustering

### Semantic (Graduated lessons)
- Rules learned from patterns
- Path: `.agent/memory/semantic/lessons.jsonl` and `LESSONS.md`
- **Read on every cycle** — agents load these automatically

### Working (Session state)
- Current session context
- Path: `.agent/memory/working/WORKSPACE.md`
- Updated as agents run; cleared between sessions

### Personal (User preferences)
- Your trading style, risk appetite, explanation style
- Path: `.agent/memory/personal/PREFERENCES.md`
- Agents read this at startup

## Commands

### Teaching

```bash
/learn "Always check bid-ask before market order" --why "5% slippage lost $300"
```
→ Lesson graduates immediately, available next cycle

### Recalling

```bash
/lessons high IV strategy
/lessons reversal patterns
/lessons
```
→ Surface top 5 lessons matching topic

### Reviewing (Terminal)

```bash
python3 .agent/tools/list_candidates.py
```
→ Show staged candidates from last dream cycle

```bash
python3 .agent/tools/graduate.py abc123def456 --rationale "3 profitable examples, matches risk profile"
```
→ Promote candidate to graduated lesson

```bash
python3 .agent/tools/reject.py abc123def456 --reason "too specific to GME; doesn't generalize"
```
→ Reject and preserve decision history (prevents churn)

## Setup: Nightly Dream Cycle

The `auto_dream.py` clusters your trade patterns nightly. To run it automatically:

### macOS / Linux

```bash
crontab -e
```

Add this line:

```
0 3 * * * python3 /Users/user/my-agent-team/.agent/memory/auto_dream.py >> /Users/user/my-agent-team/.agent/memory/dream.log 2>&1
```

This runs at 3 AM ET every day. Adjust the path to your project.

### Windows

Use Task Scheduler:
1. Create task → "New Task"
2. Action: `python3 C:\path\to\.agent\memory\auto_dream.py`
3. Trigger: Daily at 3 AM
4. Output redirect: `C:\path\to\.agent\memory\dream.log`

### Verify

Check the dream log:

```bash
tail -20 .agent/memory/dream.log
```

## Architecture

```
.agent/
├── AGENTS.md                       # Team map
├── memory/
│   ├── episodic/                   # Trade logs (source of truth)
│   │   ├── trades.jsonl            # Executed trades
│   │   ├── errors.jsonl            # Failures
│   │   └── decisions.jsonl         # Agent decisions
│   ├── semantic/                   # Learned rules
│   │   ├── lessons.jsonl           # Source of truth (graduated lessons)
│   │   ├── LESSONS.md              # Rendered lessons (for you + agents)
│   │   └── staged/                 # Candidates from dream cycle
│   ├── working/
│   │   └── WORKSPACE.md            # Current session state
│   ├── personal/
│   │   └── PREFERENCES.md          # Your style + constraints
│   ├── auto_dream.py               # Nightly clustering
│   └── memory_search.py            # [BETA] FTS search
├── skills/
│   ├── _index.md                   # Manifest of available skills
│   ├── short-vol-seller/
│   ├── gap-reversal/
│   └── ...
├── protocols/
│   ├── permissions.md              # Hard rules (enforced by orchestrator)
│   └── tool_schemas.json           # Tool definitions
└── tools/                          # Host-agent CLI
    ├── learn.py                    # Teach a lesson (stage + graduate)
    ├── recall.py                   # Surface lessons for intent
    ├── graduate.py                 # Promote staged candidate
    ├── reject.py                   # Reject with history
    ├── show.py                     # Brain state dashboard
    └── list_candidates.py          # Review staged patterns
```

## Example Workflows

### Workflow 1: Teach a Winning Pattern (Immediate)

**Scenario**: You notice your agents are selling puts with high IV successfully.

```
/learn "Sell OTM puts when IV rank > 70%" --why "3 trades, 100% win, avg theta $245"
```

**Result**: Lesson graduates. Next strategic cycle, agents recall this and prioritize high-IV setups.

---

### Workflow 2: Capture a Failed Pattern (Via Nightly Dream)

**Scenario**: Auto_dream.py clusters 4 failed gap reversals in one week.

**Morning**: You run `list_candidates.py` and see:
```
[staged-xyz123] "Avoid gap reversals during Fed events" (4 losses, -$1200)
```

**Decision**: Reject because Fed dates are an external factor, not a trading rule.

```bash
python3 .agent/tools/reject.py staged-xyz123 --reason "pattern is conditional on calendar; need Fed check first"
```

**Result**: Decision logged. Auto_dream won't re-stage this pattern for 30 days.

---

### Workflow 3: Build on Success (Cascading)

**Day 1**: You teach a lesson about IV management.

**Day 5**: Auto_dream clusters 8 trades using that lesson → 87% win rate.

**Morning**: New candidate: "High IV + support level = 90%+ win rate"

**You graduate it**:

```bash
python3 .agent/tools/graduate.py candidate-abc --rationale "builds on prior IV lesson; strong signal"
```

**Day 6+**: Agents now use BOTH lessons together → more precise signals.

---

## How Agents Use Lessons

### Before Each Strategic Cycle

In `orchestrator.py`, before agents run:

```python
lessons = recall_lessons("GME trading strategy, market conditions, IV")
# Lessons logged to agent_logs for context
```

Agents read `LESSONS.md` and see:
```
## High IV Strategy (Graduated)
- When IV rank > 70%, selling OTM puts has better theta decay
- Evidence: 3 trades, 100% profitable, avg $245
- Why: Implied vol always reverts lower
```

Agents naturally incorporate this into their reasoning.

### Result

- **Without lessons**: "I should sell a put"
- **With lessons**: "IV rank is 78%, which matches our high-IV strategy. I should sell a put."

The agents become context-aware and adaptive.

---

## Troubleshooting

### Auto_dream isn't running

```bash
# Check cron job
crontab -l | grep auto_dream

# Run manually to test
python3 .agent/memory/auto_dream.py

# Check permissions
ls -la .agent/memory/auto_dream.py
chmod +x .agent/memory/auto_dream.py
```

### Recall returns nothing

Make sure you've taught lessons first:

```bash
/learn "test rule" --why "test rationale"
```

Then:

```bash
/lessons test
```

### Staged candidates aren't appearing

1. Check episodic trades were logged:

```bash
ls -la .agent/memory/episodic/
wc -l .agent/memory/episodic/trades.jsonl
```

2. Run auto_dream manually and check output:

```bash
python3 .agent/memory/auto_dream.py
```

---

## Next: OpenClaw Integration (Optional)

This `.agent/` brain is portable across harnesses. To add OpenClaw as an interface alongside Telegram:

1. Copy `.agent/` config to OpenClaw settings
2. OpenClaw agents can run `recall.py` before decisions
3. Same learning loop, multiple access points

For now, Telegram is your teaching interface. You can add OpenClaw later without changing the memory structure.

---

## Tips

- **Teach incrementally**: One rule at a time. Evidence compounds.
- **Reject decisively**: If a candidate is spurious, reject it with rationale so auto_dream learns not to re-stage similar patterns.
- **Trust auto_dream**: It's mechanical—no reasoning, no errors. You do the judgment.
- **Review nightly**: Morning standup = review candidates + graduate. 5 minutes, high impact.
- **Season your preferences**: Update `PREFERENCES.md` if your risk appetite changes.
