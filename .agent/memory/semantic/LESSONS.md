# Graduated Lessons

Rules learned from experience. Automatically loaded by agents before each cycle.

---

## Market Conditions
**High IV = Premium Decay Advantage**
- When IV rank > 70%, selling OTM options has better theta decay
- Evidence: 5 trades, 100% profitable
- Why: Implied vol always reverts lower; extrinsic value collapses
- Graduated: 2026-04-21 by user

**Gap Down Days = Reversal Setup**
- GME gaps down > 5% → reversal within 2 hours, 70% of time
- Evidence: 8/10 observed; avg +2.5% intraday recovery
- Why: Retail panic sells; MM stabilizes; squeeze potential
- Graduated: 2026-04-21 by user

## Risk Rules
**No More Than 1 Trade Per Hour**
- Avoid overtrading on signal chasing
- Evidence: Trades spaced >60min have 2x win rate vs. clustered
- Why: Emotional fatigue, whipsaw losses
- Graduated: 2026-04-21 by user

## Execution
**Always Check Bid-Ask Spread Before Market Order**
- Reject if spread > 5% of premium
- Evidence: Spread slippage ate 15% of 3 trades
- Why: GME options illiquid; limit orders better
- Graduated: 2026-04-21 by user

---

## Pending Candidates
(Staged from `auto_dream.py` — awaiting your review)
(View with: `python3 .agent/tools/list_candidates.py`)
