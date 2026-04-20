"""
PE Playbook Pattern Library
────────────────────────────
Source intelligence: "Powered By The Players — Connect The Dots"

Encodes the 12 documented PE destruction patterns as scoreable signals.
Each signal has:
  - confidence: how reliable this signal is historically
  - timeline: how many months before the event this signal typically fires
  - action: what the trading system should do when this fires

Used by: cto_agent, sec_scanner, orchestrator
"""
from dataclasses import dataclass, field


@dataclass
class PlaybookSignal:
    name: str
    confidence: float        # 0.0–1.0 (book-documented reliability)
    timeline_months: int     # median months before bankruptcy/squeeze
    action: str              # SHORT, SQUEEZE_WATCH, EXIT, MONITOR
    description: str
    indicators: list[str]    # what SEC filings/data to check
    notes: str = ""


# ── The 12 PE Destruction Signals ─────────────────────────────────────────────

PLAYBOOK_SIGNALS: list[PlaybookSignal] = [

    PlaybookSignal(
        name="restructuring_advisor_hired",
        confidence=0.99,
        timeline_months=3,
        action="SHORT",
        description="AlixPartners, Alvarez & Marsal, or Lazard hired as CRO",
        indicators=["8-K filing: 'Chief Restructuring Officer'", "8-K: 'AlixPartners'", "8-K: 'Alvarez & Marsal'"],
        notes=(
            "Holly Etlin track record: Borders (liquidated), RadioShack (liquidated), BBBY (liquidated). "
            "These firms do not do turnarounds. Shareholders get $0. "
            "Exit immediately on 8-K announcement. Do NOT wait."
        ),
    ),

    PlaybookSignal(
        name="pe_executive_rotation",
        confidence=0.90,
        timeline_months=18,
        action="SHORT",
        description="New CEO or board member has track record across 3+ PE portfolio companies",
        indicators=["DEF 14A proxy: board biography", "8-K: new CEO appointment", "LinkedIn career history"],
        notes=(
            "Sue Gove pattern: Zales(Apollo)→Vitamin World(Apollo)→Fresh Market(Apollo)→"
            "Tailored Brands(Apollo)→Truck Hero(Apollo/Ares)→BBBY CEO. "
            "Each rotation executes the same playbook. 12–24 months to bankruptcy."
        ),
    ),

    PlaybookSignal(
        name="pe_board_infiltration",
        confidence=0.85,
        timeline_months=24,
        action="SHORT",
        description="3+ board members with Apollo/KKR/Blackstone/Bain/TPG/Carlyle/Silver Lake/Ares history",
        indicators=["DEF 14A proxy filing: director biographies"],
        notes=(
            "Bill Simon (KKR) on GameStop board was the plant. Cohen purged him. "
            "Count PE connections per director. 0–2 = normal. 3+ = target. "
            "Board composition is the clearest leading indicator."
        ),
    ),

    PlaybookSignal(
        name="sale_leaseback_announced",
        confidence=0.80,
        timeline_months=30,
        action="SHORT",
        description="Company sells real estate assets and leases them back",
        indicators=["8-K: 'sale-leaseback'", "10-K: operating lease obligations spike", "10-Q: asset disposals"],
        notes=(
            "Sears: Lampert's hedge fund bought Sears stores, Sears leased at escalating rates. "
            "Creates permanent cash drain. Company is funding its own short seller. "
            "2–3 year deterioration timeline."
        ),
    ),

    PlaybookSignal(
        name="debt_maturity_clustering",
        confidence=0.80,
        timeline_months=18,
        action="SHORT",
        description="$500M+ in debt matures within 18 months, company can't refinance at operating rates",
        indicators=["10-K: long-term debt maturity schedule", "current debt-to-revenue ratio"],
        notes=(
            "When debt maturities cluster, covenant violations become inevitable. "
            "Company can't refinance without admitting distress (rates go up). "
            "Forced restructuring gives creditors control. Equity = $0."
        ),
    ),

    PlaybookSignal(
        name="activist_investor_concealed_short",
        confidence=0.75,
        timeline_months=6,
        action="SHORT",
        description="Activist investor files 13D AFTER accumulating puts/short positions (pump-and-dump)",
        indicators=["13D filing date vs 13F put positions", "options market unusual activity before 13D"],
        notes=(
            "BBBY: Ryan Cohen (activist) shorted in May, announced letter to pump, exited at peak. "
            "Check 13F for put positions dated before 13D filing. "
            "If shorts preceded the activism → this is coordinated fraud. Front-run their exit."
        ),
    ),

    PlaybookSignal(
        name="employee_benefit_cuts",
        confidence=0.75,
        timeline_months=18,
        action="SHORT",
        description="401k match eliminated, health insurance degraded, or pension frozen",
        indicators=["8-K: benefits changes", "10-K: employee benefits section", "news searches"],
        notes=(
            "GameStop eliminated 401k match Jan 1 2024 even after Cohen cleaned house. "
            "This is extraction in plain sight. 12–24 month warning. "
            "Precedes cost-cutting that precedes covenant violations."
        ),
    ),

    PlaybookSignal(
        name="coordinated_media_attack",
        confidence=0.70,
        timeline_months=12,
        action="CONTRARIAN_INVESTIGATE",
        description="3+ major financial outlets publish negative GME-specific narratives in the same week",
        indicators=["Yahoo Finance", "Bloomberg", "CNBC", "MarketWatch same-week negative clustering"],
        notes=(
            "Yahoo Finance (Goldman connection), Kotaku/Gawker (PE interests) coordinate attacks. "
            "Media attacks demoralize retail, causing forced selling at bottoms. "
            "For GME specifically: media attacks are often contrarian BUY signals. "
            "Ask: WHY do they want retail to forget this company?"
        ),
    ),

    PlaybookSignal(
        name="short_interest_extreme",
        confidence=0.70,
        timeline_months=6,
        action="SQUEEZE_WATCH",
        description="Short interest exceeds 50% of float",
        indicators=["FINRA short interest reports", "S3 Partners SI data", "borrow rate spike"],
        notes=(
            "GME hit 140% SI (only possible via naked shorting/rehypothecation). "
            "When SI >50% and retail holds, squeeze is mathematically inevitable. "
            "The direction (squeeze UP or crash DOWN) depends on company fundamentals. "
            "GME: zero debt = squeeze. BBBY: $5B debt = crash."
        ),
    ),

    PlaybookSignal(
        name="insider_selling_waterfall",
        confidence=0.70,
        timeline_months=12,
        action="SHORT",
        description="Multiple executives file Form 4 sales in the same quarter",
        indicators=["Form 4 filings on SEC EDGAR"],
        notes=(
            "Isolated sales are normal (executives need liquidity). "
            "Multiple executives selling in same quarter = they know. "
            "PE-connected insiders cash out before announcing news. "
            "6–12 month lead time."
        ),
    ),

    PlaybookSignal(
        name="aggressive_overexpansion",
        confidence=0.65,
        timeline_months=36,
        action="SHORT",
        description="Store/headcount count growing while same-unit revenue is flat or declining",
        indicators=["10-Q: store count trend", "10-Q: same-store sales", "10-Q: revenue per unit"],
        notes=(
            "GameStop peaked at 7,500+ stores with flat revenue. "
            "PE plants this pattern to justify later 'restructuring.' "
            "Unsustainable cash burn creates covenant violations on schedule."
        ),
    ),

    PlaybookSignal(
        name="debt_to_equity_explosion",
        confidence=0.75,
        timeline_months=18,
        action="SHORT",
        description="Total debt exceeds 2× market cap with flat/declining revenue",
        indicators=["10-K: total long-term debt", "market cap"],
        notes=(
            "AMC: raised $500M capital but debt grew $5B→$8B. Capital raises don't fix structural debt. "
            "Ratio >2.0 = distress signal. Ratio >3.0 = bankruptcy within 18 months. "
            "BBBY at filing: debt ~15× remaining equity."
        ),
    ),
]

# ── GME Immunity Checklist ─────────────────────────────────────────────────────
# Conditions that make GME RESISTANT to the PE playbook.
# CTO agent checks these every morning. If any turn red, escalate immediately.

GME_IMMUNITY_CHECKS = [
    {
        "check": "debt_free",
        "description": "GameStop carries zero long-term corporate debt",
        "data_source": "quarterly 10-Q: long-term debt line",
        "green_condition": "long_term_debt == 0",
        "red_alert": "Any new long-term debt issuance — PE regains leverage weapon",
    },
    {
        "check": "cash_position_healthy",
        "description": "Cash + equivalents > $1B",
        "data_source": "quarterly 10-Q: cash and equivalents",
        "green_condition": "cash >= 1_000_000_000",
        "red_alert": "Cash below $1B — runway shrinks, distress financing risk returns",
    },
    {
        "check": "board_free_of_pe",
        "description": "No active PE operative on the board",
        "data_source": "DEF 14A: board biographies",
        "green_condition": "pe_connected_directors == 0",
        "red_alert": "Any new director with Apollo/KKR/Blackstone/Bain history",
    },
    {
        "check": "cohen_control",
        "description": "Ryan Cohen holds chairman position and >10% stake",
        "data_source": "Form 4 + DEF 14A",
        "green_condition": "cohen_is_chairman and cohen_ownership > 0.10",
        "red_alert": "Cohen resignation or stake reduction below 10% — thesis collapses",
    },
    {
        "check": "no_restructuring_advisor",
        "description": "No AlixPartners/Alvarez & Marsal CRO hired",
        "data_source": "8-K filings",
        "green_condition": "cro_hired == False",
        "red_alert": "Immediate EXIT on any restructuring advisor 8-K",
    },
    {
        "check": "profitability_maintained",
        "description": "TTM net income positive",
        "data_source": "10-Q: net income (TTM)",
        "green_condition": "ttm_net_income > 0",
        "red_alert": "Return to net losses — debt-free immunity relies on cash generation continuing",
    },
]

# ── Short Side Target Scoring ──────────────────────────────────────────────────

def score_short_target(signals_detected: list[str]) -> dict:
    """
    Given a list of fired signal names for a company, compute an aggregate
    short target score and recommended action.

    Returns:
        {
          "score": 0-100,
          "confidence": 0.0-1.0,
          "action": "SHORT" | "WATCH" | "PASS",
          "timeline_months": int,
          "reasoning": str
        }
    """
    signal_map = {s.name: s for s in PLAYBOOK_SIGNALS}
    fired = [signal_map[n] for n in signals_detected if n in signal_map]

    if not fired:
        return {"score": 0, "confidence": 0.0, "action": "PASS", "timeline_months": 0, "reasoning": "No signals"}

    # Weighted average confidence
    avg_confidence = sum(s.confidence for s in fired) / len(fired)

    # Score: 10 points per signal, weighted by confidence
    raw_score = sum(s.confidence * 10 for s in fired)
    score = min(100, int(raw_score))

    # Shortest timeline = most urgent
    min_timeline = min(s.timeline_months for s in fired)

    # Action tiers
    if score >= 60 or any(s.name == "restructuring_advisor_hired" for s in fired):
        action = "SHORT"
    elif score >= 30:
        action = "WATCH"
    else:
        action = "PASS"

    reasoning = "; ".join(
        f"{s.name}(conf={s.confidence:.0%}, t={s.timeline_months}mo)"
        for s in sorted(fired, key=lambda x: -x.confidence)
    )

    return {
        "score": score,
        "confidence": round(avg_confidence, 3),
        "action": action,
        "timeline_months": min_timeline,
        "reasoning": reasoning,
    }


# ── Anti-Pattern Guardrails (injected into CTO agent backstory) ────────────────

ANTI_PATTERNS = """
RETAIL ANTI-PATTERNS — Never let the team commit these mistakes:

1. IGNORING BOARD COMPOSITION
   "Who's on the board?" is the first question, not the last.
   Every board change is a signal. PE plants operatives inside.

2. TRUSTING MEDIA CONSENSUS
   Yahoo Finance, Bloomberg, CNBC negative consensus on a fundamentally sound company
   is not analysis — it's narrative control. Investigate WHY they want you to forget it.

3. CONFUSING CAPITAL RAISES WITH FIXES
   AMC raised $500M; debt went from $5B → $8B. Capital raises ≠ problem solved.
   Watch cash flow from operations, not headline capital raises.

4. HOLDING THROUGH RESTRUCTURING ADVISOR HIRING
   The moment AlixPartners or Alvarez & Marsal gets a CRO role = equity is gone.
   There is no turnaround. There is only extraction. Exit same day.

5. MISTAKING COMMUNITY ENTHUSIASM FOR FUNDAMENTALS
   Adam Aron monetized retail enthusiasm while loading AMC with debt.
   Charismatic CEO + meme stock ≠ strong business. Check the balance sheet.

6. SHORTING COMPANIES WITH ZERO DEBT + STRONG INSIDER OWNERSHIP
   The PE playbook requires debt as the primary weapon.
   No debt = no forced covenant violation = no controlled bankruptcy.
   GameStop has zero debt. Shorting it against Ryan Cohen is fighting the wrong war.

7. NOT TRACKING FORM 4 INSIDERS
   When multiple executives file Form 4 sales in the same quarter:
   they know. This is the insider signal retail never reads.

8. IGNORING DEBT MATURITY SCHEDULES
   The cliff is in the 10-K footnotes. Most retail investors never look.
   When $500M+ matures in 18 months and the company can't refinance → short it.
"""

# ── The GameStop Exception (why GME is structurally different) ─────────────────

GME_STRUCTURAL_THESIS = """
GAMESTOP STRUCTURAL THESIS (as of 2024-2025)

WHY GME BROKE THE PLAYBOOK:
1. Ryan Cohen eliminated ALL corporate debt (PE's primary weapon is gone)
2. Board purged of all PE operatives (June 2021 → present)
3. $9B cash position (no financing needed → no covenant risk)
4. Profitable for 9+ consecutive quarters ($1B profit swing in 10 quarters)
5. Right-sized operations (7,500 stores → profitable core number)
6. Short interest structurally elevated (FTDs, rehypothecation history)

SQUEEZE CONDITIONS:
- High short interest + retail holder base that doesn't panic-sell = mathematical squeeze
- Price catalyst triggers cover cascade
- No debt means PE can't force bankruptcy to cover shorts at $0

SHORT SIDE EDGE:
- OTHER stocks following the PE playbook (high debt + PE board + restructuring)
- These are the plays. GME's value is understanding the playbook, then applying it elsewhere.
- The same firms that targeted GME are targeting other companies RIGHT NOW.
- Track: Apollo, KKR, Blackstone, Silver Lake, Ares portfolio companies
  with >$2B debt, PE board members, and stagnant revenue.
"""
