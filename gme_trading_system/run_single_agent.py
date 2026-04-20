"""
Iterative development runner — run one agent+task at a time.

Usage:
    python run_single_agent.py daily_trend
    python run_single_agent.py news
    python run_single_agent.py futurist
    python run_single_agent.py manager
    python run_single_agent.py trader
    python run_single_agent.py full     # entire pipeline
"""
import sys
import sqlite3
import os
from datetime import datetime
from crewai import Crew, Process
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

AGENT_MAP = {
    "daily_trend":    ("agents.daily_trend_agent",      "tasks.daily_trend_task",      []),
    "multiday_trend": ("agents.multiday_trend_agent",   "tasks.multiday_trend_task",   ["daily_trend"]),
    "news":           ("agents.news_analyst_agent",     "tasks.news_task",             []),
    "futurist":       ("agents.futurist_agent",         "tasks.futurist_task",         ["daily_trend", "multiday_trend", "news"]),
    "manager":        ("agents.project_manager_agent",  "tasks.manager_task",          ["daily_trend", "multiday_trend", "news", "futurist"]),
    "trader":         ("agents.trader_agent",           "tasks.trader_task",           ["manager"]),
}


def _import(dotpath: str):
    module, attr = dotpath.rsplit(".", 1)
    mod = __import__(module)
    return getattr(mod, attr)


def run_one(name: str):
    if name not in AGENT_MAP:
        print(f"Unknown agent '{name}'. Choices: {list(AGENT_MAP)}")
        sys.exit(1)

    agent_path, task_path, context_names = AGENT_MAP[name]
    agent = _import(agent_path)
    task  = _import(task_path)

    # Attach context tasks if needed
    if context_names:
        import tasks as tasks_mod
        task.context = [getattr(tasks_mod, f"{n}_task") for n in context_names]
        context_agents = [_import(AGENT_MAP[n][0]) for n in context_names]
    else:
        task.context = []
        context_agents = []

    ctx = task.context if isinstance(task.context, list) else []
    all_agents = context_agents + [agent]
    all_tasks  = ctx + [task]

    print(f"\n{'='*60}")
    print(f"Running: {name}  ({datetime.now().strftime('%H:%M:%S')})")
    print(f"{'='*60}\n")

    crew = Crew(agents=all_agents, tasks=all_tasks, process=Process.sequential, verbose=True)
    result = crew.kickoff()

    print(f"\n{'─'*60}")
    print(f"Result:\n{result}")
    print(f"{'─'*60}\n")
    return result


def run_full():
    import agents as a
    import tasks as t
    from crewai import Crew, Process

    crew = Crew(
        agents=[a.daily_trend_agent, a.multiday_trend_agent, a.news_analyst_agent,
                a.futurist_agent, a.project_manager_agent, a.trader_agent],
        tasks=[t.daily_trend_task, t.multiday_trend_task, t.news_task,
               t.futurist_task, t.manager_task, t.trader_task],
        process=Process.sequential,
        verbose=True,
    )
    result = crew.kickoff()
    print(f"\nFull pipeline result:\n{result}")
    return result


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "daily_trend"
    skip_gate = "--no-gate" in sys.argv

    if not skip_gate:
        from safety_gate import run_gate_check
        gate = run_gate_check()
        print(gate.report())
        if not gate.allowed:
            print("\n[gate] No trade signal — nothing to do. Use --no-gate to override.")
            sys.exit(0)
        print(f"\n[gate] Signal: {gate.signal} | Bias: {gate.bias} — proceeding.\n")

    if target == "full":
        run_full()
    else:
        run_one(target)
