"""LLM cost tracking and budget management service."""
import sqlite3
import logging
import requests
import os
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DB_PATH = "agent_memory.db"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# Cost estimates per LLM (rough, update with actual provider pricing)
COST_PER_1K_TOKENS = {
    "deepseek": 0.00014,      # $0.14 per 1M tokens
    "gemini": 0.0001,          # Free tier, $1 for high volume
    "gemma": 0.0,              # Free (local Ollama)
}

# Daily budget
DAILY_BUDGET_USD = 5.00


class CostService:
    """Tracks LLM spending and enforces budgets."""

    def __init__(self, db_path: str = DB_PATH, daily_budget: float = DAILY_BUDGET_USD):
        self.db_path = db_path
        self.daily_budget = daily_budget

    @staticmethod
    def estimate_cost(llm_provider: str, tokens: int) -> float:
        """Estimate cost for tokens."""
        cost_per_token = COST_PER_1K_TOKENS.get(llm_provider, 0.0001)
        return (tokens / 1000) * cost_per_token

    def log_agent_run(self, agent_name: str, llm_provider: str, tokens: int, task_type: str):
        """Log a single agent run and its cost."""
        cost = self.estimate_cost(llm_provider, tokens)

        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO agent_costs (agent_name, run_timestamp, llm_provider, tokens_used, cost_usd, task_type)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (agent_name, datetime.now().isoformat(), llm_provider, tokens, cost, task_type)
            )
            conn.commit()
            conn.close()

            # Check budget
            daily_cost = self.get_daily_cost()
            if daily_cost > self.daily_budget:
                log.warning(f"[cost] Daily budget exceeded: ${daily_cost:.2f} > ${self.daily_budget:.2f}")

            return cost
        except Exception as e:
            log.error(f"[cost] Log run failed: {e}")
            return 0.0

    def get_daily_cost(self) -> float:
        """Get total cost for today."""
        try:
            conn = sqlite3.connect(self.db_path)
            result = conn.execute(
                """SELECT SUM(cost_usd) FROM agent_costs
                   WHERE DATE(run_timestamp) = DATE('now')"""
            ).fetchone()
            conn.close()

            return result[0] or 0.0
        except Exception as e:
            log.error(f"[cost] Get daily cost failed: {e}")
            return 0.0

    def get_daily_cost_summary(self) -> Dict[str, Any]:
        """Get detailed daily cost breakdown."""
        try:
            conn = sqlite3.connect(self.db_path)

            # Total cost today
            total = conn.execute(
                """SELECT SUM(cost_usd) FROM agent_costs
                   WHERE DATE(run_timestamp) = DATE('now')"""
            ).fetchone()[0] or 0.0

            # By agent
            by_agent = []
            agent_costs = conn.execute(
                """SELECT agent_name, SUM(cost_usd) as cost, SUM(tokens_used) as tokens
                   FROM agent_costs WHERE DATE(run_timestamp) = DATE('now')
                   GROUP BY agent_name ORDER BY cost DESC"""
            ).fetchall()

            for agent, cost, tokens in agent_costs:
                by_agent.append({
                    "agent": agent,
                    "cost": round(cost or 0.0, 4),
                    "tokens": tokens or 0
                })

            # By provider
            by_provider = {}
            provider_costs = conn.execute(
                """SELECT llm_provider, SUM(cost_usd) as cost
                   FROM agent_costs WHERE DATE(run_timestamp) = DATE('now')
                   GROUP BY llm_provider"""
            ).fetchall()

            for provider, cost in provider_costs:
                by_provider[provider] = round(cost or 0.0, 4)

            conn.close()

            return {
                "total_cost_usd": round(total, 4),
                "budget_daily": self.daily_budget,
                "percent_used": round((total / self.daily_budget * 100) if self.daily_budget > 0 else 0, 1),
                "by_agent": by_agent,
                "by_provider": by_provider,
                "status": "OK" if total < self.daily_budget else "WARNING" if total < self.daily_budget * 1.1 else "OVER_BUDGET"
            }
        except Exception as e:
            log.error(f"[cost] Get daily summary failed: {e}")
            return {
                "total_cost_usd": 0.0,
                "budget_daily": self.daily_budget,
                "percent_used": 0.0,
                "by_agent": [],
                "by_provider": {},
                "status": "ERROR"
            }

    def get_agent_costs(self, agent_name: str, days: int = 7) -> List[Dict[str, Any]]:
        """Get cost history for an agent."""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT run_timestamp, llm_provider, tokens_used, cost_usd, task_type
                   FROM agent_costs
                   WHERE agent_name = ? AND run_timestamp > datetime('now', '-{} days')
                   ORDER BY run_timestamp DESC""".format(days),
                (agent_name,)
            ).fetchall()
            conn.close()

            return [
                {
                    "timestamp": row[0],
                    "provider": row[1],
                    "tokens": row[2],
                    "cost": round(row[3], 4),
                    "task_type": row[4]
                }
                for row in rows
            ]
        except Exception as e:
            log.error(f"[cost] Get agent costs failed: {e}")
            return []

    def get_cost_per_signal(self) -> float:
        """Calculate cost per trading signal (research signals)."""
        try:
            conn = sqlite3.connect(self.db_path)

            # Total cost for research agents today
            research_cost = conn.execute(
                """SELECT SUM(cost_usd) FROM agent_costs
                   WHERE DATE(run_timestamp) = DATE('now')
                   AND agent_name IN ('Newsie', 'Pattern', 'Synthesis', 'CTO')"""
            ).fetchone()[0] or 0.0

            # Number of signals generated (agent logs with good status)
            signal_count = conn.execute(
                """SELECT COUNT(*) FROM agent_logs
                   WHERE DATE(timestamp) = DATE('now')
                   AND task_type IN ('synthesis', 'structural_brief')
                   AND status = 'ok'"""
            ).fetchone()[0] or 1  # Avoid division by zero

            conn.close()

            return research_cost / signal_count if signal_count > 0 else 0.0
        except Exception as e:
            log.error(f"[cost] Get cost per signal failed: {e}")
            return 0.0

    def check_budget_status(self) -> Dict[str, Any]:
        """Check if we're within budget."""
        daily_cost = self.get_daily_cost()
        remaining = self.daily_budget - daily_cost
        percent_used = (daily_cost / self.daily_budget * 100) if self.daily_budget > 0 else 0

        return {
            "daily_cost": round(daily_cost, 4),
            "daily_budget": self.daily_budget,
            "remaining": round(remaining, 4),
            "percent_used": round(percent_used, 1),
            "status": "OK" if remaining > 0 else "OVER_BUDGET",
            "alert": remaining < (self.daily_budget * 0.2)  # Alert at 80% usage
        }

    def get_deepseek_balance(self) -> Dict[str, Any]:
        """Get actual DeepSeek account balance."""
        if not DEEPSEEK_API_KEY:
            return {
                "error": "DEEPSEEK_API_KEY not set",
                "balance_usd": None
            }

        try:
            response = requests.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                balance_usd = data.get("balance_usd") or data.get("balance", 0)

                return {
                    "balance_usd": round(float(balance_usd), 2),
                    "currency": "USD",
                    "status": "active",
                    "last_checked": datetime.now().isoformat()
                }
            else:
                log.warning(f"[cost] DeepSeek balance check failed: {response.status_code}")
                return {
                    "error": f"API error {response.status_code}",
                    "balance_usd": None
                }

        except requests.RequestException as e:
            log.error(f"[cost] DeepSeek balance request failed: {e}")
            return {
                "error": str(e),
                "balance_usd": None
            }

    def get_account_health(self) -> Dict[str, Any]:
        """Check overall account health: budget, actual balance, burn rate."""
        daily_cost = self.get_daily_cost()
        deepseek_balance = self.get_deepseek_balance()
        budget_status = self.check_budget_status()

        balance = deepseek_balance.get("balance_usd")
        daily_burn = daily_cost

        if balance is not None and daily_burn > 0:
            days_until_empty = balance / daily_burn
        else:
            days_until_empty = None

        return {
            "deepseek_balance": balance,
            "daily_burn": round(daily_cost, 4),
            "days_until_empty": round(days_until_empty, 1) if days_until_empty else None,
            "daily_budget": self.daily_budget,
            "status": "HEALTHY" if balance and balance > (daily_cost * 7) else "WARNING" if balance and balance > daily_cost else "CRITICAL",
            "alerts": self._generate_alerts(balance, daily_cost, budget_status)
        }

    def _generate_alerts(self, balance: Optional[float], daily_burn: float, budget_status: Dict) -> List[str]:
        """Generate alerts if issues detected."""
        alerts = []

        if budget_status.get("alert"):
            cost = budget_status.get('total_cost_usd', 0)
            budget = budget_status.get('budget_daily', 0)
            alerts.append(f"⚠️ Daily budget at {budget_status['percent_used']}% (${cost:.2f} of ${budget:.2f})")

        if balance is not None:
            if balance < (daily_burn * 1):
                alerts.append(f"🔴 CRITICAL: DeepSeek balance ${balance:.2f} is less than 1 day of spending")
            elif balance < (daily_burn * 3):
                alerts.append(f"⚠️ LOW: DeepSeek balance ${balance:.2f} is less than 3 days of spending")
            elif balance < (daily_burn * 7):
                alerts.append(f"ℹ️ INFO: DeepSeek balance ${balance:.2f} is less than 1 week of spending")

        return alerts
