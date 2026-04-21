"""
Trove Score screen — fetches financials via yfinance and scores each ticker.
Altman Z-Score (revised, non-manufacturing) computed from balance sheet components.

Usage:
  python trove_screen.py                     # score all default tickers, summary table
  python trove_screen.py --detail            # same + full per-ticker breakdown
  python trove_screen.py --detail GME AAPL   # detail for specific tickers only
  python trove_screen.py GME AAPL            # summary for specific tickers only
"""

import argparse
import math
import warnings
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Scoring tables ────────────────────────────────────────────────────────────

Breakpoints = list[tuple[float, float]]

def _asc(value: float, table: Breakpoints) -> float:
    """First threshold >= value wins (lower value = more points)."""
    for threshold, pts in table:
        if value <= threshold:
            return pts
    return table[-1][1]

def _desc(value: float, table: Breakpoints, default: float = 0.0) -> float:
    """First threshold <= value wins (higher value = more points)."""
    for threshold, pts in table:
        if value >= threshold:
            return pts
    return default

_EV_FCF    = [(5,10.0),(10,8.5),(15,6.5),(20,4.5),(30,2.0),(math.inf,0.5)]
_EV_EBITDA = [(0,0.0),(5,10.0),(8,8.5),(12,6.5),(16,4.5),(25,2.0),(math.inf,0.5)]
_PB        = [(0,0.0),(0.5,10.0),(1.0,8.5),(1.5,6.5),(2.0,4.5),(3.0,2.5),(math.inf,0.5)]
_ALTMAN    = [(3.0,15.0),(1.81,7.5)]          # default 2.25 = distress zone
_DE        = [(0,15.0),(0.10,14.25),(0.30,12.0),(0.50,9.0),(1.00,6.0),(2.00,2.25),(math.inf,0.0)]
_NET_CASH  = [(1.0,15.0),(0.5,12.75),(0.3,10.5),(0.2,8.25),(0.1,6.0),(0.0,3.75)]
_OP_MARGIN = [(0,0.0),(0.03,1.5),(0.08,3.5),(0.12,5.5),(0.18,7.0),(0.25,8.5),(math.inf,10.0)]
_ROE       = [(0,0.0),(0.05,2.0),(0.10,4.0),(0.15,6.0),(0.20,7.5),(math.inf,10.0)]
_RATINGS   = [(80,"★★★★★"),(65,"★★★★☆"),(50,"★★★☆☆"),(35,"★★☆☆☆"),(20,"★☆☆☆☆")]

DEFAULT_TICKERS = [
    "VIPS","GME","ROIV","INTC","JHG","CART","FSLR","MNST","REGN","PATH",
    "TWLO","NOK","AMKR","ALGN","PINS","GROV","PUMP","CTRA","PHM","GFS",
    "AAPL","UA","NWSA","AMC","CPNG","CHWY","BKNG",
]


# ── Input model ───────────────────────────────────────────────────────────────

@dataclass
class TroveInputs:
    ev_fcf:           float
    ev_ebitda:        float
    pb:               float
    altman_z:         Optional[float]
    debt_equity:      float
    cash_mm:          float
    total_debt_mm:    float
    market_cap_mm:    float
    operating_margin: float
    roe:              float
    net_margin:       float

    @property
    def net_cash_pct(self) -> float:
        return (self.cash_mm - self.total_debt_mm) / self.market_cap_mm


# ── Scorer ────────────────────────────────────────────────────────────────────

def trove_score(i: TroveInputs) -> dict:
    nc = i.net_cash_pct

    a1 = (5.0 if nc >= 0.10 else 0.0) if i.ev_fcf <= 0 else _asc(i.ev_fcf, _EV_FCF)
    a2 = _asc(i.ev_ebitda, _EV_EBITDA)
    a3 = _asc(i.pb, _PB)

    b1 = _desc(i.altman_z, _ALTMAN, default=2.25) if i.altman_z is not None else 6.0
    b2 = 0.0 if i.debt_equity < 0 else _asc(i.debt_equity, _DE)
    b3 = _desc(nc, _NET_CASH)

    c1 = _asc(i.operating_margin, _OP_MARGIN)
    c2 = _asc(i.roe, _ROE)
    c3 = 5.0 if i.net_margin > 0 else 0.0

    total = a1 + a2 + a3 + b1 + b2 + b3 + c1 + c2 + c3
    rating = next((s for t, s in _RATINGS if total >= t), "☆☆☆☆☆")

    immunity = {
        "debt_free":         i.total_debt_mm < 0.01 * i.market_cap_mm,
        "cash_over_1b":      i.cash_mm > 1_000,
        "net_cash_positive": i.cash_mm > i.total_debt_mm,
        "profitable":        i.net_margin > 0,
        "altman_safe":       i.altman_z is not None and i.altman_z > 2.99,
    }

    return {
        "scores":  {"A1":a1,"A2":a2,"A3":a3,"B1":b1,"B2":b2,"B3":b3,"C1":c1,"C2":c2,"C3":c3},
        "pillars": {"A": a1+a2+a3, "B": b1+b2+b3, "C": c1+c2+c3},
        "total":   round(total, 2),
        "rating":  rating,
        "immunity": immunity,
        "immunity_count": sum(immunity.values()),
    }


# ── Data fetcher ──────────────────────────────────────────────────────────────

def _safe(val, fallback=float("nan")):
    try:
        v = float(val)
        return fallback if math.isnan(v) else v
    except (TypeError, ValueError):
        return fallback


def _bs_get(df, *keys) -> Optional[float]:
    """Pull the most-recent (first column) value for any matching row key."""
    for k in keys:
        if k in df.index:
            try:
                v = float(df.loc[k].iloc[0])
                if not math.isnan(v):
                    return v
            except (TypeError, ValueError):
                continue
    return None


def altman_z_revised(bs, income, mcap_mm: float) -> Optional[float]:
    """Revised Altman Z' (non-manufacturing): 6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4"""
    try:
        # yfinance uses "Title Case With Spaces"
        total_assets  = _bs_get(bs, "Total Assets")
        total_liab    = _bs_get(bs, "Total Liabilities Net Minority Interest")
        work_capital  = _bs_get(bs, "Working Capital")
        retained_earn = _bs_get(bs, "Retained Earnings")
        ebit          = _bs_get(income, "EBIT") or _bs_get(income, "Operating Income")

        # Fallback: derive working capital if not directly available
        if work_capital is None:
            cur_assets = _bs_get(bs, "Current Assets")
            cur_liab   = _bs_get(bs, "Current Liabilities")
            if cur_assets is not None and cur_liab is not None:
                work_capital = cur_assets - cur_liab

        if None in (total_assets, total_liab, work_capital, retained_earn, ebit):
            return None
        if total_assets == 0 or total_liab == 0:
            return None

        x1 = work_capital / total_assets
        x2 = retained_earn / total_assets
        x3 = ebit / total_assets
        x4 = (mcap_mm * 1e6) / total_liab

        return round(6.56*x1 + 3.26*x2 + 6.72*x3 + 1.05*x4, 2)
    except Exception:
        return None


def fetch_inputs(ticker: str) -> Optional[TroveInputs]:
    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}

        mcap_mm       = _safe(info.get("marketCap"), 0) / 1e6
        if mcap_mm == 0:
            return None

        cash_mm       = _safe(info.get("totalCash"), 0) / 1e6
        total_debt_mm = _safe(info.get("totalDebt"), 0) / 1e6
        ev_mm         = _safe(info.get("enterpriseValue"), 0) / 1e6
        ev_ebitda     = _safe(info.get("enterpriseToEbitda"), float("nan"))
        pb            = _safe(info.get("priceToBook"), float("nan"))
        de            = _safe(info.get("debtToEquity"), float("nan"))
        if not math.isnan(de):
            de /= 100   # yfinance returns D/E as a percentage (e.g. 83 = 0.83)

        op_margin  = _safe(info.get("operatingMargins"), float("nan"))
        roe        = _safe(info.get("returnOnEquity"), float("nan"))
        net_margin = _safe(info.get("profitMargins"), float("nan"))

        fcf = _safe(info.get("freeCashflow"), None)
        ev_fcf = (ev_mm / (fcf / 1e6)) if (fcf and fcf != 0 and ev_mm != 0) else float("nan")

        try:
            bs     = t.balance_sheet
            income = t.income_stmt
            altman = altman_z_revised(bs, income, mcap_mm) if (bs is not None and not bs.empty) else None
        except Exception:
            altman = None

        def nan_to(v, sentinel):
            return sentinel if (v is None or (isinstance(v, float) and math.isnan(v))) else v

        return TroveInputs(
            ev_fcf           = nan_to(ev_fcf, 0),    # 0 → cash-fortress path
            ev_ebitda        = nan_to(ev_ebitda, 99),
            pb               = nan_to(pb, 99),
            altman_z         = altman,               # None → neutral 6 pts
            debt_equity      = nan_to(de, 0),
            cash_mm          = cash_mm,
            total_debt_mm    = total_debt_mm,
            market_cap_mm    = mcap_mm,
            operating_margin = nan_to(op_margin, 0),
            roe              = nan_to(roe, 0),
            net_margin       = nan_to(net_margin, 0),
        )
    except Exception as e:
        print(f"  [{ticker}] fetch error: {e}")
        return None


# ── Output helpers ────────────────────────────────────────────────────────────

_IMM_LABELS = {
    "debt_free":         "🛡️  Debt-free       (Total Debt < 1% MCap)",
    "cash_over_1b":      "🛡️  Cash > $1B",
    "net_cash_positive": "🛡️  Net Cash +       (Cash > Total Debt)",
    "profitable":        "🛡️  Profitable       (Net Margin > 0)",
    "altman_safe":       "🛡️  Altman Safe      (Z > 2.99)",
}

_SCORE_LABELS = {
    "A1": "A1  EV/FCF         (10 pts)",
    "A2": "A2  EV/EBITDA      (10 pts)",
    "A3": "A3  P/B            (10 pts)",
    "B1": "B1  Altman Z       (15 pts)",
    "B2": "B2  Debt/Equity    (15 pts)",
    "B3": "B3  Net Cash % MCap(15 pts)",
    "C1": "C1  Op Margin      (10 pts)",
    "C2": "C2  ROE            (10 pts)",
    "C3": "C3  Net Margin > 0  (5 pts)",
}

def print_detail(ticker: str, inp: TroveInputs, result: dict) -> None:
    s  = result["scores"]
    nc = inp.net_cash_pct

    print(f"\n{'─'*60}")
    print(f"  {ticker}  —  {result['total']}/100  {result['rating']}  "
          f"  🛡️ {result['immunity_count']}/5")
    print(f"{'─'*60}")

    # Raw inputs
    print(f"  MCap ${inp.market_cap_mm:,.0f}mm  |  Cash ${inp.cash_mm:,.0f}mm  "
          f"|  Debt ${inp.total_debt_mm:,.0f}mm  |  Net Cash {nc*100:.1f}%")
    print(f"  EV/FCF {inp.ev_fcf:.1f}x  |  EV/EBITDA {inp.ev_ebitda:.1f}x  "
          f"|  P/B {inp.pb:.2f}x  |  D/E {inp.debt_equity:.2f}")
    print(f"  Altman Z {inp.altman_z}  |  OpMgn {inp.operating_margin*100:.1f}%  "
          f"|  ROE {inp.roe*100:.1f}%  |  NetMgn {inp.net_margin*100:.1f}%")
    print()

    # Scores
    pillar_pts = {"A": 0.0, "B": 0.0, "C": 0.0}
    for key, label in _SCORE_LABELS.items():
        pts   = s[key]
        pillar = key[0]
        pillar_pts[pillar] += pts
        bar = "█" * int(pts) + ("▌" if pts % 1 >= 0.5 else "")
        print(f"  {label:<30}  {pts:5.2f}  {bar}")
        if key in ("A3", "B3"):
            pillar_label = {"A3": "Pillar A (Val /30)", "B3": "Pillar B (Cap /45)"}[key]
            print(f"  {'':30}  {pillar_pts[pillar]:5.1f}  ← {pillar_label}")

    print(f"  {'':30}  {pillar_pts['C']:5.1f}  ← Pillar C (Qual /25)")
    print()

    # Immunity
    for flag, label in _IMM_LABELS.items():
        mark = "✓" if result["immunity"][flag] else "✗"
        print(f"  {mark}  {label}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trove Score deep-value screen")
    parser.add_argument("tickers", nargs="*", help="Tickers to score (default: full watchlist)")
    parser.add_argument("--detail", nargs="*", metavar="TICKER",
                        help="Print full breakdown. No args = all scored tickers; "
                             "with args = only those tickers.")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers] if args.tickers else DEFAULT_TICKERS

    # Which tickers get the detail view?
    if args.detail is None:
        detail_set = set()           # no --detail flag: summary only
    elif len(args.detail) == 0:
        detail_set = set(tickers)    # --detail with no args: all
    else:
        detail_set = {t.upper() for t in args.detail}

    rows = []
    for ticker in tickers:
        print(f"Fetching {ticker}...", end=" ", flush=True)
        inp = fetch_inputs(ticker)
        if inp is None:
            print("SKIP")
            continue
        result = trove_score(inp)

        if ticker in detail_set:
            print_detail(ticker, inp, result)
        else:
            print(f"{result['total']:5.1f}  {result['rating']}")

        rows.append({
            "Ticker":    ticker,
            "Score":     result["total"],
            "Rating":    result["rating"],
            "A (Val)":   round(result["pillars"]["A"], 1),
            "B (Cap)":   round(result["pillars"]["B"], 1),
            "C (Qual)":  round(result["pillars"]["C"], 1),
            "🛡️":        f"{result['immunity_count']}/5",
            "EV/FCF":    round(inp.ev_fcf, 1),
            "EV/EBITDA": round(inp.ev_ebitda, 1),
            "P/B":       round(inp.pb, 2),
            "AltmanZ":   inp.altman_z,
            "D/E":       round(inp.debt_equity, 2),
            "NetCash%":  f"{round(inp.net_cash_pct*100, 1)}%",
            "OpMgn%":    f"{round(inp.operating_margin*100, 1)}%",
            "ROE%":      f"{round(inp.roe*100, 1)}%",
            "NetMgn%":   f"{round(inp.net_margin*100, 1)}%",
        })

    if not rows:
        return

    df = (pd.DataFrame(rows)
          .sort_values("Score", ascending=False)
          .reset_index(drop=True))
    df.index += 1

    cols = ["Ticker","Score","Rating","A (Val)","B (Cap)","C (Qual)","🛡️",
            "EV/FCF","EV/EBITDA","P/B","AltmanZ","D/E","NetCash%","OpMgn%","ROE%","NetMgn%"]
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.1f}".format)

    print(f"\n{'='*130}")
    print("TROVE SCORE RANKINGS")
    print(f"{'='*130}")
    print(df[cols].to_string())
    print(f"{'='*130}")
    print("AltmanZ=None → scored at neutral 6.0 pts (B1). "
          "EV/FCF≤0 with NetCash%≥10% → cash-fortress 5 pts.")

if __name__ == "__main__":
    main()
