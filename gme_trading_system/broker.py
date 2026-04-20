"""
Interactive Brokers Broker Integration (ib_insync)
────────────────────────────────────────────────────
Connects to a locally-running TWS or IB Gateway instance.
TWS must be open and logged in for orders to execute.

SETUP (one-time, takes 5 minutes):
  1. Download Trader Workstation: https://www.interactivebrokers.co.uk/en/trading/tws.php
     OR IB Gateway (lighter): https://www.interactivebrokers.co.uk/en/trading/ibgateway.php

  2. Log in to TWS/Gateway with your IBKR credentials.

  3. Enable API access in TWS:
     File → Global Configuration → API → Settings
       ✅ Enable ActiveX and Socket Clients
       ✅ Allow connections from localhost only
       Socket port: 7497 (paper) or 7496 (live)
       ✅ Bypass TWS warning for API orders (optional)

  4. Add to .env:
       IBKR_PORT=7497        # 7497=paper TWS, 7496=live TWS, 4002=paper Gateway, 4001=live Gateway
       IBKR_ACCOUNT=         # your IBKR account number e.g. U1234567 (shown in TWS top-right)
       IBKR_CLIENT_ID=1      # any number 1-999, just needs to be unique per connection

  5. Run: python broker.py   — should print your account balance.

HOW IT WORKS:
  - IB Gateway/TWS runs on your machine, handles authentication
  - This code connects to it on localhost via socket
  - Orders are native IBKR bracket orders: entry + stop + take-profit submitted together
  - The position is protected even if this code crashes (IBKR holds the contingent orders)

CURRENCY:
  GME trades in USD. Your GBP is held by IBKR and converted at execution.
  IBKR's FX spread is ~0.5 pip — best in class for retail.
"""
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import yaml
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

IBKR_HOST      = os.getenv("IBKR_HOST",      "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",  "7497"))        # 7497=paper, 7496=live
IBKR_ACCOUNT   = os.getenv("IBKR_ACCOUNT",   "")
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

SYMBOL   = "GME"
DB_PATH  = os.path.join(os.path.dirname(__file__), "agent_memory.db")

# Live port = 7496 or 4001. Paper = 7497 or 4002.
IS_LIVE = IBKR_PORT in (7496, 4001)

def _load_risk_rules() -> dict:
    path = os.path.join(os.path.dirname(__file__), "risk_rules.yaml")
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception:
        return {}

RISK_RULES = _load_risk_rules()


@dataclass
class OrderResult:
    success:          bool
    order_id:         str     # IBKR order ID (integer as string)
    client_order_id:  str     # our UUID
    status:           str     # filled, submitted, presubmitted, cancelled, error
    filled_price:     float
    filled_qty:       float
    error:            str = ""


class IBKRBroker:

    def __init__(self):
        from ib_insync import IB, util
        util.logToConsole(logging.WARNING)   # suppress ib_insync INFO spam
        self._ib = IB()
        self._connected = False
        self._connect()

    def _connect(self):
        try:
            self._ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, readonly=False)
            self._connected = True
            mode = "LIVE" if IS_LIVE else "PAPER"
            log.info(f"[broker] Connected to IBKR [{mode}] on port {IBKR_PORT}")
        except Exception as e:
            self._connected = False
            log.error(f"[broker] IBKR connection failed: {e}")
            log.error(
                "Ensure TWS or IB Gateway is running and API connections are enabled:\n"
                "  TWS → File → Global Configuration → API → Settings → Enable ActiveX and Socket Clients"
            )

    def _ensure_connected(self) -> bool:
        if not self._connected or not self._ib.isConnected():
            log.info("[broker] Reconnecting to IBKR...")
            self._connect()
        return self._connected

    def _gme_contract(self):
        from ib_insync import Stock
        return Stock(SYMBOL, "SMART", "USD")

    # ── Account information ────────────────────────────────────────────────────

    def get_account(self) -> dict | None:
        if not self._ensure_connected():
            return None
        try:
            summary = self._ib.accountSummary(IBKR_ACCOUNT or "")
            result = {}
            for item in summary:
                result[item.tag] = item.value
            return result
        except Exception as e:
            log.error(f"[broker] Account summary failed: {e}")
            return None

    def account_summary(self) -> dict:
        acct = self.get_account()
        if not acct:
            return {"error": "Not connected to TWS/Gateway. Is it running?"}

        equity_usd  = float(acct.get("EquityWithLoanValue", 0) or 0)
        cash_usd    = float(acct.get("CashBalance", 0) or 0)
        buying_pwr  = float(acct.get("BuyingPower", 0) or 0)
        unrealized  = float(acct.get("UnrealizedPnL", 0) or 0)
        daily_pnl   = float(acct.get("RealizedPnL", 0) or 0)

        gbp_rate    = self._gbp_usd_rate()
        equity_gbp  = round(equity_usd / gbp_rate, 2) if gbp_rate else None

        return {
            "mode":             "LIVE" if IS_LIVE else "PAPER",
            "account":          IBKR_ACCOUNT or acct.get("AccountId", "—"),
            "equity_usd":       round(equity_usd, 2),
            "equity_gbp":       equity_gbp,
            "cash_usd":         round(cash_usd, 2),
            "buying_power_usd": round(buying_pwr, 2),
            "unrealized_pnl":   round(unrealized, 2),
            "realized_pnl_today": round(daily_pnl, 2),
            "gbp_usd_rate":     gbp_rate,
        }

    def _gbp_usd_rate(self) -> float:
        try:
            import yfinance as yf
            hist = yf.Ticker("GBPUSD=X").history(period="1d")
            if not hist.empty:
                return round(float(hist["Close"].iloc[-1]), 4)
        except Exception:
            pass
        return 1.27

    # ── Positions ──────────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        if not self._ensure_connected():
            return []
        try:
            positions = self._ib.positions(IBKR_ACCOUNT or "")
            result = []
            for p in positions:
                result.append({
                    "symbol":           p.contract.symbol,
                    "qty":              p.position,
                    "avg_entry_price":  p.avgCost,
                    "market_value":     p.position * p.avgCost,
                })
            return result
        except Exception as e:
            log.error(f"[broker] Positions fetch failed: {e}")
            return []

    def get_gme_position(self) -> dict | None:
        for p in self.get_positions():
            if p["symbol"] == SYMBOL:
                return p
        return None

    # ── Orders ─────────────────────────────────────────────────────────────────

    def submit_bracket_order(
        self,
        action: str,
        qty: float,
        order_id: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> OrderResult:
        """
        Submit a bracket order: parent limit entry + attached stop-loss + attached take-profit.
        All three orders are transmitted together. Position is protected even if this process exits.
        """
        if not self._ensure_connected():
            return OrderResult(
                success=False, order_id="", client_order_id=order_id,
                status="error", filled_price=0.0, filled_qty=0.0,
                error="Not connected to TWS/Gateway",
            )

        from ib_insync import LimitOrder, StopOrder, util

        contract = self._gme_contract()
        side     = "BUY" if action.upper() == "BUY" else "SELL"

        # Qualify the contract (gets full contract details from IBKR)
        try:
            self._ib.qualifyContracts(contract)
        except Exception as e:
            log.warning(f"[broker] Contract qualification warning: {e}")

        # Parent: limit entry order
        parent = LimitOrder(
            side,
            qty,
            round(entry_price, 2),
            orderId=self._ib.client.getReqId(),
            transmit=False,   # don't send yet — wait until all legs are attached
        )

        # Take-profit: limit order on the opposite side
        tp_side = "SELL" if side == "BUY" else "BUY"
        take_profit_order = LimitOrder(
            tp_side,
            qty,
            round(take_profit, 2),
            orderId=self._ib.client.getReqId(),
            parentId=parent.orderId,
            transmit=False,
        )

        # Stop-loss: stop order
        stop_order = StopOrder(
            tp_side,
            qty,
            round(stop_loss, 2),
            orderId=self._ib.client.getReqId(),
            parentId=parent.orderId,
            transmit=True,    # transmit=True on the last leg sends all three together
        )

        mode_str = "LIVE" if IS_LIVE else "PAPER"
        log.info(
            f"[broker] [{mode_str}] BRACKET {side} {qty} {SYMBOL} "
            f"@ {entry_price:.2f} | SL={stop_loss:.2f} TP={take_profit:.2f}"
        )

        try:
            trades = []
            for order in [parent, take_profit_order, stop_order]:
                trade = self._ib.placeOrder(contract, order)
                trades.append(trade)

            # Wait up to 10 seconds for the parent to be acknowledged
            self._ib.sleep(3)
            parent_trade = trades[0]
            status = parent_trade.orderStatus.status or "submitted"

            filled_price = parent_trade.orderStatus.avgFillPrice or 0.0
            filled_qty   = parent_trade.orderStatus.filled or 0.0

            return OrderResult(
                success=status not in ("Cancelled", "Inactive"),
                order_id=str(parent.orderId),
                client_order_id=order_id,
                status=status.lower(),
                filled_price=float(filled_price),
                filled_qty=float(filled_qty),
            )

        except Exception as e:
            log.error(f"[broker] Order submission failed: {e}")
            return OrderResult(
                success=False, order_id="", client_order_id=order_id,
                status="error", filled_price=0.0, filled_qty=0.0,
                error=str(e),
            )

    def cancel_all_orders(self) -> bool:
        if not self._ensure_connected():
            return False
        try:
            self._ib.reqGlobalCancel()
            log.info("[broker] All orders cancelled")
            return True
        except Exception as e:
            log.error(f"[broker] Cancel all failed: {e}")
            return False

    def close_position(self) -> bool:
        """Market order to close the entire GME position."""
        if not self._ensure_connected():
            return False
        pos = self.get_gme_position()
        if not pos or pos["qty"] == 0:
            log.info("[broker] No GME position to close")
            return True

        from ib_insync import MarketOrder
        contract = self._gme_contract()
        qty  = abs(pos["qty"])
        side = "SELL" if pos["qty"] > 0 else "BUY"

        try:
            order = MarketOrder(side, qty)
            self._ib.placeOrder(contract, order)
            self._ib.sleep(2)
            log.info(f"[broker] Close position: {side} {qty} {SYMBOL}")
            return True
        except Exception as e:
            log.error(f"[broker] Close position failed: {e}")
            return False

    # ── Risk guard ─────────────────────────────────────────────────────────────

    def check_daily_loss_limit(self, limit_usd: float | None = None) -> bool:
        if limit_usd is None:
            limit_usd = float(
                RISK_RULES.get("position_limits", {}).get("max_daily_loss_usd", 1.0)
            )
        acct = self.get_account()
        if not acct:
            log.error("[broker] Cannot verify daily P&L — trading halted")
            return False
        daily_pnl = float(acct.get("RealizedPnL", 0) or 0) + float(acct.get("UnrealizedPnL", 0) or 0)
        if daily_pnl <= -limit_usd:
            log.warning(f"[broker] Daily loss limit: ${daily_pnl:.2f} (cap: -${limit_usd:.2f})")
            return False
        return True

    # ── Main execution entry point ─────────────────────────────────────────────

    def execute_trade_decision(self, decision: dict, order_id: str) -> OrderResult:
        if not self.check_daily_loss_limit():
            return OrderResult(
                success=False, order_id="", client_order_id=order_id,
                status="rejected", filled_price=0.0, filled_qty=0.0,
                error="Daily loss limit reached",
            )

        action      = decision["action"]
        entry_price = float(decision["entry_price"])
        stop_loss   = float(decision["stop_loss"])
        take_profit = float(decision["take_profit"])
        max_pos_usd = float(
            RISK_RULES.get("position_limits", {}).get("max_position_size_usd", 3.0)
        )
        qty_usd = float(decision.get("quantity_usd", max_pos_usd))
        qty_usd = min(qty_usd, max_pos_usd)          # never exceed risk rules cap
        shares  = round(max(0.001, qty_usd / entry_price), 6)

        result = self.submit_bracket_order(
            action=action, qty=shares, order_id=order_id,
            entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
        )

        self._log_execution(order_id, result)

        # Telegram notification
        try:
            from notifier import notify_trade
            mode = "LIVE" if IS_LIVE else "PAPER"
            notify_trade(action, entry_price, decision.get("confidence", 0.75),
                         stop_loss, take_profit, shares,
                         status=f"FILLED ({mode})" if result.success else "REJECTED")
        except Exception:
            pass

        return result

    def _log_execution(self, order_id: str, result: OrderResult):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "UPDATE trade_decisions SET status=?, exit_price=?, notes=? WHERE order_id=?",
                (result.status, result.filled_price or 0,
                 f"ibkr_order={result.order_id} {result.error or 'ok'}", order_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[broker] DB log failed: {e}")

    def disconnect(self):
        if self._connected:
            self._ib.disconnect()
            self._connected = False
            log.info("[broker] Disconnected from IBKR")


# ── Singleton ──────────────────────────────────────────────────────────────────

_broker: IBKRBroker | None = None


def get_broker() -> IBKRBroker:
    global _broker
    if _broker is None:
        _broker = IBKRBroker()
    return _broker


# ── Standalone diagnostics ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    broker = IBKRBroker()
    if not broker._connected:
        print("\n❌ Could not connect. Check TWS/Gateway is running with API enabled.")
        exit(1)

    print(f"\n✅ Connected — port {IBKR_PORT} ({'LIVE' if IS_LIVE else 'PAPER'})")

    summary = broker.account_summary()
    print("\n=== Account ===")
    print(json.dumps(summary, indent=2))

    positions = broker.get_positions()
    print(f"\n=== Positions ({len(positions)}) ===")
    for p in positions:
        print(f"  {p['symbol']}: {p['qty']} shares @ avg ${p['avg_entry_price']:.2f}")

    broker.disconnect()
