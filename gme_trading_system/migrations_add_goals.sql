-- Add goal hierarchy and cost tracking tables
-- Run with: sqlite3 agent_memory.db < migrations_add_goals.sql

-- Missions
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Team Goals
CREATE TABLE IF NOT EXISTS team_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL,
    team VARCHAR(50) NOT NULL,  -- research, trading, risk, monitoring
    goal VARCHAR(500) NOT NULL,
    quarterly_target FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (mission_id) REFERENCES missions(id)
);

-- Agent Tasks
CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    agent_name VARCHAR(100) NOT NULL,
    task VARCHAR(500) NOT NULL,
    schedule VARCHAR(100),  -- "every 1 min", "event-driven", etc.
    required_for VARCHAR(100),  -- Which goal
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (goal_id) REFERENCES team_goals(id)
);

-- Agent Costs
CREATE TABLE IF NOT EXISTS agent_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name VARCHAR(100) NOT NULL,
    run_timestamp TIMESTAMP,
    llm_provider VARCHAR(50),  -- "deepseek", "gemini", "gemma"
    tokens_used INTEGER,
    cost_usd FLOAT,
    task_type VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Portfolio State
CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP,
    cash FLOAT,
    positions JSON,  -- {"GME": {"qty": 100, "avg_cost": 22.50, "current_price": 23.00}}
    unrealized_pnl FLOAT,
    realized_pnl FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Trade Proposals
CREATE TABLE IF NOT EXISTS trade_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_from VARCHAR(100),
    action VARCHAR(20),  -- BUY, SELL, SHORT, COVER, HOLD
    ticker VARCHAR(20),
    price FLOAT,
    quantity FLOAT,
    confidence FLOAT,
    reasoning TEXT,
    status VARCHAR(50),  -- PROPOSED, PENDING_APPROVAL, APPROVED, EXECUTED, REJECTED
    approved_by VARCHAR(100),
    approval_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP,
    executed_at TIMESTAMP
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_agent_costs_date ON agent_costs(run_timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_costs_agent ON agent_costs(agent_name);
CREATE INDEX IF NOT EXISTS idx_team_goals_mission ON team_goals(mission_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_goal ON agent_tasks(goal_id);

-- Bootstrap: Create default mission
INSERT OR IGNORE INTO missions (id, name, description)
VALUES (1, 'Profitable GME Trading', 'Maximize risk-adjusted returns via sentiment analysis, chart patterns, and structural intelligence');

-- Bootstrap: Create default goals
INSERT OR IGNORE INTO team_goals (mission_id, team, goal, quarterly_target) VALUES
(1, 'research', 'Identify 3+ strong signals daily', 100),
(1, 'trading', 'Execute profitable trades with >60% win rate', 50000),
(1, 'risk', 'Maintain zero blown positions (hard stop active)', 0),
(1, 'monitoring', 'Track all agent health and costs', 500);

-- Bootstrap: Link agents to goals (run one query per agent)
-- Will be populated as agents run
