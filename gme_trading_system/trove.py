"""
Trove Score — deep-value scoring framework.
Pillars: A=Valuation (30pts), B=Capital Structure (45pts), C=Quality (25pts).

Shared library used by TroveScoreTool (agents) and the /trove Telegram command.
"""

import math
import warnings
from dataclasses import dataclass
from typing import Optional

warnings.filterwarnings("ignore")

# ── Scoring tables ────────────────────────────────────────────────────────────

Breakpoints = list[tuple[float, float]]

def _asc(value: float, table: Breakpoints) -> float:
    for threshold, pts in table:
        if value <= threshold:
            return pts
    return table[-1][1]

def _desc(value: float, table: Breakpoints, default: float = 0.0) -> float:
    for threshold, pts in table:
        if value >= threshold:
            return pts
    return default

_EV_FCF    = [(5,10.0),(10,8.5),(15,6.5),(20,4.5),(30,2.0),(math.inf,0.5)]
_EV_EBITDA = [(0,0.0),(5,10.0),(8,8.5),(12,6.5),(16,4.5),(25,2.0),(math.inf,0.5)]
_PB        = [(0,0.0),(0.5,10.0),(1.0,8.5),(1.5,6.5),(2.0,4.5),(3.0,2.5),(math.inf,0.5)]
_ALTMAN    = [(3.0,15.0),(1.81,7.5)]
_DE        = [(0,15.0),(0.10,14.25),(0.30,12.0),(0.50,9.0),(1.00,6.0),(2.00,2.25),(math.inf,0.0)]
_NET_CASH  = [(1.0,15.0),(0.5,12.75),(0.3,10.5),(0.2,8.25),(0.1,6.0),(0.0,3.75)]
_OP_MARGIN = [(0,0.0),(0.03,1.5),(0.08,3.5),(0.12,5.5),(0.18,7.0),(0.25,8.5),(math.inf,10.0)]
_ROE       = [(0,0.0),(0.05,2.0),(0.10,4.0),(0.15,6.0),(0.20,7.5),(math.inf,10.0)]
_RATINGS   = [(80,"★★★★★"),(65,"★★★★☆"),(50,"★★★☆☆"),(35,"★★☆☆☆"),(20,"★☆☆☆☆")]

DEFAULT_WATCHLIST = [
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

def score(inputs: TroveInputs) -> dict:
    i  = inputs
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

    total  = a1 + a2 + a3 + b1 + b2 + b3 + c1 + c2 + c3
    rating = next((s for t, s in _RATINGS if total >= t), "☆☆☆☆☆")

    immunity = {
        "debt_free":         i.total_debt_mm < 0.01 * i.market_cap_mm,
        "cash_over_1b":      i.cash_mm > 1_000,
        "net_cash_positive": i.cash_mm > i.total_debt_mm,
        "profitable":        i.net_margin > 0,
        "altman_safe":       i.altman_z is not None and i.altman_z > 2.99,
    }

    return {
        "scores":         {"A1":a1,"A2":a2,"A3":a3,"B1":b1,"B2":b2,"B3":b3,"C1":c1,"C2":c2,"C3":c3},
        "pillars":        {"A": round(a1+a2+a3,2), "B": round(b1+b2+b3,2), "C": round(c1+c2+c3,2)},
        "total":          round(total, 2),
        "rating":         rating,
        "immunity":       immunity,
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
    for k in keys:
        if k in df.index:
            try:
                v = float(df.loc[k].iloc[0])
                if not math.isnan(v):
                    return v
            except (TypeError, ValueError):
                continue
    return None


def _altman_z(bs, income, mcap_mm: float) -> Optional[float]:
    """Revised Altman Z' (non-manufacturing): 6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4"""
    try:
        total_assets  = _bs_get(bs, "Total Assets")
        total_liab    = _bs_get(bs, "Total Liabilities Net Minority Interest")
        work_capital  = _bs_get(bs, "Working Capital")
        retained_earn = _bs_get(bs, "Retained Earnings")
        ebit          = _bs_get(income, "EBIT") or _bs_get(income, "Operating Income")

        if work_capital is None:
            ca = _bs_get(bs, "Current Assets")
            cl = _bs_get(bs, "Current Liabilities")
            work_capital = (ca - cl) if (ca is not None and cl is not None) else None

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


def fetch(ticker: str) -> Optional[TroveInputs]:
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        info = t.info or {}

        mcap_mm = _safe(info.get("marketCap"), 0) / 1e6
        if mcap_mm == 0:
            return None

        cash_mm       = _safe(info.get("totalCash"),       0) / 1e6
        total_debt_mm = _safe(info.get("totalDebt"),        0) / 1e6
        ev_mm         = _safe(info.get("enterpriseValue"),  0) / 1e6
        ev_ebitda     = _safe(info.get("enterpriseToEbitda"), float("nan"))
        pb            = _safe(info.get("priceToBook"),        float("nan"))
        de            = _safe(info.get("debtToEquity"),       float("nan"))
        if not math.isnan(de):
            de /= 100   # yfinance returns as percentage

        op_margin  = _safe(info.get("operatingMargins"), float("nan"))
        roe        = _safe(info.get("returnOnEquity"),   float("nan"))
        net_margin = _safe(info.get("profitMargins"),    float("nan"))

        fcf = _safe(info.get("freeCashflow"), None)
        ev_fcf = (ev_mm / (fcf / 1e6)) if (fcf and fcf != 0 and ev_mm != 0) else float("nan")

        try:
            bs     = t.balance_sheet
            income = t.income_stmt
            altman = _altman_z(bs, income, mcap_mm) if (bs is not None and not bs.empty) else None
        except Exception:
            altman = None

        def nan_to(v, sentinel):
            return sentinel if (v is None or (isinstance(v, float) and math.isnan(v))) else v

        return TroveInputs(
            ev_fcf           = nan_to(ev_fcf,    0),
            ev_ebitda        = nan_to(ev_ebitda, 99),
            pb               = nan_to(pb,        99),
            altman_z         = altman,
            debt_equity      = nan_to(de,        0),
            cash_mm          = cash_mm,
            total_debt_mm    = total_debt_mm,
            market_cap_mm    = mcap_mm,
            operating_margin = nan_to(op_margin,  0),
            roe              = nan_to(roe,         0),
            net_margin       = nan_to(net_margin,  0),
        )
    except Exception:
        return None


def run_screen(tickers: list[str], max_tickers: int = 30) -> list[dict]:
    """Fetch and score a list of tickers. Returns list of result dicts sorted by score desc."""
    import time
    results = []
    for i, ticker in enumerate(tickers[:max_tickers]):
        # Rate limit: small delay between requests to avoid yfinance throttling
        if i > 0:
            time.sleep(0.5)

        # Retry logic for flaky connections
        for attempt in range(2):
            try:
                inp = fetch(ticker)
                if inp is None:
                    break
                result = score(inp)
                results.append({
                    "ticker":    ticker,
                    "score":     result["total"],
                    "rating":    result["rating"],
                    "pillar_A":  result["pillars"]["A"],
                    "pillar_B":  result["pillars"]["B"],
                    "pillar_C":  result["pillars"]["C"],
                    "immunity":  result["immunity_count"],
                    "ev_fcf":    round(inp.ev_fcf, 1),
                    "ev_ebitda": round(inp.ev_ebitda, 1),
                    "pb":        round(inp.pb, 2),
                    "altman_z":  inp.altman_z,
                    "de":        round(inp.debt_equity, 2),
                    "net_cash_pct": round(inp.net_cash_pct * 100, 1),
                    "op_margin": round(inp.operating_margin * 100, 1),
                    "roe":       round(inp.roe * 100, 1),
                    "net_margin":round(inp.net_margin * 100, 1),
                })
                break  # success, move to next ticker
            except Exception as e:
                if attempt == 1:
                    # Failed after retry
                    pass
                else:
                    # Retry once more after short delay
                    time.sleep(1)

    return sorted(results, key=lambda x: x["score"], reverse=True)
