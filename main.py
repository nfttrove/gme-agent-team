import json
import sqlite3
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from apscheduler.schedulers.blocking import BlockingScheduler
from crewai import Agent, Task, Crew, Process, LLM
from dotenv import load_dotenv

# Load API keys from .env
load_dotenv()

# ---------------------------
# 1. SHARED MEMORY (SQLite)
# ---------------------------
DB_PATH = "agent_memory.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS agent_outputs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  agent_name TEXT,
                  timestamp TEXT,
                  content TEXT,
                  task_type TEXT)''')
    conn.commit()
    conn.close()

def write_memory(agent_name, content, task_type):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO agent_outputs (agent_name, timestamp, content, task_type) VALUES (?, ?, ?, ?)",
              (agent_name, datetime.now(ZoneInfo("America/New_York")).isoformat(), content, task_type))
    conn.commit()
    conn.close()

# ---------------------------
# 2. MODEL ROUTING
# ---------------------------
# Local Worker Model (Ollama)
ollama_llm = LLM(
    model="ollama/gemma2:9b",
    base_url="http://localhost:11434",
    temperature=0.2,
)

# Cloud Reasoning Model (DeepSeek)
deepseek_llm = LLM(
    model="deepseek/deepseek-chat",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1",
    temperature=0.3,
)

# Roles
ANALYST_MODEL = ollama_llm
STRATEGIST_MODEL = ollama_llm
MANAGER_MODEL = deepseek_llm

# ---------------------------
# 3. DEFINE AGENTS & TASKS
# ---------------------------
analyst = Agent(
    role="Data Analyst",
    goal="Extract key metrics from the latest raw data",
    backstory="You specialize in reading raw data and outputting only JSON.",
    llm=ANALYST_MODEL,
    verbose=True,
)

strategist = Agent(
    role="Business Strategist",
    goal="Propose one actionable recommendation based on data",
    backstory="You translate data insights into business moves. Keep it concise.",
    llm=STRATEGIST_MODEL,
    verbose=True,
)

manager = Agent(
    role="Manager",
    goal="Coordinate the team and ensure no contradictions",
    backstory="You review all outputs and produce the final executive summary.",
    llm=MANAGER_MODEL,
)

task1 = Task(
    description="Analyze this input: 'Current server CPU usage is 85%, memory at 70%. Latency increased by 12%'. Output JSON: {cpu, memory, latency_change}.",
    expected_output='{"cpu": 85, "memory": 70, "latency_change": "+12%"}',
    agent=analyst,
)

task2 = Task(
    description="Take the analyst's JSON and write a short recommendation. Format: 'Action: ... Expected outcome: ...'",
    expected_output="Action: Scale up servers. Expected outcome: Latency drops.",
    agent=strategist,
    context=[task1],
)

task3 = Task(
    description="Review both outputs. If they conflict, prioritize the data. Output a final summary line.",
    expected_output="Final: Agree with strategist. CPU at 85% warrants scaling.",
    agent=manager,
    context=[task1, task2],
)

crew = Crew(
    agents=[analyst, strategist, manager],
    tasks=[task1, task2, task3],
    process=Process.sequential,
    verbose=True,
)

# ---------------------------
# 4. WRAPPER & SCHEDULER
# ---------------------------
def run_multi_agent_cycle():
    print(f"\n=== Starting new cycle at {datetime.now(ZoneInfo('America/New_York'))} ===")
    try:
        result = crew.kickoff()
        write_memory("Manager", str(result), "final_summary")
        print(f"Cycle complete. Result: {result}")
    except Exception as e:
        print(f"Cycle failed: {e}")
        write_memory("System", f"Error: {str(e)}", "error")

if __name__ == "__main__":
    init_db()
    scheduler = BlockingScheduler()
    scheduler.add_job(run_multi_agent_cycle, 'interval', minutes=30)

    # Run once immediately on start
    run_multi_agent_cycle()

    print("Multi-agent system started. Running every 30 minutes. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("Shutting down...")
