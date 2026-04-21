# Skills Manifest

Progressive disclosure: lightweight manifest always loads; full `SKILL.md` files only load when triggers match.

## Trading Skills

### short-vol-seller
**Triggers**: "sell option", "IV > 70", "theta decay"
**Purpose**: Execute short vol strategy with risk checks
**Status**: active

### gap-reversal
**Triggers**: "gap down", "reversal", "intraday bounce"
**Purpose**: Identify and trade gap reversals
**Status**: active

### execution-validator
**Triggers**: "before market order", "bid-ask", "liquidity"
**Purpose**: Validate order quality before submission
**Status**: active

### memory-logger
**Triggers**: "trade complete", "error", "decision made"
**Purpose**: Log outcomes to episodic memory
**Status**: active

---

Use: `python3 .agent/tools/skill_loader.py <skill_name>` to load full details.
