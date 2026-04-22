#!/usr/bin/env python3
"""CLI tool for team to log signal execution feedback.

Usage:
    python log_signal_feedback.py --alert-id ABC123 --action executed --entry 23.45 --exit 24.50 --member "Alice" --notes "Strong volume"
    python log_signal_feedback.py --alert-id ABC123 --action ignored --notes "Conflicting indicators"
    python log_signal_feedback.py --alert-id ABC123 --action missed --notes "Was AFK, signal fired at 10:30"
    python log_signal_feedback.py --metrics          # Show signal performance
"""
import argparse
import sys
import os
from signal_manager import SignalManager
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


def log_feedback(args):
    """Log team feedback on a signal."""
    manager = SignalManager(DB_PATH)

    if not args.alert_id:
        print("❌ --alert-id is required")
        sys.exit(1)

    if args.action not in ["executed", "ignored", "missed"]:
        print("❌ --action must be 'executed', 'ignored', or 'missed'")
        sys.exit(1)

    # Validate that alert exists
    alert = manager.get_alert_with_feedback(args.alert_id)
    if not alert:
        print(f"❌ Alert {args.alert_id} not found")
        sys.exit(1)

    # Log feedback
    try:
        feedback_id = manager.log_feedback(
            alert_id=args.alert_id,
            action_taken=args.action,
            entry_price=args.entry,
            exit_price=args.exit,
            quantity=args.quantity,
            team_member=args.member,
            team_notes=args.notes or "",
        )
        print(f"✅ Feedback logged: {feedback_id}")
        print(f"   Alert: {alert['agent_name']} | {alert['signal_type']} (confidence={alert['confidence']:.0%})")
        print(f"   Action: {args.action}")
        if args.entry and args.exit:
            pnl_pct = ((args.exit - args.entry) / args.entry) * 100
            print(f"   P&L: {pnl_pct:+.2f}%")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


def show_metrics(args):
    """Display signal performance metrics."""
    manager = SignalManager(DB_PATH)
    result = manager.get_signal_metrics(agent_name=args.agent, signal_type=args.signal_type, days=args.days)

    if "error" in result:
        print(f"❌ {result['error']}")
        sys.exit(1)

    metrics = result["metrics"]
    if not metrics:
        print(f"No signals found in last {result['period_days']} days")
        return

    print(f"\n📊 Signal Metrics (last {result['period_days']} days)\n")
    print(f"{'Agent':<15} {'Signal Type':<20} {'Alerts':<8} {'Exec %':<8} {'Win %':<8} {'Avg PnL %':<10}")
    print("-" * 75)

    for m in sorted(metrics, key=lambda x: x["execution_rate"], reverse=True):
        agent = m["agent"][:14]
        signal = m["signal_type"][:19]
        alerts = m["total_alerts"]
        exec_pct = m["execution_rate"] * 100
        win_pct = m["win_rate"] * 100
        avg_pnl = m["avg_pnl_pct"] or 0.0

        print(f"{agent:<15} {signal:<20} {alerts:<8} {exec_pct:>6.0f}% {win_pct:>6.0f}% {avg_pnl:>+8.2f}%")

    print()


def show_recent(args):
    """Show recent alerts."""
    manager = SignalManager(DB_PATH)
    alerts = manager.get_recent_alerts(limit=args.limit, agent_name=args.agent)

    if not alerts:
        print("No recent alerts")
        return

    print(f"\n📢 Recent Alerts (last {args.limit})\n")
    print(f"{'Agent':<12} {'Type':<18} {'Conf':<6} {'Entry':<8} {'Stop':<8} {'Target':<8} {'Status':<10}")
    print("-" * 75)

    for alert in alerts:
        agent = alert["agent_name"][:11]
        sig_type = alert["signal_type"][:17]
        conf = alert["confidence"] * 100
        entry = f"{alert['entry_price']:.2f}" if alert["entry_price"] else "—"
        stop = f"{alert['stop_loss']:.2f}" if alert["stop_loss"] else "—"
        target = f"{alert['take_profit']:.2f}" if alert["take_profit"] else "—"

        # Check if feedback exists
        feedback = None
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            feedback_row = conn.execute("SELECT action_taken FROM signal_feedback WHERE alert_id = ?", (alert["id"],)).fetchone()
            conn.close()
            feedback = dict(feedback_row) if feedback_row else None
        except:
            pass

        status = feedback["action_taken"] if feedback else "Pending"

        print(f"{agent:<12} {sig_type:<18} {conf:>4.0f}% {entry:>7} {stop:>7} {target:>7} {status:<10}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Log signal execution feedback or view metrics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Log feedback command
    log_parser = subparsers.add_parser("log", help="Log feedback on an alert")
    log_parser.add_argument("--alert-id", required=True, help="Alert ID to feedback on")
    log_parser.add_argument("--action", required=True, choices=["executed", "ignored", "missed"], help="Action taken")
    log_parser.add_argument("--entry", type=float, help="Entry price (if executed)")
    log_parser.add_argument("--exit", type=float, help="Exit price (if executed)")
    log_parser.add_argument("--quantity", type=float, help="Trade quantity")
    log_parser.add_argument("--member", help="Team member name")
    log_parser.add_argument("--notes", help="Team notes")
    log_parser.set_defaults(func=log_feedback)

    # Metrics command
    metrics_parser = subparsers.add_parser("metrics", help="Show signal performance metrics")
    metrics_parser.add_argument("--agent", help="Filter by agent name")
    metrics_parser.add_argument("--signal-type", help="Filter by signal type")
    metrics_parser.add_argument("--days", type=int, default=30, help="Look back N days (default: 30)")
    metrics_parser.set_defaults(func=show_metrics)

    # Recent alerts command
    recent_parser = subparsers.add_parser("recent", help="Show recent alerts")
    recent_parser.add_argument("--limit", type=int, default=10, help="Number of alerts to show (default: 10)")
    recent_parser.add_argument("--agent", help="Filter by agent name")
    recent_parser.set_defaults(func=show_recent)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
